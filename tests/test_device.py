import asyncio
import errno
import hashlib
import json
import logging
import re
from pathlib import Path

import asyncssh
import pytest

import lte_pro_access.device as device_mod
from lte_pro_access.config import Config, load_config
from lte_pro_access.device import AsyncSshDevice, HostKeyChanged, SshRefused
from lte_pro_access.relays import LTE_SOCAT
from test_relays import SAMPLE

SOCAT_BYTES = b"binary-bytes"
PI_SOCAT_BYTES = b"pi-binary-bytes"
SOCAT_SHA = hashlib.sha256(SOCAT_BYTES).hexdigest()
UPLOAD_DIR = "/tmp/socat-upload.AbC123"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    binary = tmp_path / "socat-device"
    binary.write_bytes(SOCAT_BYTES)
    pi_binary = tmp_path / "socat-pi"
    pi_binary.write_bytes(PI_SOCAT_BYTES)
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


def new_key() -> asyncssh.SSHKey:
    return asyncssh.generate_private_key("ssh-ed25519").convert_to_public()


class FakeResult:
    def __init__(self, stdout):
        self.stdout = stdout


class FakeConn:
    def __init__(self, host_key=None, responder=None):
        self.host_key = host_key if host_key is not None else new_key()
        self.responder = responder or (lambda cmd, kwargs: "out\n")
        self.runs = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get_server_host_key(self):
        return self.host_key

    async def run(self, cmd, **kwargs):
        self.runs.append((cmd, kwargs))
        return FakeResult(self.responder(cmd, kwargs))


def patch_connect(monkeypatch, conn_factory):
    seen = []

    def fake_connect(host, **kwargs):
        seen.append({"host": host, **kwargs})
        return conn_factory()

    monkeypatch.setattr(device_mod.asyncssh, "connect", fake_connect)
    return seen


async def test_connect_options(cfg, monkeypatch):
    conn = FakeConn()
    seen = patch_connect(monkeypatch, lambda: conn)
    assert await AsyncSshDevice(cfg).run("netstat -tnl") == "out\n"
    assert seen[0]["host"] == cfg.lte_host
    assert seen[0]["username"] == cfg.ssh_user
    assert seen[0]["client_keys"] == [str(cfg.ssh_key_path)]
    assert conn.runs[0][0] == "netstat -tnl"
    assert conn.runs[0][1]["check"] is False


async def test_connection_refused_becomes_ssh_refused(cfg, monkeypatch):
    def fake_connect(host, **kwargs):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(device_mod.asyncssh, "connect", fake_connect)
    with pytest.raises(SshRefused):
        await AsyncSshDevice(cfg).run("netstat -tnl")


