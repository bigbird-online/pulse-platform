"""Run the complete new Spec suite, independent of the old pytest environment."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def shard(text: str) -> tuple[int, int]:
    index, _, count = text.partition("/")
    if not (index.isdigit() and count.isdigit() and 0 <= int(index) < int(count)):
        raise argparse.ArgumentTypeError("shard is INDEX/COUNT with 0 <= INDEX < COUNT")
    return int(index), int(count)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--junitxml", type=Path)
parser.add_argument("--shard", type=shard, default=(0, 1),
                    help="run every COUNT-th collected test starting at INDEX; "
                         "the COUNT shards together are the complete suite")
args = parser.parse_args()
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
for variable in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
    os.environ.pop(variable, None)
sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
os.chdir(root)

import pytest  # noqa: E402


class Shard:
    """Keeps the collected tests whose position is INDEX modulo COUNT."""

    def __init__(self, index: int, count: int) -> None:
        self.index, self.count = index, count

    def pytest_collection_modifyitems(self, config, items):  # type: ignore[no-untyped-def]
        if self.count == 1:
            return
        mine = [position % self.count == self.index for position in range(len(items))]
        config.hook.pytest_deselected(items=[item for item, kept in zip(items, mine) if not kept])
        items[:] = [item for item, kept in zip(items, mine) if kept]


options = ["-c", "pytest-rewrite.ini", "--confcutdir=tests_rewrite", "tests_rewrite", "-q"]
if args.junitxml:
    options += ["--junitxml", str(args.junitxml.resolve())]
raise SystemExit(pytest.main(options, plugins=[Shard(*args.shard)]))
