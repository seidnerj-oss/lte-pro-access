"""Keeps SSH to a UniFi LTE Backup Pro enabled and supervises socat relays to it.

Embedding: build a Config, call create_supervisor(cfg), run run_forever(supervisor, cfg.check_interval_seconds,
TICK_TIMEOUT_SECONDS, asyncio.sleep) as a task, read supervisor.relays_up for health and call supervisor.close()
when done.
"""

from lte_pro_access.config import AtRelayConfig, Config
from lte_pro_access.service import create_supervisor
from lte_pro_access.supervisor import TICK_TIMEOUT_SECONDS, Supervisor, TickResult, run_forever

__all__ = [
    "TICK_TIMEOUT_SECONDS",
    "AtRelayConfig",
    "Config",
    "Supervisor",
    "TickResult",
    "create_supervisor",
    "run_forever",
]
