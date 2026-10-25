import json
import sys
import types
from pathlib import Path

import pytest

from lte_pro_access.config import Config, load_config
from lte_pro_access.ssh_enable import enable_ssh_via_unifi
from test_relays import SAMPLE


def make_cfg(tmp_path: Path, unifi: dict) -> Config:
    (tmp_path / "key").write_text("PRIVATE")
    (tmp_path / "key.pub").write_text("ssh-ed25519 AAAA test\n")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**SAMPLE, "ssh_key_path": str(tmp_path / "key"), "unifi": unifi}))
    return load_config(path)


RESULT = object()


@pytest.fixture
def fake_pyunifiapi(monkeypatch):
    calls = []
    module = types.ModuleType("pyunifiapi")

    class ControllerConfig:
        def __init__(self, **kw):
            self.kw = kw

    class CloudConfig:
        def __init__(self, **kw):
            self.kw = kw

    async def enable_ssh(controller, device_mac, public_key):
        calls.append((controller, device_mac, public_key))
        return RESULT

    module.ControllerConfig = ControllerConfig
    module.CloudConfig = CloudConfig
    module.enable_ssh = enable_ssh
    monkeypatch.setitem(sys.modules, "pyunifiapi", module)
    return calls


async def test_local_controller(tmp_path, fake_pyunifiapi):
    cfg = make_cfg(tmp_path, {"host": "u.local", "username": "a", "password": "b"})
    assert await enable_ssh_via_unifi(cfg) is RESULT
    controller, mac, key = fake_pyunifiapi[0]
    assert type(controller).__name__ == "ControllerConfig"
    assert controller.kw == {"host": "u.local", "username": "a", "password": "b"}
    assert mac == cfg.lte_mac
    assert key == "ssh-ed25519 AAAA test"


async def test_cloud_controller_when_device_id_present(tmp_path, fake_pyunifiapi):
    cfg = make_cfg(tmp_path, {"username": "a", "password": "b", "device_id": "d"})
    await enable_ssh_via_unifi(cfg)
    assert type(fake_pyunifiapi[0][0]).__name__ == "CloudConfig"
