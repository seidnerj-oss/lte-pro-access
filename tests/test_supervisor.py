import asyncio
import dataclasses
import hashlib
import json
import logging
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

from lte_pro_access.config import Config, load_config
from lte_pro_access.device import HostKeyChanged, SshRefused
from lte_pro_access.relays import LTE_SOCAT, lte_at_relay_cmd, lte_qmi_relay_cmd, pi_qmi_relay_argv
from lte_pro_access.supervisor import (
    RELAY_SETTLE_SECONDS,
    Supervisor,
    TickResult,
    parse_listening_ports,
    run_forever,
)
from test_relays import SAMPLE

SOCAT_BYTES = b"\x7fELF fake socat"
SOCAT_SHA = hashlib.sha256(SOCAT_BYTES).hexdigest()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    binary = tmp_path / "socat-device"
    binary.write_bytes(SOCAT_BYTES)
    pi_binary = tmp_path / "socat-pi"
    pi_binary.write_bytes(b"\x7fELF fake pi socat")
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                **SAMPLE,
                "socat_device_binary": str(binary),
                "socat_pi_binary": str(pi_binary),
                "state_dir": str(tmp_path / "state"),
            }
        )
    )
    return load_config(path)


def netstat_output(ports: set[int]) -> str:
    lines = [
        "Active Internet connections (only servers)",
        "Proto Recv-Q Send-Q Local Address           Foreign Address         State",
        "tcp        0      0 0.0.0.0:22              0.0.0.0:*               LISTEN",
        "tcp        0      0 192.168.1.20:22         192.168.1.10:51000       ESTABLISHED",
    ]
    lines += [f"tcp        0      0 0.0.0.0:{p}           0.0.0.0:*               LISTEN" for p in sorted(ports)]
    return "\n".join(lines) + "\n"


class FakeDevice:
    def __init__(
        self, cfg: Config, *, listening: set[int], socat_sha: str | None, refusals: int = 0, relays_die: bool = False
    ):
        self.cfg = cfg
        self.relays_die = relays_die
        self.listening = set(listening)
        self.socat_sha = socat_sha
        self.refusals = refusals
        self.commands: list[str] = []
        self.events: list[str] = []
        self.uploads = 0
        self.host_key_changed = False
        self.hang = False

    def allow_host_key_change(self) -> None:
        self.events.append("allow-host-key-change")

    def reboot(self) -> None:
        self.listening = set()
        self.socat_sha = None
        self.refusals = 1

    async def run(self, cmd: str) -> str:
        self.commands.append(cmd)
        self.events.append(f"run:{cmd}")
        if self.hang:
            await asyncio.Event().wait()
        if self.host_key_changed:
            raise HostKeyChanged("host key changed")
        if self.refusals:
            self.refusals -= 1
            raise SshRefused("connection refused")
        if cmd == "netstat -tnl":
            return netstat_output(self.listening)
        if cmd == f"sha256sum {LTE_SOCAT} 2>/dev/null":
            return f"{self.socat_sha}  {LTE_SOCAT}\n" if self.socat_sha else ""
        if cmd.startswith("nohup "):
            ports = [self.cfg.qmi_port] + ([self.cfg.at_relay.port] if self.cfg.at_relay is not None else [])
            for port in ports:
                if f"TCP-LISTEN:{port}," in cmd and not self.relays_die:
                    self.listening.add(port)
            return ""
        if cmd.startswith("killall "):
            return "killed\n"
        raise AssertionError(f"unexpected device command: {cmd}")

    async def upload_socat(self) -> None:
        self.uploads += 1
        self.socat_sha = SOCAT_SHA


class FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None


class FakeSpawner:
    def __init__(self, *, exits_with: int | None = None) -> None:
        self.calls: list[list[str]] = []
        self.procs: list[FakeProc] = []
        self.exits_with = exits_with

    async def __call__(self, argv: list[str]) -> FakeProc:
        self.calls.append(argv)
        proc = FakeProc()
        proc.returncode = self.exits_with
        self.procs.append(proc)
        return proc


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@dataclass(frozen=True)
class FakeEnableResult:
    key_added: bool
    dropbear_started: bool


