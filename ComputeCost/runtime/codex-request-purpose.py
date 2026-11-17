#!/usr/bin/env python3
"""Native Hook and per-request purpose declaration entrypoint."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pulse_compute_cost.native_request_purpose import main
if __name__ == '__main__':
    raise SystemExit(main())
