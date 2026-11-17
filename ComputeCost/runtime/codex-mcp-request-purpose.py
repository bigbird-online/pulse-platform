#!/usr/bin/env python3
"""Structured request-purpose MCP candidate; enrollment is a separate operation."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pulse_compute_cost.mcp_request_purpose import main

if __name__ == "__main__":
    raise SystemExit(main())
