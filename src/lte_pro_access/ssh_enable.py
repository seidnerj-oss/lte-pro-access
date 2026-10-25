from pathlib import Path
from typing import Protocol

from lte_pro_access.config import Config


class EnableResult(Protocol):
    """Structural view of pyunifiapi's SSHEnableResult: what enable_ssh changed on the device."""

    key_added: bool
    dropbear_started: bool


async def enable_ssh_via_unifi(cfg: Config) -> EnableResult:
    """Production enable_ssh: asks the UniFi controller (pyunifiapi, WebRTC) to start dropbear on the device.

    pyunifiapi is imported lazily because it is not installable yet: v0.2.0 is tagged on GitHub but its PyPI
    publish failed, so it is not a dependency. A `device_id` key in cfg.unifi selects a cloud controller;
    otherwise it is a local one.
    """
    from pyunifiapi import CloudConfig, ControllerConfig, enable_ssh

    controller = CloudConfig(**cfg.unifi) if "device_id" in cfg.unifi else ControllerConfig(**cfg.unifi)
    key_path = cfg.ssh_key_path
    public_key = Path(f"{key_path}.pub").read_text().strip()
    return await enable_ssh(controller, cfg.lte_mac, public_key)
