#!/usr/bin/env python3
"""Read-only S3 consumer bridge for real weekly patrol and Duke UAT."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pulse_compute_cost.consumers import (
    ConsumerError,
    consume_duke_recommendation,
    consume_weekly_query,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="s3-consume.py")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("weekly", "duke"):
        item = sub.add_parser(name)
        item.add_argument("--input", type=Path, required=True)
    return parser


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsumerError(f"cannot read consumer artifact {path}") from exc


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source = _read(args.input)
        if args.command == "weekly":
            result = consume_weekly_query(source)
        else:
            result = consume_duke_recommendation(source)
    except ConsumerError as exc:
        print(f"s3-consume: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
