import dataclasses
import json
from pathlib import Path

import pytest

from lte_pro_access.config import load_config
from test_relays import SAMPLE


def _write(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    return path


def test_rejects_unknown_key(tmp_path):
    with pytest.raises(ValueError, match="qmi_prot"):
        load_config(_write(tmp_path, {**SAMPLE, "qmi_prot": 1}))


@pytest.mark.parametrize("value", [[], "text", 3, None])
def test_rejects_non_object_top_level(tmp_path, value):
    with pytest.raises(ValueError, match="JSON object"):
        load_config(_write(tmp_path, value))


@pytest.mark.parametrize("key", ["qmi_port", "check_interval_seconds"])
@pytest.mark.parametrize("bad", ["18222", 1.5, True, None])
def test_rejects_wrong_type_for_int_fields(tmp_path, key, bad):
    with pytest.raises(ValueError, match=key):
        load_config(_write(tmp_path, {**SAMPLE, key: bad}))


@pytest.mark.parametrize(
    "key",
    [
        "lte_host",
        "lte_mac",
        "pi_address",
        "ssh_user",
        "ssh_key_path",
        "state_dir",
        "socat_device_binary",
        "socat_pi_binary",
    ],
)
def test_rejects_wrong_type_for_string_fields(tmp_path, key):
    with pytest.raises(ValueError, match=key):
        load_config(_write(tmp_path, {**SAMPLE, key: 5}))


def test_rejects_non_object_unifi(tmp_path):
    with pytest.raises(ValueError, match="unifi"):
        load_config(_write(tmp_path, {**SAMPLE, "unifi": "x"}))


def test_state_dir_is_required(tmp_path):
    data = {k: v for k, v in SAMPLE.items() if k != "state_dir"}
    with pytest.raises(ValueError, match="state_dir"):
        load_config(_write(tmp_path, data))


def test_example_config_loads():
    example = Path(__file__).resolve().parent.parent / "config.example.json"
    cfg = load_config(example)
    assert cfg.state_dir == Path("/home/pi/.local/state/lte-pro-access")
    assert cfg.pi_address == "192.168.1.10"
    assert cfg.at_relay is not None


def test_at_relay_null_disables_it(tmp_path):
    assert load_config(_write(tmp_path, {**SAMPLE, "at_relay": None})).at_relay is None


def test_at_relay_is_required_even_when_unused(tmp_path):
    data = {k: v for k, v in SAMPLE.items() if k != "at_relay"}
    with pytest.raises(ValueError, match="at_relay"):
        load_config(_write(tmp_path, data))


@pytest.mark.parametrize("bad", ["x", 1, [], True])
def test_rejects_non_object_at_relay(tmp_path, bad):
    with pytest.raises(ValueError, match="at_relay must be an object or null"):
        load_config(_write(tmp_path, {**SAMPLE, "at_relay": bad}))


@pytest.mark.parametrize("key", ["port", "device", "idle_seconds"])
def test_at_relay_keys_are_required(tmp_path, key):
    at_relay = {k: v for k, v in SAMPLE["at_relay"].items() if k != key}
    with pytest.raises(ValueError, match=f"at_relay is missing required keys: {key}"):
        load_config(_write(tmp_path, {**SAMPLE, "at_relay": at_relay}))


def test_rejects_unknown_at_relay_key(tmp_path):
    with pytest.raises(ValueError, match="at_relay has unknown keys: prot"):
        load_config(_write(tmp_path, {**SAMPLE, "at_relay": {**SAMPLE["at_relay"], "prot": 1}}))


@pytest.mark.parametrize("key", ["port", "idle_seconds"])
@pytest.mark.parametrize("bad", ["18223", 1.5, True, None])
def test_rejects_wrong_type_for_at_relay_int_fields(tmp_path, key, bad):
    with pytest.raises(ValueError, match=f"at_relay: key {key} must be an integer"):
        load_config(_write(tmp_path, {**SAMPLE, "at_relay": {**SAMPLE["at_relay"], key: bad}}))


def test_rejects_wrong_type_for_at_relay_device(tmp_path):
    with pytest.raises(ValueError, match="at_relay: key device must be a string"):
        load_config(_write(tmp_path, {**SAMPLE, "at_relay": {**SAMPLE["at_relay"], "device": 5}}))


def test_load_config_rejects_the_same_socat_binary_for_device_and_pi(tmp_path):
    data = {**SAMPLE, "socat_device_binary": "/usr/bin/socat", "socat_pi_binary": "/usr/bin/socat"}
    with pytest.raises(ValueError, match="socat_device_binary and socat_pi_binary must differ"):
        load_config(_write(tmp_path, data))


def test_config_construction_rejects_the_same_socat_binary_for_device_and_pi(tmp_path):
    cfg = load_config(_write(tmp_path, SAMPLE))
    with pytest.raises(ValueError, match="must differ"):
        dataclasses.replace(cfg, socat_pi_binary=cfg.socat_device_binary)


def test_config_accepts_string_paths_and_stores_them_as_paths(tmp_path):
    cfg = load_config(_write(tmp_path, SAMPLE))
    built = dataclasses.replace(cfg, ssh_key_path="/tmp/key", state_dir="/var/state", socat_device_binary="/a/socat")
    assert built.ssh_key_path == Path("/tmp/key")
    assert built.state_dir == Path("/var/state")
    assert built.socat_device_binary == Path("/a/socat")
    assert all(isinstance(getattr(built, n), Path) for n in ("ssh_key_path", "state_dir", "socat_device_binary"))
    assert isinstance(built.socat_pi_binary, Path)


def test_config_rejects_the_same_socat_binary_given_as_a_string_and_a_path(tmp_path):
    cfg = load_config(_write(tmp_path, SAMPLE))
    with pytest.raises(ValueError, match="must differ"):
        dataclasses.replace(cfg, socat_device_binary="/usr/bin/socat", socat_pi_binary=Path("/usr/bin/socat"))
