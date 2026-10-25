import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from lte_pro_access.config import Config
from lte_pro_access.device import Device, SshRefused
from lte_pro_access.relays import LTE_SOCAT, lte_at_relay_cmd, lte_qmi_relay_cmd, pi_qmi_relay_argv
from lte_pro_access.ssh_enable import EnableResult

logger = logging.getLogger("lte_pro_access")

TICK_TIMEOUT_SECONDS = 180
RELAY_SETTLE_SECONDS = 1

ENABLED_SSH = "enabled-ssh"
UPLOADED_SOCAT = "uploaded-socat"
STARTED_QMI_RELAY = "started-qmi-relay"
STARTED_AT_RELAY = "started-at-relay"
STARTED_PI_RELAY = "started-pi-relay"
FAILED_QMI_RELAY = "failed-qmi-relay"
FAILED_AT_RELAY = "failed-at-relay"
FAILED_PI_RELAY = "failed-pi-relay"


class LocalProcess(Protocol):
    returncode: int | None

    def terminate(self) -> None: ...


EnableSsh = Callable[[Config], Awaitable[EnableResult]]
SpawnLocal = Callable[[list[str]], Awaitable[LocalProcess]]


@dataclass(frozen=True)
class TickResult:
    """What a tick did, and whether it left every relay up: the Pi relay, the device QMI relay and, when
    configured, the device AT relay."""

    actions: list[str]
    relays_up: bool


class Ticker(Protocol):
    async def tick(self) -> TickResult: ...


def parse_listening_ports(netstat_output: str) -> set[int]:
    """Ports in LISTEN state from BusyBox `netstat -tnl` output."""
    ports = set()
    for line in netstat_output.splitlines():
        parts = line.split()
        if "LISTEN" in parts and len(parts) >= 4:
            _, _, port = parts[3].rpartition(":")
            if port.isdigit():
                ports.add(int(port))
    return ports


def _first_token(text: str) -> str | None:
    tokens = text.split()
    return tokens[0] if tokens else None


