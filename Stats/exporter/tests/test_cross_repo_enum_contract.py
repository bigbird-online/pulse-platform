"""跨仓枚举守恒闸：harness 是写侧真源，Stats 的分类表必须对每个值表过态。

问题不是「某张表少了一个值」，是**harness 增删状态时 Stats 这边没有任何闸会
红**。已出过事故：`WI_OPEN_STATUSES` 少了 `blocked`，「在飞」少算 6 件，
hestia 那件「卡真人换电池」从未出现在大鸟眼前（2026-08-07 大鸟定）。改那一行
只解决那一次；这个文件解决这一类。

## 断言的是「表过态」不是「相等」

Stats 的四张表**刻意是子集**，各有理由（如 `HANDOFF_TERMINAL_STATES` 排除
`accepted`，因为「已接 ≠ 已办完」）。所以判据不是集合相等，是两条：

1. **无幻影**：读侧每个值必须 ∈ 写侧枚举。写错字或 harness 删了值 → 红。
2. **无沉默**：写侧每个值必须被**显式分类**——要么进了某张表，要么在下面
   `_EXEMPT_*` 里带理由登记。harness 新增一个状态而 Stats 没表态 → 红。

第 2 条是这个闸的核心价值。豁免必须写在代码里带理由，**不是静默漏掉**——
「没分类」与「想清楚了不分类」从行为上看一样，只有写下来才分得开。

## 写侧来源

旧 Harness 内核已退役，写侧枚举冻结在 `ARCHIVED_HARNESS_ENUMS`；关系不成立即红。

## 自证闸

`test_probe_itself_is_alive` 先证明这个闸没瞎跑：枚举条数低于合理下限即判
**探针失效**而非契约成立。

## 怎么手动跑

    cd Stats/exporter && python3 -m unittest tests.test_cross_repo_enum_contract -v

"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_stats.usage_sources import (  # noqa: E402
    HANDOFF_ACTION_STATES,
    HANDOFF_TERMINAL_STATES,
    WI_IN_FLIGHT_STATUSES,
    WI_OPEN_STATUSES,
)
from pulse_stats.live_contract import HUMAN_DECISION_KINDS  # noqa: E402

RELAY_HTTP_SERVER = Path(__file__).resolve().parents[3] / "Relay" / "src" / "http-server.ts"

#: 旧 Harness 内核已退役，其账本是只读归档；写侧枚举在此冻结为退役时的值
#: （harness.models.WorkItemStatus / AckState、harness.human_attention）。
ARCHIVED_HARNESS_ENUMS: dict[str, list[str]] = {
    "wi": ["draft", "ready", "in_progress", "awaiting_verification", "blocked",
           "completed", "failed", "cancelled", "superseded", "migrated"],
    "ack": ["pending", "accepted", "rejected", "timed_out", "archived", "withdrawn"],
    "human": ["irreversible_authorization", "main_only", "owner_decision"],
}

#: 自证下限。写侧真值 wi 七态 / ack 六态（2026-08-10 实测）；取到的比这还少，
#: 说明取的不是那张枚举，判探针失效。
_MIN_WI_STATES = 5
_MIN_ACK_STATES = 4

# --- 显式豁免：写侧有、但 Stats 的表刻意不收的值 ---------------------------
# 登记在这里 = 「想清楚了不分类」。空着 = 「没想过」。两者行为一样，只有写下来
# 才分得开。新增豁免必须带理由，不接受「暂时先放这」。

_EXEMPT_WI = {
    "completed": "终态。四张表都只描述「还摊在台面上的活」，办完的按定义不在飞。",
    "failed": "终态，同上。失败的活由 escalation 流负责，不由在飞表承载。",
    "cancelled": "终态。明确停止的事务不再是待行动或在飞工作。",
    "superseded": "终态。被后续 WI 替代的旧事务不应重复进入当前待行动表。",
    "migrated": "终态。未决义务由 successor 承接，原 WI 不再作为在飞项重复计算。",
}

_EXEMPT_ACK = {
    "withdrawn": (
        "发起方自己撤回。既不是「等人动手」(action) 也不是「被处置掉」"
        "(terminal 那三个都含处置动作)——它是单子压根没成立过。"
        "计入任一张都会让统计口径失真。"
    ),
}


def _fetch_harness_enums() -> dict[str, list[str]]:
    """归档账本的写侧枚举。"""
    return {key: list(values) for key, values in ARCHIVED_HARNESS_ENUMS.items()}


class CrossRepoEnumContractTests(unittest.TestCase):
    """写侧 harness 枚举 ↔ 读侧 Stats 分类表的守恒闸。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.enums: dict[str, list[str]] | None
        cls.fetch_error: str | None
        try:
            cls.enums = _fetch_harness_enums()
            cls.fetch_error = None
        except Exception as exc:  # noqa: BLE001 - 任何取值失败都归 fail-open
            cls.enums = None
            cls.fetch_error = f"{type(exc).__name__}: {exc}"

    def _enums_or_skip(self) -> dict[str, list[str]]:
        """fail-open 闸门。取不到写侧值时跳过并说清原因，不假装通过。"""
        if self.enums is None:
            self.skipTest(
                "SKIP≠PASS：读不到 harness 权威枚举，本条**未做判定**（不是通过）。"
                f"原因：{self.fetch_error}"
            )
        return self.enums

    def test_probe_itself_is_alive(self) -> None:
        """自证闸：取到的枚举得像那么回事，否则判探针失效而非契约成立。"""
        enums = self._enums_or_skip()
        wi, ack = enums.get("wi", []), enums.get("ack", [])
        self.assertGreaterEqual(
            len(wi), _MIN_WI_STATES,
            f"只取到 {len(wi)} 个 wi 状态 {wi}，低于下限 {_MIN_WI_STATES}——"
            "判探针失效（取错模块/枚举被搬走），不是契约成立。",
        )
        self.assertGreaterEqual(
            len(ack), _MIN_ACK_STATES,
            f"只取到 {len(ack)} 个 ack 状态 {ack}，低于下限 {_MIN_ACK_STATES}——"
            "判探针失效，不是契约成立。",
        )
        self.assertTrue(
            all(isinstance(v, str) and v for v in wi + ack),
            f"枚举里有空值或非字符串，取值路径可疑：wi={wi} ack={ack}",
        )

    def test_read_side_values_all_exist_upstream(self) -> None:
        """无幻影：Stats 四张表里的每个值都必须是 harness 真有的状态。

        写错字、或 harness 删了某个值而 Stats 还在用 → 红。这类 bug 的表现是
        「判据永远不命中」，静默且极难从面板上看出来。
        """
        enums = self._enums_or_skip()
        wi_upstream, ack_upstream = set(enums["wi"]), set(enums["ack"])
        phantom: list[str] = []
        for name, table, upstream in (
            ("WI_OPEN_STATUSES", WI_OPEN_STATUSES, wi_upstream),
            ("WI_IN_FLIGHT_STATUSES", WI_IN_FLIGHT_STATUSES, wi_upstream),
            ("HANDOFF_TERMINAL_STATES", HANDOFF_TERMINAL_STATES, ack_upstream),
            ("HANDOFF_ACTION_STATES", HANDOFF_ACTION_STATES, ack_upstream),
        ):
            for value in table:
                if value not in upstream:
                    phantom.append(
                        f"{name} 里的 {value!r} 在 harness 枚举里不存在"
                        f"（上游只有 {sorted(upstream)}）"
                    )
        self.assertEqual(
            phantom, [],
            "读侧有幻影状态——这些判据永远不会命中：\n  " + "\n  ".join(phantom),
        )

    def test_every_upstream_value_is_classified(self) -> None:
        """无沉默：harness 每个状态都必须被 Stats 显式表过态。

        **这是本闸的核心。** harness 新增一个状态、Stats 没人管 → 该状态在面板
        上永久隐身，且没有任何现象提示你去看。要么把它加进某张表，要么在
        `_EXEMPT_*` 里写下不收的理由——不接受第三种「就那么放着」。
        """
        enums = self._enums_or_skip()
        classified_wi = set(WI_OPEN_STATUSES) | set(WI_IN_FLIGHT_STATUSES)
        classified_ack = set(HANDOFF_TERMINAL_STATES) | set(HANDOFF_ACTION_STATES)

        unclassified: list[str] = []
        for value in enums["wi"]:
            if value not in classified_wi and value not in _EXEMPT_WI:
                unclassified.append(
                    f"harness WorkItemStatus.{value} 没有归属："
                    "不在 WI_OPEN_STATUSES / WI_IN_FLIGHT_STATUSES，也没登记豁免"
                )
        for value in enums["ack"]:
            if value not in classified_ack and value not in _EXEMPT_ACK:
                unclassified.append(
                    f"harness AckState.{value} 没有归属："
                    "不在 HANDOFF_TERMINAL_STATES / HANDOFF_ACTION_STATES，"
                    "也没登记豁免"
                )
        self.assertEqual(
            unclassified, [],
            "写侧新增/改名了状态而 Stats 没表态——这些值会在面板上静默隐身。\n"
            "修法二选一：加进对应的分类表，或在 _EXEMPT_WI/_EXEMPT_ACK 里"
            "写明不收的理由。\n  " + "\n  ".join(unclassified),
        )

    def test_human_attention_decision_kinds_match_harness(self) -> None:
        """Harness write receipt and Stats act tier consume the same tiny enum."""
        enums = self._enums_or_skip()
        self.assertEqual(
            set(enums.get("human", [])),
            set(HUMAN_DECISION_KINDS),
            "human-attention contract drift: Stats could hide or invent owner work",
        )

    def test_relay_accepts_exactly_the_stats_human_decision_kinds(self) -> None:
        """Relay's transport gate cannot invent or omit a reviewed fact."""
        source = RELAY_HTTP_SERVER.read_text(encoding="utf-8")
        block = re.search(
            r"const HUMAN_DECISION_KINDS = new Set<HumanDecisionKind>\(\[(.*?)\]\);",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(block, "Relay human-attention enum is not machine-readable")
        relay_kinds = set(re.findall(r"'([^']+)'", block.group(1)))
        self.assertEqual(
            relay_kinds,
            set(HUMAN_DECISION_KINDS),
            "Relay/Stats human-attention contract drift",
        )

    def test_exemptions_are_not_stale(self) -> None:
        """豁免清单本身也会腐烂：登记了但上游已经没有的值，要清掉。

        没有这条，豁免表会变成只进不出的坟场，久而久之没人敢动。
        """
        enums = self._enums_or_skip()
        wi_upstream, ack_upstream = set(enums["wi"]), set(enums["ack"])
        stale = [
            f"_EXEMPT_WI 里的 {v!r} 上游已不存在" for v in _EXEMPT_WI
            if v not in wi_upstream
        ] + [
            f"_EXEMPT_ACK 里的 {v!r} 上游已不存在" for v in _EXEMPT_ACK
            if v not in ack_upstream
        ]
        self.assertEqual(
            stale, [], "豁免清单过期，清掉这些条目：\n  " + "\n  ".join(stale)
        )


if __name__ == "__main__":
    unittest.main()
