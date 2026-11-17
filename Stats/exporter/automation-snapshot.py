#!/usr/bin/env python3
"""Read only: use already installed Stats peer identities, never run a cron job."""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
from pulse_stats.automation_source import gather, read_site, SITES


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', choices=SITES)
    args=parser.parse_args()
    value=read_site(Path.home(),args.site,datetime.now(timezone.utc).isoformat()) if args.site else gather(Path.home())
    print(json.dumps(value,ensure_ascii=False,separators=(',',':')))