class Supervisor:
    def __init__(
        self,
        cfg: Config,
        device: Device,
        enable_ssh: EnableSsh,
        spawn_local: SpawnLocal,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._cfg = cfg
        self._device = device
        self._enable_ssh = enable_ssh
        self._spawn_local = spawn_local
        self._sleep = sleep
        self._pi_relay: LocalProcess | None = None
        self._last_result: TickResult | None = None
        self._lock = asyncio.Lock()

    @property
    def relays_up(self) -> bool:
        """Whether the last tick completed and left every relay up, and the Pi relay is still running.

        False before the first tick and after a tick that raised or was cancelled (a timed-out tick is cancelled).
        A tick in progress leaves the previous result in place, so a health check never sees a running tick as down.
        """
        return self._last_result is not None and self._last_result.relays_up and self._pi_relay_running()

    def _pi_relay_running(self) -> bool:
        return self._pi_relay is not None and self._pi_relay.returncode is None

    async def tick(self) -> TickResult:
        """Runs one supervision pass. Safe to call while `run_forever` runs: passes and `run_on_device` calls are
        serialized, so an overlapping call waits for the running one to finish."""
        async with self._lock:
            return await self._tick()

    async def run_on_device(self, command: str) -> str:
        """Runs `command` on the device over the supervisor's pinned-host-key SSH path and returns its stdout.

        Like a tick, it enables SSH through the UniFi controller when the device refuses the connection, then retries
        once. The exit status is not checked. Serialized with ticks, so it can wait for a running tick.
        """
        async with self._lock:
            return await self._run_enabling_ssh(command, lambda action: logger.info("%s", action))

    async def _tick(self) -> TickResult:
        actions: list[str] = []

        def record(action: str) -> None:
            actions.append(action)
            logger.info("%s", action)

        def fail(action: str, message: str) -> None:
            actions.append(action)
            logger.warning("%s: %s", action, message)

        try:
            await self._ensure_pi_relay(record, fail)
            listening = await self._probe(record)
            await self._ensure_socat(record)
            lte_relays_up = await self._ensure_lte_relays(listening, record, fail)
        except BaseException:
            self._last_result = None
            raise
        self._last_result = TickResult(actions=actions, relays_up=lte_relays_up and self._pi_relay_running())
        return self._last_result

    def close(self) -> None:
        if self._pi_relay is not None and self._pi_relay.returncode is None:
            self._pi_relay.terminate()

    async def _ensure_pi_relay(self, record: Callable[[str], None], fail: Callable[[str, str], None]) -> None:
        if self._pi_relay is None or self._pi_relay.returncode is not None:
            self._pi_relay = await self._spawn_local(pi_qmi_relay_argv(self._cfg))
            await self._sleep(RELAY_SETTLE_SECONDS)
            if self._pi_relay.returncode is not None:
                fail(FAILED_PI_RELAY, f"Pi relay exited right after starting (return code {self._pi_relay.returncode})")
            else:
                record(STARTED_PI_RELAY)

    async def _probe(self, record: Callable[[str], None]) -> set[int]:
        return parse_listening_ports(await self._run_enabling_ssh("netstat -tnl", record))

    async def _run_enabling_ssh(self, command: str, record: Callable[[str], None]) -> str:
        try:
            return await self._device.run(command)
        except SshRefused:
            result = await self._enable_ssh(self._cfg)
            record(ENABLED_SSH)
            if result.dropbear_started:
                self._device.allow_host_key_change()
            else:
                logger.warning(
                    "SSH was refused but dropbear was already running on the device (no reboot confirmed by the "
                    "controller); not allowing an SSH host key change"
                )
            return await self._device.run(command)

    async def _ensure_socat(self, record: Callable[[str], None]) -> None:
        local_bytes = await asyncio.to_thread(self._cfg.socat_device_binary.read_bytes)
        local_sha = hashlib.sha256(local_bytes).hexdigest()
        remote_sha = _first_token(await self._device.run(f"sha256sum {LTE_SOCAT} 2>/dev/null"))
        if remote_sha != local_sha:
            await self._device.upload_socat()
            record(UPLOADED_SOCAT)

    async def _ensure_lte_relays(
        self, listening: set[int], record: Callable[[str], None], fail: Callable[[str, str], None]
    ) -> bool:
        """Starts each configured device relay whose port is not listening; returns whether all of them listen."""
        relays = [(self._cfg.qmi_port, lte_qmi_relay_cmd(self._cfg), STARTED_QMI_RELAY, FAILED_QMI_RELAY)]
        at_relay = self._cfg.at_relay
        if at_relay is not None:
            relays.append((at_relay.port, lte_at_relay_cmd(self._cfg, at_relay), STARTED_AT_RELAY, FAILED_AT_RELAY))
        started = []
        for port, cmd, started_action, failed_action in relays:
            if port not in listening:
                await self._device.run(f"nohup {cmd} >/dev/null 2>&1 &")
                record(started_action)
                started.append((port, failed_action))
        if not started:
            return True
        await self._sleep(RELAY_SETTLE_SECONDS)
        now_listening = parse_listening_ports(await self._device.run("netstat -tnl"))
        for port, failed_action in started:
            if port not in now_listening:
                fail(failed_action, f"port {port} is still not listening on the device after starting its relay")
        return all(port in now_listening for port, _, _, _ in relays)


async def run_forever(
    supervisor: Ticker,
    interval_seconds: int,
    tick_timeout_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    while True:
        try:
            await asyncio.wait_for(supervisor.tick(), tick_timeout_seconds)
        except TimeoutError:
            logger.error(
                "tick timed out after %s seconds; the next tick starts after the interval", tick_timeout_seconds
            )
        except Exception:
            logger.exception("tick failed")
        await sleep(interval_seconds)
