from __future__ import annotations

import argparse
from pathlib import Path

from .config import ConfigError
from .server import run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pisa-experiment-runner",
        description="Build and run local PISA experiments through a standalone web interface.",
    )
    parser.add_argument("--config", type=Path, default=Path("config/experiment_runner.yaml"))
    parser.add_argument("--local-config", type=Path, default=Path("config/experiment_runner.local.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run_server(
            config=args.config,
            local_config=args.local_config,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
        )
    except ConfigError as exc:
        parser.error(str(exc))
    return 0
