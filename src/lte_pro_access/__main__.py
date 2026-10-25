import argparse
import asyncio
import logging
from pathlib import Path

from lte_pro_access.config import load_config
from lte_pro_access.service import create_supervisor, require_pyunifiapi
from lte_pro_access.supervisor import TICK_TIMEOUT_SECONDS, run_forever


async def amain(config_path: Path) -> None:
    try:
        require_pyunifiapi()
    except ModuleNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    cfg = load_config(config_path)
    supervisor = create_supervisor(cfg)
    try:
        await run_forever(supervisor, cfg.check_interval_seconds, TICK_TIMEOUT_SECONDS, asyncio.sleep)
    finally:
        supervisor.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="lte_pro_access")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