class FakeEnableSsh:
    def __init__(self, events: list[str] | None = None, *, dropbear_started: bool = True) -> None:
        self.calls: list[Config] = []
        self.events = events
        self.dropbear_started = dropbear_started

    async def __call__(self, cfg: Config) -> FakeEnableResult:
        self.calls.append(cfg)
        if self.events is not None:
            self.events.append("enable-ssh")
        return FakeEnableResult(key_added=True, dropbear_started=self.dropbear_started)


def make(cfg, device, *, dropbear_started=True, pi_relay_exits_with=None):
    enable = FakeEnableSsh(getattr(device, "events", None), dropbear_started=dropbear_started)
    spawner = FakeSpawner(exits_with=pi_relay_exits_with)
    return Supervisor(cfg, device, enable, spawner, FakeSleep()), enable, spawner


def healthy_device(cfg):
    return FakeDevice(cfg, listening={cfg.qmi_port, cfg.at_relay.port}, socat_sha=SOCAT_SHA)


async def test_healthy_tick_takes_no_action(cfg):
    sup, enable, spawner = make(cfg, healthy_device(cfg))
    assert (await sup.tick()).actions == ["started-pi-relay"]
    assert await sup.tick() == TickResult(actions=[], relays_up=True)
    assert enable.calls == []
    assert spawner.calls == [pi_qmi_relay_argv(cfg)]


async def test_reboot_recovery(cfg):
    device = healthy_device(cfg)
    sup, enable, _ = make(cfg, device)
    await sup.tick()
    device.reboot()
    result = await sup.tick()
    assert result.actions == ["enabled-ssh", "uploaded-socat", "started-qmi-relay", "started-at-relay"]
    assert enable.calls == [cfg]
    assert device.uploads == 1
    assert f"nohup {lte_qmi_relay_cmd(cfg)} >/dev/null 2>&1 &" in device.commands
    assert f"nohup {lte_at_relay_cmd(cfg, cfg.at_relay)} >/dev/null 2>&1 &" in device.commands
    assert (await sup.tick()).actions == []


async def test_ssh_still_refused_after_enable_propagates(cfg):
    device = healthy_device(cfg)
    device.refusals = 2
    sup, enable, _ = make(cfg, device)
    with pytest.raises(SshRefused):
        await sup.tick()
    assert len(enable.calls) == 1


class ConnectGuard:
    def __init__(self) -> None:
        self.attempts: list[object] = []

    def at_port_attempts(self, port: int) -> list[object]:
        return [a for a in self.attempts if isinstance(a, tuple) and a[-1] == port]


