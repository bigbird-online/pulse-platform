"""SPEC ↔ exporter 能力状态一致性闸。

2026-08-02 大鸟问「覆盖未知是什么意思」，查下去发现 baseline SPEC §143 判
`s1.request_trace_capture` 为 `active_validated`（取证：07-15 canary、
`relay-request-trace.ts`、07-16~22 live 事件计数），exporter 却把它写死成
`active_partial` + freshness `unknown`，理由 "Trace source is not yet
adapted"——那句话在 07-15 之后就不成立了。**单这一项把 overall coverage
从 partial 短路成 unknown**（`producer.reduce_coverage`：任一项 freshness
unknown 即整体 unknown），于是菜单栏常年挂着「覆盖未知」。

真缺陷不是那一行值写错，是**19 项能力的状态在 SPEC 里是一张表、在代码里是
散落的硬编码字符串，两边对不上时没有任何闸会红**。改完那一行只解决这一次；
这个文件解决这一类：SPEC 表是真源，exporter 实跑产出必须与它对齐，对不上
当场红。

判据只管 `implementation_status`，不管 freshness——freshness 是运行时事实
（源当天有没有数据），本来就该浮动；`implementation_status` 是契约声明，
浮动就是 drift。

SPEC 真源：`specs/pulse-monitoring-baseline.md` §143-160。
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SPEC_PATH = (
    Path(__file__).resolve().parents[3]
    / "specs" / "pulse-monitoring-baseline.md"
)

#: `| `id` | `status` | 说明 | 证据 |` —— 只认反引号包裹的前两列，
#: 表头/分隔行天然不匹配。
ROW = re.compile(r"^\|\s*`([a-z0-9._]+)`\s*\|\s*`([a-z_]+)`\s*\|")

VALID_STATUSES = {
    "active_validated", "active_partial", "retired", "not_implemented",
}


def parse_spec_capabilities() -> dict[str, str]:
    """从 SPEC 的能力表抠出 {capability_id: implementation_status}。"""
    table: dict[str, str] = {}
    for line in SPEC_PATH.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line.strip())
        if match:
            table[match.group(1)] = match.group(2)
    return table


class SpecCapabilityDriftTests(unittest.TestCase):
    def test_spec_table_is_parseable(self) -> None:
        """闸自身先自证：SPEC 读得到、表抠得出、状态值合法。

        没有这条,SPEC 改格式或路径变了会让下面两条**静默全过**——
        「绿得没意义比红更危险」。
        """
        self.assertTrue(SPEC_PATH.is_file(), f"SPEC 不在 {SPEC_PATH}")
        table = parse_spec_capabilities()
        self.assertGreaterEqual(
            len(table), 15, f"只抠出 {len(table)} 条,表格式可能变了"
        )
        for capability_id, status in table.items():
            self.assertIn(
                status, VALID_STATUSES,
                f"{capability_id} 的状态 {status!r} 不在 SPEC §317 枚举里",
            )

    def test_exporter_status_matches_spec(self) -> None:
        """exporter 实跑产出的每一项能力,状态必须与 SPEC 表一致。

        跑的是**生产那条接线本身**(`LiveSnapshotService.default()`),不是
        fixture——fixture 绿证明不了真源对齐(0731 教训)。只把事故状态文件换到
        临时目录:测试不写生产 incident state。读不到生产源的机器上会 skip 而不是假绿。
        """
        from dataclasses import replace
        import json
        import subprocess
        import tempfile
        from pulse_stats.live import LiveSnapshotError, LiveSnapshotService
        from pulse_stats.state import StateError

        spec = parse_spec_capabilities()
        with tempfile.TemporaryDirectory() as directory:
            service = LiveSnapshotService.default()
            service.paths = replace(service.paths, state=Path(directory) / "incident-state-v1.json")
            try:
                snapshot = json.loads(service.render_json())
            except (LiveSnapshotError, StateError, OSError, ValueError, subprocess.SubprocessError) as exc:
                self.skipTest(f"生产快照跑不起来,本条不判: {type(exc).__name__}")

        drift: list[str] = []
        for item in snapshot.get("capabilities", []):
            capability_id = str(item.get("id"))
            actual = str(item.get("implementation_status"))
            expected = spec.get(capability_id)
            if expected is None:
                # SPEC 没列的能力:代码先行,SPEC 欠一条。也是 drift。
                drift.append(f"{capability_id}: 代码有 {actual!r},SPEC 表没这一项")
            elif actual != expected:
                drift.append(
                    f"{capability_id}: SPEC 说 {expected!r},代码给 {actual!r}"
                )
        self.assertEqual(
            drift, [],
            "SPEC 与 exporter 能力状态不一致——改代码或改 SPEC,不要放着:\n  "
            + "\n  ".join(drift),
        )


if __name__ == "__main__":
    unittest.main()
