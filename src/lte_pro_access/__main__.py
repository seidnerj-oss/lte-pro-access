import argparse
import asyncio
import logging
from pathlib import Path

from lte_pro_access.config import load_config
from lte_pro_access.service import create_supervisor, require_pyunifiapi
from lte_pro_access.socat_fetch import SocatFetchError, fetch_socat
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


def fetch_socat_command(destination: Path) -> None:
    try:
        path = fetch_socat(destination)
    except (SocatFetchError, OSError) as exc:
        raise SystemExit(f"fetch-socat failed: {exc}") from exc
    print(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lte_pro_access")
    parser.add_argument(
        "--config", type=Path, help="run the service with this config file (required without a command)"
    )
    commands = parser.add_subparsers(dest="command")
    fetch = commands.add_parser("fetch-socat", help="download the pinned MIPS socat for the device relay")
    fetch.add_argument("--destination", type=Path, required=True, help="where to write the socat binary")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "fetch-socat":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        fetch_socat_command(args.destination)
        return
    if args.config is None:
        parser.error("--config is required unless a command is given")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
