import asyncio
import importlib.util

from lte_pro_access.config import Config
from lte_pro_access.device import AsyncSshDevice
from lte_pro_access.ssh_enable import enable_ssh_via_unifi
from lte_pro_access.supervisor import LocalProcess, Supervisor


async def spawn_local(argv: list[str]) -> LocalProcess:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


# enable_ssh reaches the device through pyunifiapi's WebRTC debug terminal, which needs its cloud extra.
REQUIRED_MODULES = ("pyunifiapi", "libdatachannel")


def require_pyunifiapi() -> None:
    """Fail at startup, not on the first LTE reboot, when the lazily imported pyunifiapi[cloud] is missing."""
    for name in REQUIRED_MODULES:
        if importlib.util.find_spec(name) is None:
            raise ModuleNotFoundError(
                f"{name} is not installed, so SSH cannot be re-enabled after an LTE reboot. Install "
                "pyunifiapi[cloud] (see the README Dependencies section) and restart.",
                name=name,
            )


def create_supervisor(cfg: Config) -> Supervisor:
    """A Supervisor wired to the real device: SSH via asyncssh, SSH enabling via the UniFi controller, local
    subprocesses and asyncio.sleep. Raises ModuleNotFoundError when pyunifiapi is missing."""
    require_pyunifiapi()
    return Supervisor(cfg, AsyncSshDevice(cfg), enable_ssh_via_unifi, spawn_local, asyncio.sleep)
