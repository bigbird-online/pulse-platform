"""PULSE 计量链路：节点追尾、SQLite 事实库、节点到汇总库的传输、视图与发布物。

表结构见同目录 schema.sql；节点库 ~/.local/state/pulse/meter/node.sqlite，
汇总库 ~/.local/state/pulse/meter/central.sqlite。
"""

from __future__ import annotations

from pathlib import Path

SCHEMA_VERSION = "1"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")