async def test_other_errors_are_not_masked(cfg, monkeypatch):
    def fake_connect(host, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(device_mod.asyncssh, "connect", fake_connect)
    with pytest.raises(TimeoutError):
        await AsyncSshDevice(cfg).run("netstat -tnl")


class HangingConn(FakeConn):
    async def run(self, cmd, **kwargs):
        self.runs.append((cmd, kwargs))
        await asyncio.Event().wait()


async def test_run_is_bounded_by_a_timeout(cfg, monkeypatch):
    monkeypatch.setattr(device_mod, "COMMAND_TIMEOUT_SECONDS", 0.05)
    patch_connect(monkeypatch, lambda: HangingConn())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(AsyncSshDevice(cfg).run("netstat -tnl"), 5)


async def test_upload_is_bounded_by_a_timeout(cfg, monkeypatch):
    monkeypatch.setattr(device_mod, "UPLOAD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(device_mod, "CLEANUP_TIMEOUT_SECONDS", 0.05)
    patch_connect(monkeypatch, lambda: HangingConn())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(AsyncSshDevice(cfg).upload_socat(), 5)


def upload_responder(*, final_sha=SOCAT_SHA, mktemp_out=f"{UPLOAD_DIR}\n"):
    def respond(cmd, kwargs):
        if cmd.startswith("mktemp"):
            return mktemp_out
        if cmd.startswith("sha256sum"):
            return f"{final_sha}  {LTE_SOCAT}\n"
        return ""

    return respond


async def test_upload_uses_private_dir_then_verifies_hash(cfg, monkeypatch):
    conn = FakeConn(responder=upload_responder())
    patch_connect(monkeypatch, lambda: conn)
    await AsyncSshDevice(cfg).upload_socat()
    cmds = [c for c, _ in conn.runs]
    assert cmds[0] == "mktemp -d /tmp/socat-upload.XXXXXX"
    assert not any(c.startswith("cat > /tmp/socat.new") or "/tmp/socat.new" in c for c in cmds)
    assert cmds[1] == f"cat > {UPLOAD_DIR}/socat && chmod 755 {UPLOAD_DIR}/socat"
    assert conn.runs[1][1]["input"] == SOCAT_BYTES
    assert conn.runs[1][1]["input"] != PI_SOCAT_BYTES
    assert conn.runs[1][1]["encoding"] is None
    assert conn.runs[1][1]["check"] is True
    assert cmds[2] == f"mv {UPLOAD_DIR}/socat {LTE_SOCAT}"
    assert cmds[3] == f"sha256sum {LTE_SOCAT}"
    assert cmds[-1] == f"rm -rf {UPLOAD_DIR}"


async def test_upload_fails_and_cleans_up_on_hash_mismatch(cfg, monkeypatch):
    conn = FakeConn(responder=upload_responder(final_sha="0" * 64))
    patch_connect(monkeypatch, lambda: conn)
    with pytest.raises(device_mod.UploadVerificationFailed):
        await AsyncSshDevice(cfg).upload_socat()
    assert conn.runs[-1][0] == f"rm -rf {UPLOAD_DIR}"


async def test_upload_removes_temp_dir_when_a_step_fails(cfg, monkeypatch):
    def respond(cmd, kwargs):
        if cmd.startswith("mktemp"):
            return f"{UPLOAD_DIR}\n"
        if cmd.startswith("cat >"):
            raise RuntimeError("write failed")
        return ""

    conn = FakeConn(responder=respond)
    patch_connect(monkeypatch, lambda: conn)
    with pytest.raises(RuntimeError, match="write failed"):
        await AsyncSshDevice(cfg).upload_socat()
    assert conn.runs[-1][0] == f"rm -rf {UPLOAD_DIR}"


@pytest.mark.parametrize("bad", ["", "/etc\n", "/tmp/a b\n", "/tmp/../etc\n", "/tmp/x;reboot\n", "relative\n"])
async def test_upload_rejects_unexpected_mktemp_output(cfg, monkeypatch, bad):
    conn = FakeConn(responder=upload_responder(mktemp_out=bad))
    patch_connect(monkeypatch, lambda: conn)
    with pytest.raises(RuntimeError, match="mktemp"):
        await AsyncSshDevice(cfg).upload_socat()
    assert all(not c.startswith(("cat", "mv", "rm")) for c, _ in conn.runs)


def pin_path(cfg: Config) -> Path:
    return cfg.state_dir / "lte_host_key.pub"


def stored_key(cfg: Config) -> asyncssh.SSHKey:
    return asyncssh.import_public_key(pin_path(cfg).read_text())


async def test_first_connection_pins_host_key(cfg, monkeypatch):
    key = new_key()
    seen = patch_connect(monkeypatch, lambda: FakeConn(host_key=key))
    await AsyncSshDevice(cfg).run("netstat -tnl")
    assert seen[0]["known_hosts"] is None
    assert stored_key(cfg).public_data == key.public_data
    assert (pin_path(cfg).stat().st_mode & 0o777) == 0o600


async def test_pinned_key_is_enforced_by_the_ssh_layer_on_later_connections(cfg, monkeypatch):
    key = new_key()
    seen = patch_connect(monkeypatch, lambda: FakeConn(host_key=key))
    device = AsyncSshDevice(cfg)
    await device.run("a")
    await device.run("b")
    keys, ca_keys, revoked = seen[1]["known_hosts"]
    assert [k.public_data for k in keys] == [key.public_data]
    assert list(ca_keys) == [] and list(revoked) == []


async def test_pin_survives_a_new_device_object(cfg, monkeypatch):
    key = new_key()
    patch_connect(monkeypatch, lambda: FakeConn(host_key=key))
    await AsyncSshDevice(cfg).run("a")
    other = new_key()
    conn = FakeConn(host_key=other)
    patch_connect(monkeypatch, lambda: conn)
    with pytest.raises(HostKeyChanged):
        await AsyncSshDevice(cfg).run("b")
    assert conn.runs == []


async def test_changed_key_is_refused_logged_and_runs_no_commands(cfg, monkeypatch, caplog):
    key = new_key()
    patch_connect(monkeypatch, lambda: FakeConn(host_key=key))
    device = AsyncSshDevice(cfg)
    await device.run("first")
    evil = FakeConn(host_key=new_key())
    patch_connect(monkeypatch, lambda: evil)
    with caplog.at_level(logging.WARNING, logger="lte_pro_access"), pytest.raises(HostKeyChanged):
        await device.run("netstat -tnl")
    assert evil.runs == []
    assert any(r.levelno == logging.WARNING and "host key" in r.getMessage() for r in caplog.records)
    assert stored_key(cfg).public_data == key.public_data


async def test_changed_key_rejected_during_upload_uploads_nothing(cfg, monkeypatch):
    patch_connect(monkeypatch, lambda: FakeConn(host_key=new_key(), responder=upload_responder()))
    device = AsyncSshDevice(cfg)
    await device.run("first")
    evil = FakeConn(host_key=new_key(), responder=upload_responder())
    patch_connect(monkeypatch, lambda: evil)
    with pytest.raises(HostKeyChanged):
        await device.upload_socat()
    assert evil.runs == []


async def test_ssh_layer_host_key_rejection_becomes_host_key_changed(cfg, monkeypatch, caplog):
    patch_connect(monkeypatch, lambda: FakeConn())
    device = AsyncSshDevice(cfg)
    await device.run("first")

    def fake_connect(host, **kwargs):
        raise asyncssh.HostKeyNotVerifiable("Host key is not trusted for host x")

    monkeypatch.setattr(device_mod.asyncssh, "connect", fake_connect)
    with caplog.at_level(logging.WARNING, logger="lte_pro_access"), pytest.raises(HostKeyChanged):
        await device.run("netstat -tnl")
    assert any(r.levelno == logging.WARNING and "host key" in r.getMessage() for r in caplog.records)


async def test_allow_host_key_change_accepts_one_new_key_and_repins(cfg, monkeypatch):
    patch_connect(monkeypatch, lambda: FakeConn())
    device = AsyncSshDevice(cfg)
    await device.run("first")
    new = new_key()
    device.allow_host_key_change()
    seen = patch_connect(monkeypatch, lambda: FakeConn(host_key=new))
    await device.run("after-reboot")
    assert seen[0]["known_hosts"] is None
    assert stored_key(cfg).public_data == new.public_data
    await device.run("again")
    assert seen[1]["known_hosts"] is not None


async def test_allowance_is_single_use(cfg, monkeypatch):
    patch_connect(monkeypatch, lambda: FakeConn())
    device = AsyncSshDevice(cfg)
    await device.run("first")
    device.allow_host_key_change()
    patch_connect(monkeypatch, lambda: FakeConn(host_key=new_key()))
    await device.run("accepted")
    patch_connect(monkeypatch, lambda: FakeConn(host_key=new_key()))
    with pytest.raises(HostKeyChanged):
        await device.run("refused")


async def test_allowance_survives_a_connection_that_never_reached_key_exchange(cfg, monkeypatch):
    patch_connect(monkeypatch, lambda: FakeConn())
    device = AsyncSshDevice(cfg)
    await device.run("first")
    device.allow_host_key_change()

    def refused(host, **kwargs):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(device_mod.asyncssh, "connect", refused)
    with pytest.raises(SshRefused):
        await device.run("early")
    new = new_key()
    patch_connect(monkeypatch, lambda: FakeConn(host_key=new))
    await device.run("late")
    assert stored_key(cfg).public_data == new.public_data


async def test_corrupt_pin_file_fails_closed(cfg, monkeypatch):
    cfg.state_dir.mkdir(parents=True)
    pin_path(cfg).write_text("not a key")
    conn = FakeConn()
    patch_connect(monkeypatch, lambda: conn)
    with pytest.raises(ValueError, match=re.escape(str(pin_path(cfg)))):
        await AsyncSshDevice(cfg).run("netstat -tnl")
    assert conn.runs == []


async def test_allowance_expires_after_two_connection_attempts(cfg, monkeypatch):
    patch_connect(monkeypatch, lambda: FakeConn())
    device = AsyncSshDevice(cfg)
    await device.run("first")
    device.allow_host_key_change()

    def refused(host, **kwargs):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")

    monkeypatch.setattr(device_mod.asyncssh, "connect", refused)
    for _ in range(2):
        with pytest.raises(SshRefused):
            await device.run("early")
    evil = FakeConn(host_key=new_key())
    patch_connect(monkeypatch, lambda: evil)
    with pytest.raises(HostKeyChanged):
        await device.run("too-late")
    assert evil.runs == []


async def test_repin_logs_old_and_new_fingerprints_at_warning(cfg, monkeypatch, caplog):
    old = new_key()
    patch_connect(monkeypatch, lambda: FakeConn(host_key=old))
    device = AsyncSshDevice(cfg)
    await device.run("first")
    new = new_key()
    device.allow_host_key_change()
    patch_connect(monkeypatch, lambda: FakeConn(host_key=new))
    with caplog.at_level(logging.WARNING, logger="lte_pro_access"):
        await device.run("after-reboot")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(old.get_fingerprint("sha256") in m and new.get_fingerprint("sha256") in m for m in warnings)
    assert old.get_fingerprint("sha256").startswith("SHA256:")
