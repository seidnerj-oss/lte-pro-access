import asyncio
import contextlib
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any, Protocol

import asyncssh

from lte_pro_access.config import Config
from lte_pro_access.relays import LTE_SOCAT

logger = logging.getLogger("lte_pro_access")

CONNECT_TIMEOUT_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 20
UPLOAD_TIMEOUT_SECONDS = 60
CLEANUP_TIMEOUT_SECONDS = 10
HOST_KEY_CHANGE_ATTEMPTS = 2
HOST_KEY_PIN_FILENAME = "lte_host_key.pub"
UPLOAD_DIR_TEMPLATE = "/tmp/socat-upload.XXXXXX"
_UPLOAD_DIR_PATTERN = re.compile(r"/tmp/socat-upload\.[A-Za-z0-9]+")


class SshRefused(Exception):
    """The device actively refused the SSH connection (sshd is not running)."""


class HostKeyChanged(Exception):
    """The device presented an SSH host key different from the pinned one, and a change is not allowed."""


class UploadVerificationFailed(RuntimeError):
    """The socat binary on the device does not match the local binary after the upload."""


class Device(Protocol):
    async def run(self, cmd: str) -> str: ...

    async def upload_socat(self) -> None: ...

    def allow_host_key_change(self) -> None: ...


class AsyncSshDevice:
    """Runs commands on the LTE device over SSH.

    The device generates a new host key on every reboot, so its key is pinned per boot cycle (trust on
    first use): the accepted key is stored in `<state_dir>/lte_host_key.pub` and enforced on every later
    connection. A different key is accepted only once, on the first connection that reaches key
    verification after `allow_host_key_change()`, which the supervisor calls only when the authenticated
    UniFi controller channel confirmed that it started dropbear (that is a reboot; a TCP refusal alone is
    spoofable). The allowance covers at most `HOST_KEY_CHANGE_ATTEMPTS` connection attempts, so a slow sshd
    start does not lock the pin in but a stale allowance cannot linger. Every re-pin is logged at WARNING
    with both fingerprints. Any other change is refused before a single command runs.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._allow_attempts = 0
        self._pin_path = cfg.state_dir / HOST_KEY_PIN_FILENAME

    def allow_host_key_change(self) -> None:
        self._allow_attempts = HOST_KEY_CHANGE_ATTEMPTS

    def _load_pin(self) -> asyncssh.SSHKey | None:
        try:
            text = self._pin_path.read_text()
        except FileNotFoundError:
            return None
        try:
            return asyncssh.import_public_key(text)
        except asyncssh.KeyImportError as exc:
            raise ValueError(f"pinned host key file {self._pin_path} is not a valid public key") from exc

    def _store_pin(self, key: asyncssh.SSHKey) -> None:
        self._pin_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self._pin_path.with_name(f"{self._pin_path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.export_public_key())
        os.replace(tmp, self._pin_path)

    def _refuse(self, detail: str) -> HostKeyChanged:
        message = f"SSH host key of {self._cfg.lte_host} changed unexpectedly ({detail}); refusing to connect"
        logger.warning("%s", message)
        return HostKeyChanged(message)

    def _verify_host_key(self, conn: Any, pinned: asyncssh.SSHKey | None, enforced: bool) -> None:
        key = conn.get_server_host_key()
        if key is None:
            raise self._refuse("no host key presented")
        if pinned is not None and key.public_data == pinned.public_data:
            self._allow_attempts = 0
            return
        if enforced:
            raise self._refuse("not the pinned key")
        self._allow_attempts = 0
        if pinned is not None:
            logger.warning(
                "re-pinning SSH host key of %s after the controller confirmed dropbear was started: old %s, new %s",
                self._cfg.lte_host,
                pinned.get_fingerprint("sha256"),
                key.get_fingerprint("sha256"),
            )
        self._store_pin(key)

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        pinned = self._load_pin()
        enforced = pinned is not None and self._allow_attempts == 0
        self._allow_attempts = max(self._allow_attempts - 1, 0)
        known_hosts = ([pinned], [], []) if enforced else None
        try:
            async with asyncssh.connect(
                self._cfg.lte_host,
                username=self._cfg.ssh_user,
                client_keys=[str(self._cfg.ssh_key_path)],
                known_hosts=known_hosts,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
            ) as conn:
                self._verify_host_key(conn, pinned, enforced)
                yield conn
        except ConnectionRefusedError as exc:
            raise SshRefused(str(exc)) from exc
        except asyncssh.HostKeyNotVerifiable as exc:
            self._allow_attempts = 0
            raise self._refuse(str(exc)) from exc

    async def run(self, cmd: str) -> str:
        async with self._session() as conn:
            result = await asyncio.wait_for(conn.run(cmd, check=False, stdin=asyncssh.DEVNULL), COMMAND_TIMEOUT_SECONDS)
        return str(result.stdout or "")

    async def upload_socat(self) -> None:
        data = await asyncio.to_thread(self._cfg.socat_device_binary.read_bytes)
        local_sha = hashlib.sha256(data).hexdigest()
        async with self._session() as conn:
            made = await asyncio.wait_for(
                conn.run(f"mktemp -d {UPLOAD_DIR_TEMPLATE}", check=True), COMMAND_TIMEOUT_SECONDS
            )
            upload_dir = str(made.stdout or "").strip()
            if not _UPLOAD_DIR_PATTERN.fullmatch(upload_dir):
                raise RuntimeError(f"mktemp -d returned an unexpected path: {upload_dir!r}")
            try:
                staged = f"{upload_dir}/socat"
                await asyncio.wait_for(
                    conn.run(f"cat > {staged} && chmod 755 {staged}", input=data, encoding=None, check=True),
                    UPLOAD_TIMEOUT_SECONDS,
                )
                await asyncio.wait_for(conn.run(f"mv {staged} {LTE_SOCAT}", check=True), COMMAND_TIMEOUT_SECONDS)
                checked = await asyncio.wait_for(
                    conn.run(f"sha256sum {LTE_SOCAT}", check=True), COMMAND_TIMEOUT_SECONDS
                )
                tokens = str(checked.stdout or "").split()
                if not tokens or tokens[0] != local_sha:
                    raise UploadVerificationFailed(
                        f"{LTE_SOCAT} on the device does not match the local binary after upload"
                    )
            finally:
                try:
                    await asyncio.wait_for(conn.run(f"rm -rf {upload_dir}", check=False), CLEANUP_TIMEOUT_SECONDS)
                except Exception:
                    logger.warning("could not remove temporary upload directory %s", upload_dir, exc_info=True)
