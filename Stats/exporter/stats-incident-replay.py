#!/usr/bin/env python3
"""A6 硬门：拿 2026-07-25 两场真实事故的语料回放，验分档是否命中。

SPEC `Stats/specs/pulse-stats-v3-alert-triage.md` §1 验收 A6。

上一轮 Slice 1 判 GO 栽在「五项判据全在测 App 自己健不健康，
没有任何一条在测生产真出事时它报不报」。这个脚本就是那条缺失的判据：
**用真实生产语料回放，不构造数据**
（[[reasoning-mistaken-for-verification-declared-done-wrongly]]）。

跑法：
    /usr/bin/python3 Stats/exporter/stats-incident-replay.py

退出码 0 = 两场事故都被判进 act；非 0 = 有事故漏判，不许判 GO。
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

EXPORTER = Path("/Users/owner/Coevolution/Bigbird/PULSE/Stats/exporter")
sys.path.insert(0, str(EXPORTER))

EVENTS = Path("/Users/owner/Coevolution/.harness/events/escalation_event-2026-07.jsonl")

# 事故窗口：2026-07-25 全天（UTC）。当天 104 条事件。
WINDOW_LO = _dt.datetime(2026, 7, 25, 0, 0, tzinfo=_dt.timezone.utc)
WINDOW_HI = _dt.datetime(2026, 7, 26, 0, 0, tzinfo=_dt.timezone.utc)

# 判据基准时刻取事故当天 22:00 UTC——即事故正在发生、大鸟正在等系统报警的那一刻。
# 不能取「现在」：那样 48h 时效窗会把整场事故判成陈账，回归就成了空跑。
AS_OF = _dt.datetime(2026, 7, 25, 22, 0, tzinfo=_dt.timezone.utc)


class Incident:
    """一场真实事故 + 它必须被判成什么。"""

    def __init__(self, name: str, label: str, min_count: int, expect_tier: str, note: str):
        self.name = name
        self.label = label
        self.min_count = min_count
        self.expect_tier = expect_tier
        self.note = note


INCIDENTS = [
    Incident(
        name="Amanda 日巡连续失败",
        label="autonomous_wrap_missing",
        min_count=6,
        expect_tier="act",
        note="跑完报成功但零产出、S1 台账停更；当天飞书已发过通知，Stats 却一无所知",
    ),
    Incident(
        name="监控探针自身塌掉",
        label="ack_probe_down",
        min_count=2,
        expect_tier="act",
        note="探针死了等于协作面没人盯——别的告警不响不代表没事",
    ),
]


def load_incident_events() -> list[dict]:
    out = []
    with EVENTS.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                when = _dt.datetime.fromisoformat(str(raw["occurred_at"]).replace("Z", "+00:00"))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if WINDOW_LO <= when < WINDOW_HI:
                out.append(raw)
    return out


def build_groups(events: list[dict]):
    """按 exporter 同一套规则收敛。

    分组逻辑内嵌在 `_collect_alerts` 里（没有可复用的公开函数），
    所以这里复用它用到的同一批私有件——测试贴着实现走，
    好过为了可测性去改生产代码的结构。若将来那段被抽成公开函数，
    这里换成直接调用即可。
    """
    from pulse_stats import live_sources as ls
    from pulse_stats.live_contract import ALERT_TEMPLATES

    groups: dict[tuple[str, str], object] = {}
    for raw in events:
        try:
            occurred = ls._parse_date(raw["occurred_at"])
            severity = ls._ALERT_SEVERITY.get(str(raw.get("severity", "minor")), "info")
            match_label = str(raw.get("match_label", ""))
            capability_id, title = ALERT_TEMPLATES[match_label]
        except (KeyError, TypeError, ValueError):
            continue
        message = raw.get("message")
        subject = ls._alert_subject(message, raw.get("agent_id"))
        key = (match_label, subject)
        group = groups.get(key)
        if group is None:
            groups[key] = ls._AlertGroup(
                match_label=match_label, subject=subject, capability_id=capability_id,
                title=title, severity=severity, first_seen=occurred, last_seen=occurred,
                message=message if isinstance(message, str) else None, occurrence_count=1,
            )
        else:
            group.observe(occurred, severity, message)
    return list(groups.values())


def main() -> int:
    events = load_incident_events()
    if not events:
        print("FAIL: 事故窗口内读不到任何事件，语料路径或格式变了", file=sys.stderr)
        return 2

    print(f"事故语料：{WINDOW_LO:%Y-%m-%d} 当天 {len(events)} 条原始事件")
    print(f"判据基准：{AS_OF:%Y-%m-%d %H:%M}Z（事故进行中的那一刻）\n")

    groups = build_groups(events)
    by_label: dict[str, object] = {}
    for grp in groups:
        # 同 label 多对象时，取次数最多的那个作代表
        prev = by_label.get(grp.match_label)
        if prev is None or grp.occurrence_count > prev.occurrence_count:
            by_label[grp.match_label] = grp

    print(f"收敛后 {len(groups)} 件事\n")

    failures = []
    for inc in INCIDENTS:
        grp = by_label.get(inc.label)
        if grp is None:
            failures.append(f"{inc.name}：语料里没有 {inc.label}，事故根本没进采集")
            print(f"✗ {inc.name}\n    {inc.label} 未出现在收敛结果里")
            continue

        tier = grp.tier(AS_OF)
        ok_tier = tier == inc.expect_tier
        ok_count = grp.occurrence_count >= inc.min_count
        mark = "✓" if (ok_tier and ok_count) else "✗"
        print(f"{mark} {inc.name}")
        print(f"    {inc.note}")
        print(f"    label={inc.label} 次数={grp.occurrence_count} tier={tier}（期望 {inc.expect_tier}）")

        if not ok_tier:
            failures.append(
                f"{inc.name}：判成 {tier}，期望 {inc.expect_tier}"
                f"——事故当天这条不会打断大鸟"
            )
        if not ok_count:
            failures.append(
                f"{inc.name}：只收敛到 {grp.occurrence_count} 次，期望 ≥{inc.min_count}"
            )
        print()

    if failures:
        print("=" * 60)
        print("A6 未过 —— 不许判 GO：", file=sys.stderr)
        for f in failures:
            print(f"  · {f}", file=sys.stderr)
        return 1

    print("=" * 60)
    print("A6 通过：两场真实事故在改后的分档下都会打断大鸟。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
