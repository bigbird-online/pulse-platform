#!/usr/bin/env python3
"""pulse-stats: read-only Agent query over the data PulseStats shows (pulse_stats_query/v1)."""

from pathlib import Path
import sys

# Run with `python -I`; the installed release carries pulse_stats beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ComputeCost"))

from pulse_stats.query_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
