import json
from pathlib import Path

import pytest

from lte_pro_access.config import AtRelayConfig, Config, load_config
from lte_pro_access.relays import lte_at_relay_cmd, lte_qmi_relay_cmd, pi_qmi_relay_argv

SAMPLE = {
    "lte_host": "192.168.1.20",
    "lte_mac": "00:00:00:00:00:00",
    "pi_address": "192.168.1.10",
    "ssh_user": "root",
    "ssh_key_path": "/tmp/key",
    "state_dir": "/var/lib/lte-pro-access",
    "socat_device_binary": "/opt/lte/socat-mips",
    "socat_pi_binary": "/usr/bin/socat",
    "qmi_port": 18222,
    "at_relay": {"port": 18223, "device": "/dev/ttyUSB2", "idle_seconds": 60},
    "check_interval_seconds": 30,
    "unifi": {"host": "unifi.local"},
}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(SAMPLE))
    return load_config(path)


def test_at_relay_cmd(cfg):
    assert lte_at_relay_cmd(cfg, cfg.at_relay) == (
        "/tmp/socat -T 60 TCP-LISTEN:18223,reuseaddr,fork,max-children=1,range=192.168.1.10/32 "
        "OPEN:/dev/ttyUSB2,raw,echo=0"
    )


def test_qmi_relay_cmd(cfg):
    assert lte_qmi_relay_cmd(cfg) == (
        "/tmp/socat TCP-LISTEN:18222,reuseaddr,fork,range=192.168.1.10/32 ABSTRACT-CONNECT:qmi-proxy"
    )


def test_pi_relay_argv(cfg):
    assert pi_qmi_relay_argv(cfg) == [
        "/usr/bin/socat",
        "ABSTRACT-LISTEN:qmi-proxy,fork",
        "TCP:192.168.1.20:18222,keepalive,keepidle=30,keepintvl=10,keepcnt=3",
    ]


def test_load_config_types(cfg):
    assert cfg.ssh_key_path == Path("/tmp/key")
    assert cfg.state_dir == Path("/var/lib/lte-pro-access")
    assert cfg.socat_device_binary == Path("/opt/lte/socat-mips")
    assert cfg.socat_pi_binary == Path("/usr/bin/socat")
    assert cfg.unifi == {"host": "unifi.local"}
    assert cfg.at_relay == AtRelayConfig(port=18223, device="/dev/ttyUSB2", idle_seconds=60)


@pytest.mark.parametrize("key", sorted(SAMPLE))
def test_load_config_rejects_missing_key(tmp_path, key):
    data = {k: v for k, v in SAMPLE.items() if k != key}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=key):
        load_config(path)


def test_at_relay_cmd_uses_the_given_at_relay(cfg):
    other = AtRelayConfig(port=19000, device="/dev/ttyUSB3", idle_seconds=5)
    assert lte_at_relay_cmd(cfg, other) == (
        "/tmp/socat -T 5 TCP-LISTEN:19000,reuseaddr,fork,max-children=1,range=192.168.1.10/32 "
        "OPEN:/dev/ttyUSB3,raw,echo=0"
    )
