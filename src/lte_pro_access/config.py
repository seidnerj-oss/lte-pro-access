import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class AtRelayConfig:
    """The device-side relay to the modem's AT serial port."""

    port: int
    device: str
    idle_seconds: int


@dataclass(frozen=True)
class Config:
    lte_host: str
    lte_mac: str
    pi_address: str
    ssh_user: str
    ssh_key_path: Path
    state_dir: Path
    socat_device_binary: Path
    socat_pi_binary: Path
    qmi_port: int
    at_relay: AtRelayConfig | None
    check_interval_seconds: int
    unifi: dict

    def __post_init__(self) -> None:
        for name in _PATH_FIELDS:
            object.__setattr__(self, name, Path(getattr(self, name)))
        if self.socat_device_binary == self.socat_pi_binary:
            raise ValueError(
                f"socat_device_binary and socat_pi_binary must differ (both are {self.socat_device_binary}): the "
                "device and the Pi are different architectures"
            )


_PATH_FIELDS = {"ssh_key_path", "state_dir", "socat_device_binary", "socat_pi_binary"}
_STR_FIELDS = {"lte_host", "lte_mac", "pi_address", "ssh_user"} | _PATH_FIELDS
_INT_FIELDS = {"qmi_port", "check_interval_seconds"}
_AT_RELAY_STR_FIELDS = {"device"}
_AT_RELAY_INT_FIELDS = {"port", "idle_seconds"}


def _check_keys(where: str, data: dict, names: list[str]) -> None:
    missing = [n for n in names if n not in data]
    if missing:
        raise ValueError(f"{where} is missing required keys: {', '.join(missing)}")
    unknown = sorted(set(data) - set(names))
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(unknown)}")


def _check_type(where: str, name: str, value: object, str_fields: set[str], int_fields: set[str]) -> None:
    if name in str_fields:
        ok, expected = isinstance(value, str), "a string"
    elif name in int_fields:
        ok, expected = isinstance(value, int) and not isinstance(value, bool), "an integer"
    else:
        ok, expected = isinstance(value, dict), "an object"
    if not ok:
        raise ValueError(f"{where}: key {name} must be {expected}, got {type(value).__name__}")


def _load_at_relay(path: Path, value: object) -> AtRelayConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"config {path}: key at_relay must be an object or null, got {type(value).__name__}")
    where = f"config {path} at_relay"
    names = [f.name for f in fields(AtRelayConfig)]
    _check_keys(where, value, names)
    for n in names:
        _check_type(where, n, value[n], _AT_RELAY_STR_FIELDS, _AT_RELAY_INT_FIELDS)
    return AtRelayConfig(**{n: value[n] for n in names})


def load_config(path: Path) -> Config:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"config {path} must be a JSON object, got {type(data).__name__}")
    where = f"config {path}"
    names = [f.name for f in fields(Config)]
    _check_keys(where, data, names)
    values: dict[str, object] = {}
    for n in names:
        if n == "at_relay":
            values[n] = _load_at_relay(path, data[n])
            continue
        _check_type(where, n, data[n], _STR_FIELDS, _INT_FIELDS)
        values[n] = Path(data[n]) if n in _PATH_FIELDS else data[n]
    return Config(**values)
