from __future__ import annotations

import argparse
import json
from typing import Any


def setup_cli_parser(subparser: argparse.ArgumentParser) -> None:
    subcommands = subparser.add_subparsers(dest="hermes_otel_action")
    subcommands.add_parser("status", help="Show plugin runtime status")
    subcommands.add_parser("show-config", help="Print resolved plugin config")
    subcommands.add_parser("test-export", help="Emit one synthetic span/metric/log and flush")


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))