@pytest.fixture
def connect_guard(monkeypatch) -> ConnectGuard:
    guard = ConnectGuard()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self, address):
        guard.attempts.append(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        guard.attempts.append(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    return guard


async def test_connect_guard_detects_a_connection_to_the_at_port(cfg, connect_guard):
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", cfg.at_relay.port)
    assert connect_guard.at_port_attempts(cfg.at_relay.port) != []


async def test_probe_never_connects_to_at_port(cfg, connect_guard):
    device = FakeDevice(cfg, listening=set(), socat_sha=None)
    sup, _, _ = make(cfg, device)
    await sup.tick()
    await sup.tick()
    assert all(c.startswith(("netstat", "sha256sum", "nohup ")) for c in device.commands)
    assert not any(str(cfg.at_relay.port) in c for c in device.commands if not c.startswith("nohup "))
    assert connect_guard.at_port_attempts(cfg.at_relay.port) == []
    assert connect_guard.attempts == []


async def test_starts_only_missing_relay(cfg):
    device = FakeDevice(cfg, listening={cfg.qmi_port}, socat_sha=SOCAT_SHA)
    sup, _, _ = make(cfg, device)
    assert (await sup.tick()).actions == ["started-pi-relay", "started-at-relay"]
    assert [c for c in device.commands if c.startswith("nohup")] == [
        f"nohup {lte_at_relay_cmd(cfg, cfg.at_relay)} >/dev/null 2>&1 &"
    ]


async def test_never_kills_running_relays(cfg):
    device = FakeDevice(cfg, listening={cfg.qmi_port}, socat_sha="0" * 64)
    sup, _, _ = make(cfg, device)
    await sup.tick()
    await sup.tick()
    assert not any("kill" in c for c in device.commands)


async def test_reuploads_socat_on_hash_mismatch(cfg):
    device = FakeDevice(cfg, listening={cfg.qmi_port, cfg.at_relay.port}, socat_sha="0" * 64)
    sup, _, _ = make(cfg, device)
    assert (await sup.tick()).actions == ["started-pi-relay", "uploaded-socat"]
    assert device.uploads == 1
    assert (await sup.tick()).actions == []
    assert device.uploads == 1


async def test_socat_missing_triggers_upload(cfg):
    device = FakeDevice(cfg, listening={cfg.qmi_port, cfg.at_relay.port}, socat_sha=None)
    sup, _, _ = make(cfg, device)
    assert "uploaded-socat" in (await sup.tick()).actions


async def test_pi_relay_restarted_when_it_exits(cfg):
    sup, _, spawner = make(cfg, healthy_device(cfg))
    await sup.tick()
    spawner.procs[0].returncode = 1
    assert (await sup.tick()).actions == ["started-pi-relay"]
    assert len(spawner.calls) == 2
    assert (await sup.tick()).actions == []


async def test_pi_relay_still_supervised_when_device_unreachable(cfg):
    device = healthy_device(cfg)
    device.refusals = 2
    sup, _, spawner = make(cfg, device)
    with pytest.raises(SshRefused):
        await sup.tick()
    assert len(spawner.calls) == 1


async def test_actions_are_logged_at_info(cfg, caplog):
    sup, _, _ = make(cfg, FakeDevice(cfg, listening=set(), socat_sha=None))
    with caplog.at_level(logging.INFO, logger="lte_pro_access"):
        await sup.tick()
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("uploaded-socat" in m for m in messages)
    assert any("started-qmi-relay" in m for m in messages)


def test_parse_listening_ports():
    out = netstat_output({18222, 182220})
    out += "tcp 0 0 :::18999 :::* LISTEN\ntcp 0 0 1.2.3.4:18223 5.6.7.8:1 ESTABLISHED\n"
    assert parse_listening_ports(out) == {22, 18222, 182220, 18999}


async def test_run_forever_survives_tick_exception(cfg, caplog):
    class Flaky:
        def __init__(self):
            self.ticks = 0

        async def tick(self):
            self.ticks += 1
            if self.ticks == 1:
                raise RuntimeError("boom")
            return TickResult(actions=[], relays_up=True)

    class Stop(Exception):
        pass

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    flaky = Flaky()
    with caplog.at_level(logging.ERROR, logger="lte_pro_access"), pytest.raises(Stop):
        await run_forever(flaky, cfg.check_interval_seconds, 5, fake_sleep)
    assert flaky.ticks == 3
    assert sleeps == [30, 30, 30]
    assert any("boom" in (r.exc_text or "") for r in caplog.records)


async def test_host_key_change_is_allowed_only_after_enable_ssh_following_a_refusal(cfg):
    device = healthy_device(cfg)
    sup, enable, _ = make(cfg, device)
    await sup.tick()
    assert "allow-host-key-change" not in device.events
    device.events.clear()
    device.reboot()
    await sup.tick()
    assert [e for e in device.events if not e.startswith("run:")] == ["enable-ssh", "allow-host-key-change"]
    allow_at = device.events.index("allow-host-key-change")
    assert device.events[allow_at - 1] == "enable-ssh"
    assert device.events[allow_at + 1] == "run:netstat -tnl"
    assert device.events.count("allow-host-key-change") == 1


async def test_changed_host_key_fails_the_tick_without_enabling_or_running_more(cfg):
    device = healthy_device(cfg)
    device.host_key_changed = True
    sup, enable, spawner = make(cfg, device)
    with pytest.raises(HostKeyChanged):
        await sup.tick()
    assert enable.calls == []
    assert device.commands == ["netstat -tnl"]
    assert "allow-host-key-change" not in device.events
    assert len(spawner.calls) == 1


async def test_run_forever_bounds_a_hanging_tick_and_continues(cfg, caplog):
    class Hangs:
        def __init__(self):
            self.ticks = 0

        async def tick(self):
            self.ticks += 1
            await asyncio.Event().wait()

    class Stop(Exception):
        pass

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    hangs = Hangs()
    with caplog.at_level(logging.ERROR, logger="lte_pro_access"), pytest.raises(Stop):
        await asyncio.wait_for(run_forever(hangs, 30, 0.05, fake_sleep), 5)
    assert hangs.ticks == 3
    assert sleeps == [30, 30, 30]
    assert any("timed out" in r.getMessage() for r in caplog.records)


async def test_pi_relay_supervision_continues_while_the_device_hangs(cfg):
    device = healthy_device(cfg)
    device.hang = True
    sup, _, spawner = make(cfg, device)

    class Stop(Exception):
        pass

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            spawner.procs[0].returncode = 1
        if len(sleeps) == 2:
            raise Stop

    with pytest.raises(Stop):
        await asyncio.wait_for(run_forever(sup, 30, 0.05, fake_sleep), 5)
    assert len(spawner.calls) == 2


async def test_host_key_change_not_allowed_when_dropbear_was_already_running(cfg, caplog):
    device = healthy_device(cfg)
    sup, enable, _ = make(cfg, device, dropbear_started=False)
    await sup.tick()
    device.reboot()
    with caplog.at_level(logging.WARNING, logger="lte_pro_access"):
        await sup.tick()
    assert len(enable.calls) == 1
    assert "allow-host-key-change" not in device.events
    assert any(r.levelno == logging.WARNING and "dropbear" in r.getMessage() for r in caplog.records)


async def test_device_binary_is_hashed_and_pi_binary_is_executed(cfg):
    device = healthy_device(cfg)
    sup, _, spawner = make(cfg, device)
    result = await sup.tick()
    assert "uploaded-socat" not in result.actions
    assert device.uploads == 0
    assert spawner.calls[0][0] == str(cfg.socat_pi_binary)
    assert cfg.socat_pi_binary != cfg.socat_device_binary
    assert hashlib.sha256(cfg.socat_device_binary.read_bytes()).hexdigest() == SOCAT_SHA
    assert hashlib.sha256(cfg.socat_pi_binary.read_bytes()).hexdigest() != SOCAT_SHA


async def test_lte_relay_that_is_still_not_listening_after_start_is_reported(cfg, caplog):
    device = FakeDevice(cfg, listening={cfg.at_relay.port}, socat_sha=SOCAT_SHA, relays_die=True)
    sup, _, _ = make(cfg, device)
    with caplog.at_level(logging.INFO, logger="lte_pro_access"):
        result = await sup.tick()
    assert result.actions == ["started-pi-relay", "started-qmi-relay", "failed-qmi-relay"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "failed-qmi-relay" in warnings[0]
    assert str(cfg.qmi_port) in warnings[0]
    assert device.commands[-1] == "netstat -tnl"


async def test_lte_relays_are_reprobed_once_after_starting_both(cfg):
    device = FakeDevice(cfg, listening=set(), socat_sha=SOCAT_SHA, relays_die=True)
    sup, _, _ = make(cfg, device)
    result = await sup.tick()
    assert result.actions[-2:] == ["failed-qmi-relay", "failed-at-relay"]
    assert device.commands.count("netstat -tnl") == 2


async def test_no_reprobe_when_no_lte_relay_was_started(cfg):
    device = healthy_device(cfg)
    sup, _, _ = make(cfg, device)
    await sup.tick()
    assert device.commands.count("netstat -tnl") == 1


async def test_pi_relay_that_exits_right_after_starting_is_reported(cfg, caplog):
    sup, _, spawner = make(cfg, healthy_device(cfg), pi_relay_exits_with=1)
    with caplog.at_level(logging.INFO, logger="lte_pro_access"):
        result = await sup.tick()
    assert result.actions == ["failed-pi-relay"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "return code 1" in warnings[0]
    assert not any("started-pi-relay" in r.getMessage() for r in caplog.records)
    assert (await sup.tick()).actions == ["failed-pi-relay"]
    assert len(spawner.calls) == 2


async def test_pi_relay_is_given_a_moment_before_its_exit_status_is_read(cfg):
    sleep = FakeSleep()
    sup = Supervisor(cfg, healthy_device(cfg), FakeEnableSsh(), FakeSpawner(), sleep)
    await sup.tick()
    assert sleep.calls == [RELAY_SETTLE_SECONDS]


@pytest.fixture
def cfg_no_at(cfg: Config) -> Config:
    return dataclasses.replace(cfg, at_relay=None)


async def test_without_at_relay_only_the_qmi_relay_is_started(cfg_no_at):
    device = FakeDevice(cfg_no_at, listening=set(), socat_sha=SOCAT_SHA)
    sup, _, _ = make(cfg_no_at, device)
    result = await sup.tick()
    assert result == TickResult(actions=["started-pi-relay", "started-qmi-relay"], relays_up=True)
    assert [c for c in device.commands if c.startswith("nohup")] == [
        f"nohup {lte_qmi_relay_cmd(cfg_no_at)} >/dev/null 2>&1 &"
    ]
    assert not any("OPEN:" in c for c in device.commands)


async def test_without_at_relay_a_healthy_device_needs_only_the_qmi_port(cfg_no_at):
    device = FakeDevice(cfg_no_at, listening={cfg_no_at.qmi_port}, socat_sha=SOCAT_SHA)
    sup, _, _ = make(cfg_no_at, device)
    await sup.tick()
    assert await sup.tick() == TickResult(actions=[], relays_up=True)
    assert sup.relays_up


async def test_relays_up_is_false_before_the_first_tick(cfg):
    sup, _, _ = make(cfg, healthy_device(cfg))
    assert not sup.relays_up


async def test_relays_up_after_a_healthy_tick(cfg):
    sup, _, _ = make(cfg, healthy_device(cfg))
    assert (await sup.tick()).relays_up
    assert sup.relays_up


async def test_relays_up_is_false_when_a_device_relay_fails_to_listen(cfg):
    device = FakeDevice(cfg, listening={cfg.qmi_port}, socat_sha=SOCAT_SHA, relays_die=True)
    sup, _, _ = make(cfg, device)
    assert not (await sup.tick()).relays_up
    assert not sup.relays_up


async def test_relays_up_is_false_when_the_pi_relay_fails_to_start(cfg):
    sup, _, _ = make(cfg, healthy_device(cfg), pi_relay_exits_with=1)
    assert not (await sup.tick()).relays_up
    assert not sup.relays_up


async def test_relays_up_turns_false_when_the_pi_relay_exits_between_ticks(cfg):
    sup, _, spawner = make(cfg, healthy_device(cfg))
    await sup.tick()
    spawner.procs[0].returncode = 1
    assert not sup.relays_up


async def test_relays_up_is_false_after_a_tick_that_raised(cfg):
    device = healthy_device(cfg)
    sup, _, _ = make(cfg, device)
    await sup.tick()
    device.host_key_changed = True
    with pytest.raises(HostKeyChanged):
        await sup.tick()
    assert not sup.relays_up


async def test_relays_up_is_false_after_a_tick_that_timed_out(cfg):
    device = healthy_device(cfg)
    sup, _, _ = make(cfg, device)
    await sup.tick()
    device.hang = True
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sup.tick(), 0.05)
    assert not sup.relays_up


async def test_relays_up_keeps_the_previous_result_while_a_tick_runs(cfg):
    device = healthy_device(cfg)
    sup, _, _ = make(cfg, device)
    await sup.tick()
    device.hang = True
    task = asyncio.ensure_future(sup.tick())
    await asyncio.sleep(0)
    assert sup.relays_up
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sup.relays_up


async def test_run_on_device_returns_the_command_output(cfg):
    device = healthy_device(cfg)
    sup, enable, _ = make(cfg, device)
    assert await sup.run_on_device("killall socat") == "killed\n"
    assert device.commands == ["killall socat"]
    assert enable.calls == []


async def test_run_on_device_enables_ssh_after_a_refusal_and_retries(cfg):
    device = healthy_device(cfg)
    device.refusals = 1
    sup, enable, _ = make(cfg, device)
    assert await sup.run_on_device("killall socat") == "killed\n"
    assert enable.calls == [cfg]
    assert [e for e in device.events if not e.startswith("run:")] == ["enable-ssh", "allow-host-key-change"]
    assert device.commands == ["killall socat", "killall socat"]


async def test_run_on_device_does_not_allow_a_host_key_change_when_dropbear_was_already_running(cfg):
    device = healthy_device(cfg)
    device.refusals = 1
    sup, enable, _ = make(cfg, device, dropbear_started=False)
    await sup.run_on_device("killall socat")
    assert len(enable.calls) == 1
    assert "allow-host-key-change" not in device.events


async def test_run_on_device_propagates_a_changed_host_key_without_enabling_ssh(cfg):
    device = healthy_device(cfg)
    device.host_key_changed = True
    sup, enable, _ = make(cfg, device)
    with pytest.raises(HostKeyChanged):
        await sup.run_on_device("killall socat")
    assert enable.calls == []
    assert device.commands == ["killall socat"]


async def test_run_on_device_propagates_a_refusal_that_enabling_ssh_did_not_fix(cfg):
    device = healthy_device(cfg)
    device.refusals = 2
    sup, enable, _ = make(cfg, device)
    with pytest.raises(SshRefused):
        await sup.run_on_device("killall socat")
    assert len(enable.calls) == 1


class ConcurrencyProbeDevice(FakeDevice):
    """Counts how many device commands are in flight at once; each command yields to the loop several times."""

    def __init__(self, cfg: Config, *, listening: set[int], socat_sha: str | None):
        super().__init__(cfg, listening=listening, socat_sha=socat_sha)
        self.in_flight = 0
        self.max_in_flight = 0

    async def run(self, cmd: str) -> str:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            return await super().run(cmd)
        finally:
            self.in_flight -= 1


async def test_overlapping_ticks_do_not_run_concurrently(cfg):
    device = ConcurrencyProbeDevice(cfg, listening={cfg.qmi_port, cfg.at_relay.port}, socat_sha=SOCAT_SHA)
    sup, _, spawner = make(cfg, device)
    results = await asyncio.gather(sup.tick(), sup.tick(), sup.tick())
    assert device.max_in_flight == 1
    assert len(spawner.calls) == 1
    assert sorted(len(r.actions) for r in results) == [0, 0, 1]


async def test_a_tick_overlapping_run_on_device_waits_for_it(cfg):
    device = ConcurrencyProbeDevice(cfg, listening={cfg.qmi_port, cfg.at_relay.port}, socat_sha=SOCAT_SHA)
    sup, _, _ = make(cfg, device)
    await asyncio.gather(sup.tick(), sup.run_on_device("killall socat"), sup.tick())
    assert device.max_in_flight == 1


async def test_a_timed_out_tick_releases_the_lock(cfg):
    device = healthy_device(cfg)
    device.hang = True
    sup, _, _ = make(cfg, device)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(sup.tick(), 0.05)
    device.hang = False
    assert (await asyncio.wait_for(sup.tick(), 5)).relays_up
    assert await asyncio.wait_for(sup.run_on_device("killall socat"), 5) == "killed\n"
