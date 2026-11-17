#!/usr/bin/env python3
"""
test_anchors.py — 锚点校验器自测

一个查漂移的工具自己没有测试,是自相矛盾的。这里的用例直接对应
2026-07-25 PULSE Relay 采证中踩到的真实形态,每条注明来源。

跑法: python3 src/test_anchors.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from anchors import (  # noqa: E402
    Anchor, build_test_index, parse_anchors, verify,
    VERDICT_HOLDS, VERDICT_BROKEN, VERDICT_UNVERIFIABLE, VERDICT_ERROR,
    _SOURCE_CACHE, _EVER_CACHE,
)
from infer import infer_anchors  # noqa: E402
from scan import count_claims, scan, summarize  # noqa: E402


class RepoFixture:
    """造一个最小仓库: src/ + tests/ + specs/。"""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "specs").mkdir()
        _SOURCE_CACHE.clear()
        _EVER_CACHE.clear()

    def src(self, name: str, body: str) -> None:
        (self.root / "src" / name).write_text(body, encoding="utf-8")
        _SOURCE_CACHE.clear()

    def test(self, name: str, body: str) -> None:
        (self.root / "tests" / name).write_text(body, encoding="utf-8")

    def spec(self, name: str, body: str) -> Path:
        p = self.root / "specs" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def index(self) -> dict:
        return build_test_index(self.root, ["tests/**/*.ts"])

    def close(self) -> None:
        self._tmp.cleanup()


class TestTestAnchor(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_case_name_present_holds(self) -> None:
        self.fx.test("a.test.ts", "it('records usage to JSONL', () => {});")
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="tests/a.test.ts", claim="records usage to JSONL")
        verify(a, self.fx.root, self.fx.index())
        self.assertEqual(a.verdict, VERDICT_HOLDS)

    def test_missing_file_is_drift(self) -> None:
        """来源: spec 引用 tests/feishu-ask-multi.test.ts,该文件已被 b346287 删除。"""
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="tests/gone.test.ts", claim="whatever")
        verify(a, self.fx.root, self.fx.index())
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("不存在", a.detail)

    def test_file_exists_but_case_absent_is_drift(self) -> None:
        """文件在、测试绿,但没有这条用例 —— 只查文件存在会漏掉。"""
        self.fx.test("a.test.ts", "it('unrelated thing [Unit Test-009]', () => {});")
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="tests/a.test.ts", claim="[Unit Test-002]")
        verify(a, self.fx.root, self.fx.index())
        self.assertEqual(a.verdict, VERDICT_BROKEN)

    def test_convention_not_adopted_is_gap_not_drift(self) -> None:
        """来源: bot-commands 的 8 条 Unit Test-00x。

        该测试文件整体没用 [ID] 标签,spec 的编号从未与测试绑定
        → 属覆盖缺口,判 broken 会制造大批误报(实测 128→22 的主因)。
        """
        self.fx.test("a.test.ts", "it('plain name no tag', () => {});")
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="tests/a.test.ts", claim="[Unit Test-001]")
        verify(a, self.fx.root, self.fx.index())
        self.assertEqual(a.verdict, VERDICT_UNVERIFIABLE)
        self.assertIn("从未与测试绑定", a.detail)

    def test_no_host_file_searches_all_tests(self) -> None:
        """来源: session-management 未点名测试文件。

        早期实现把 target 落成字面量 'tests/',读出 0 个用例,
        误判成「未采用约定」,漏掉了最关键的假勾。
        """
        self.fx.test("pool.test.ts", "it('getStatus returns fields [Unit Test-002]', () => {});")
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="tests/", claim="[Unit Test-002]")
        verify(a, self.fx.root, self.fx.index())
        self.assertEqual(a.verdict, VERDICT_HOLDS)
        self.assertTrue(a.needs_human_review, "无宿主命中必须标待人工复核")
        self.assertIn("未证明该测试断言的正是本条 spec 声明", a.detail)

    def test_id_absent_everywhere_is_fake_check(self) -> None:
        """约定在用,但全仓找不到这个 ID → 假勾嫌疑。"""
        self.fx.test("pool.test.ts", "it('other [Unit Test-001]', () => {});")
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="tests/", claim="[Unit Test-777]")
        verify(a, self.fx.root, self.fx.index())
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("假勾嫌疑", a.detail)


class TestSymbolAndConst(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_symbol_present(self) -> None:
        self.fx.src("a.ts", "export function createMetering() {}")
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1, target="createMetering")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_HOLDS)

    def test_symbol_absent_never_existed_is_unbuilt(self) -> None:
        """来源: updateJob / stopAgent —— git 全历史 0 次,是设计未落地非漂移。"""
        self.fx.src("a.ts", "export function other() {}")
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1, target="updateJob")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("设计未落地", a.detail)

    def test_const_value_drift(self) -> None:
        """来源: busyTimeout 10min→30min、job timeout 300s→3600s(12×)。

        「烂的是值,不是名」—— 只验符号存在抓不到这类漂移。
        """
        self.fx.src("a.ts", "const RATE_LIMIT_MAX_RETRY = 8;")
        a = Anchor(type="const", raw="", spec_file="s.md", spec_line=1,
                   target="RATE_LIMIT_MAX_RETRY", claim="2")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("值漂移", a.detail)

    def test_const_value_holds(self) -> None:
        self.fx.src("a.ts", "const RATE_LIMIT_MAX_RETRY = 8;")
        a = Anchor(type="const", raw="", spec_file="s.md", spec_line=1,
                   target="RATE_LIMIT_MAX_RETRY", claim="8")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_HOLDS)


class TestWireAnchor(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_unwired_symbol_detected(self) -> None:
        """来源: metering.checkBalance —— 有定义、有测试、零生产调用点。

        这是「值/接线」类漂移里最危险的一种: 契约声称已接,实际没接。
        """
        self.fx.src("metering.ts", "\n".join([
            "export interface Metering {",
            "  checkBalance(tenantId: string): Promise<boolean>;",
            "}",
            "export function createMetering() {",
            "  async function checkBalance(t: string) { return true; }",
            "  return { checkBalance };",
            "}",
        ]))
        a = Anchor(type="wire", raw="", spec_file="s.md", spec_line=1,
                   target="checkBalance", claim="wired")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("空接线", a.detail)

    def test_wired_symbol_holds(self) -> None:
        """对照组: onSoftQuotaWarning 有真实订阅者。"""
        self.fx.src("metering.ts", "export function onSoftQuotaWarning(h) {}")
        self.fx.src("telegram.ts", "deps.metering?.onSoftQuotaWarning((event) => {});")
        a = Anchor(type="wire", raw="", spec_file="s.md", spec_line=1,
                   target="onSoftQuotaWarning", claim="wired")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_HOLDS)

    def test_callback_param_declaration_is_not_a_call(self) -> None:
        """回归: onLowBalance 曾被误判 holds。

        声明 `onLowBalance(handler: (event: E) => void): void;` 里嵌了一对括号,
        早期排除式用 [^)]* 匹配形参,碰到回调类型就断掉 → 声明被当成调用点,
        「零订阅者」被判成 holds。真实 Relay 上 onLowBalance 确实零订阅。
        """
        self.fx.src("metering.ts", "\n".join([
            "export interface Metering {",
            "  onLowBalance(handler: (event: LowBalanceEvent) => void): void;",
            "}",
            "export function createMetering() {",
            "  return {",
            "    onLowBalance(handler: (event: LowBalanceEvent) => void): void {",
            "      handlers.push(handler);",
            "    },",
            "  };",
            "}",
        ]))
        a = Anchor(type="wire", raw="", spec_file="s.md", spec_line=1,
                   target="onLowBalance", claim="wired")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("空接线", a.detail)

    def test_absent_symbol_distinct_from_unwired(self) -> None:
        """完全不存在 ≠ 有定义没接线,detail 必须分开说。"""
        self.fx.src("a.ts", "export function other() {}")
        a = Anchor(type="wire", raw="", spec_file="s.md", spec_line=1,
                   target="ghostFn", claim="wired")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("完全不存在", a.detail)


class TestManualAnchor(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_manual_never_judged(self) -> None:
        """32% 的断言是性能/属性类,全仓无计时器无指标。

        标 manual 好过假装能自动验 —— 假装可验正是假勾的温床。
        """
        a = Anchor(type="manual", raw="", spec_file="s.md", spec_line=1,
                   target="每进程约 250MB", claim="用 top 观察 RSS")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_UNVERIFIABLE)

    def test_manual_without_steps_is_flagged(self) -> None:
        a = Anchor(type="manual", raw="", spec_file="s.md", spec_line=1,
                   target="无跨租户泄漏", claim="")
        verify(a, self.fx.root, None)
        self.assertIn("缺观察步骤", a.detail)


class TestParseAndInfer(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_parse_explicit_anchor(self) -> None:
        p = self.fx.spec("a.md", '<!-- anchor: test tests/a.test.ts :: "does the thing" -->\n')
        anchors = parse_anchors(p, self.fx.root)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0].type, "test")
        self.assertEqual(anchors[0].target, "tests/a.test.ts")
        self.assertEqual(anchors[0].claim, "does the thing")

    def test_infer_from_existing_style(self) -> None:
        """存量 spec 不会用新语法。不推断则读数恒为 0,工具在真实场景无用。"""
        p = self.fx.spec("b.md", "**测试文件**：`tests/metering.test.ts`\n")
        anchors = infer_anchors(p, self.fx.root)
        self.assertTrue(any(a.target == "tests/metering.test.ts" for a in anchors))
        self.assertTrue(all(a.inferred for a in anchors))

    def test_infer_skips_todo_lines(self) -> None:
        """标了「待实现」的行不算契约声明 —— 它没声称成立,不该判漂移。

        来源: feishu-pairing-http 的 RH-005 标 [ ] 待实现,那 3 个测试文件
        从未存在过,但 spec 是诚实的。
        """
        p = self.fx.spec("c.md", "| RH-005 | 测试：`tests/nope.test.ts` | [ ] | 待实现 |\n")
        anchors = infer_anchors(p, self.fx.root)
        self.assertEqual(len(anchors), 0)

    def test_infer_skips_unchecked_test_cases(self) -> None:
        p = self.fx.spec("d.md", "- [ ] **Unit Test-003**：还没做\n")
        anchors = infer_anchors(p, self.fx.root)
        self.assertEqual(len(anchors), 0)


class TestScanIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_zero_anchor_spec_is_not_healthy(self) -> None:
        """核心读数原则: 一份零锚点的 spec 漂移数=0,但那不是健康。

        只报漂移数会让烂 spec 显示为 100% 健康 —— 那正是假勾的机制。
        覆盖缺口必须与漂移并列呈现。
        """
        self.fx.spec("empty.md", "- [x] 这条断言什么锚点都没绑\n- [x] 这条也是\n")
        reports = scan(self.fx.root, "specs/**/*.md", ["tests/**/*.ts"])
        s = summarize(reports)
        self.assertEqual(s["drift_count"], 0)
        self.assertGreater(s["unanchored_claims"], 0,
                           "零锚点 spec 必须体现为覆盖缺口,不能显示为健康")

    def test_count_claims(self) -> None:
        self.fx.spec("e.md", "\n".join([
            "# 标题不算",
            "- [x] 勾选项算",
            "- [ ] 未勾也算声明",
            "普通段落不算",
        ]))
        p = self.fx.root / "specs" / "e.md"
        self.assertEqual(count_claims(p), 2)

    def test_verifier_error_distinct_from_drift(self) -> None:
        """校验器自身出错必须与「漂移」区分 —— 否则工具故障会伪装成发现。"""
        a = Anchor(type="bogus_type", raw="", spec_file="s.md", spec_line=1, target="x")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_ERROR)


class TestProseDialectClaims(unittest.TestCase):
    """方言 `prose`: 散文体 spec 的契约声明计数。

    实证来源 2026-08-01: PULSE Stats 的三份 spec 用散文 + 无编号表格写契约,
    CLAIM_PATTERNS 三条(勾选框 / 带 FR-AC-REQ 编号的表格行 / 粗体 ID)全是 Relay 方言,
    一条都匹配不上 → claims=0 → 覆盖率 0.0%、未绑锚点 = 0-53 = **负数**。
    而 README/SPEC-RULES 都强调「未绑锚点比漂移更重要」,坏的正是这个更重要的数。
    """

    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_prose_spec_counts_zero_without_dialect(self) -> None:
        """不传方言时散文体恒为 0 —— 这就是被修的缺陷,锁住它以免「修好了又退回去」。"""
        p = self.fx.spec("prose.md", "\n".join([
            "- runtime health 与 coverage 是两条独立轴；unknown/partial 不得显示成绿色。",
            "| 沉默、source stale | 任意 | 绝不自动 resolved |",
        ]))
        self.assertEqual(count_claims(p), 0)

    def test_normative_bullet_counts_with_dialect(self) -> None:
        """依据 specs/pulse-stats.md:28 原文写法(含「不得」的无序列表项)。"""
        p = self.fx.spec("prose.md",
                         "- runtime health 与 coverage 是两条独立轴；"
                         "unknown/partial 不得显示成绿色。\n")
        self.assertEqual(count_claims(p, {"prose"}), 1)

    def test_unnumbered_table_row_counts_with_dialect(self) -> None:
        """依据 specs/pulse-stats.md:131 状态机表 —— 无编号,老模式够不着。"""
        p = self.fx.spec("prose.md", "\n".join([
            "| 输入 | 原状态 | 新状态 |",
            "|---|---|---|",
            "| 沉默、source stale、解析失败 | 任意 | 绝不自动 resolved |",
        ]))
        # 表头无规范动词、分隔行被排除,只有数据行算。
        self.assertEqual(count_claims(p, {"prose"}), 1)

    def test_non_normative_lines_are_not_claims(self) -> None:
        """判据是**规范动词**不是「凡列表行都算」。

        依据 specs/pulse-stats.md:33 `- 开发机:macOS 15.7.3、Xcode 26.3` ——
        环境事实,不是可证伪的约束。凡列表都算会把覆盖缺口这个读数灌水。
        """
        p = self.fx.spec("prose.md", "\n".join([
            "- 开发机：macOS 15.7.3、Xcode 26.3、Swift 6.2.4。",
            "- App 技术基线：Swift 6、SwiftUI `MenuBarExtra`。",
            "- 监控能力与 snapshot schema 真源：`specs/pulse-monitoring-baseline.md`。",
        ]))
        self.assertEqual(count_claims(p, {"prose"}), 0)

    def test_ordered_acceptance_item_counts(self) -> None:
        """依据 specs/pulse-stats.md:41-45「### 验收」编号列表。"""
        p = self.fx.spec("prose.md", "\n".join([
            "1. 状态栏同时表达 runtime health 与 coverage，任何组合都不会假绿。",
            "2. P1 完善前，S1 逐请求兑现率必须为 unknown。",
        ]))
        # 第 1 条无规范动词(描述性),第 2 条有「必须」。
        self.assertEqual(count_claims(p, {"prose"}), 1)

    def test_fenced_code_block_not_counted(self) -> None:
        """代码块内是示例/数据结构,不是契约。

        散文方言判据比老模式宽得多(任何含「必须」的列表行),
        不排除围栏就会把示例算成契约。
        """
        p = self.fx.spec("prose.md", "\n".join([
            "```text",
            "- 这行在代码块里，写了必须也不算契约",
            "```",
            "- 这行在外面，queued message 必须携带自己的 context。",
        ]))
        self.assertEqual(count_claims(p, {"prose"}), 1)

    def test_relay_dialect_unaffected_by_prose_flag(self) -> None:
        """回归: 开方言不得改变老模式已能数到的条数(只增不改)。"""
        body = "\n".join([
            "# 标题不算",
            "- [x] 勾选项算",
            "- [ ] 未勾也算声明",
            "普通段落不算",
        ])
        p = self.fx.spec("relay.md", body)
        self.assertEqual(count_claims(p), 2)
        self.assertEqual(count_claims(p, {"prose"}), 2)


class TestUnanchoredNeverNegative(unittest.TestCase):
    """覆盖缺口不得为负 —— 负的「缺口」是自相矛盾的读数。

    2026-08-01 实测: Stats 三份 spec 得到 `总声明 0 / 已绑锚点 53` → 缺口 -53。
    处置不是 max(0,x)(那会把口径不一致伪装成「缺口为 0 = 已覆盖」,
    正好掩盖 README 强调的最重要读数),而是显式标 basis_mismatch。
    """

    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_negative_gap_becomes_explicit_mismatch(self) -> None:
        from scan import SpecReport
        rep = SpecReport(path="s.md", claims=0)
        rep.anchored_claims = 53
        s = summarize([rep])
        self.assertTrue(s["unanchored_basis_mismatch"])
        self.assertIsNone(s["unanchored_claims"],
                          "口径不可比时应显式置 None,不能印一个假的 0 或负数")

    def test_render_md_prints_no_negative_number(self) -> None:
        """呈现层不得出现负数缺口,且必须说明原因。"""
        from scan import SpecReport, render_md
        rep = SpecReport(path="s.md", claims=0)
        rep.anchored_claims = 53
        s = summarize([rep])
        md = render_md([rep], s, self.fx.root)
        self.assertNotIn("-53", md)
        self.assertIn("口径不可比", md)

    def test_normal_case_still_reports_number(self) -> None:
        """口径可比时照常给数字,不因防负数而丢掉正常读数。"""
        from scan import SpecReport
        rep = SpecReport(path="s.md", claims=43)
        rep.anchored_claims = 27
        s = summarize([rep])
        self.assertFalse(s["unanchored_basis_mismatch"])
        self.assertEqual(s["unanchored_claims"], 16)


class TestGitPathspec(unittest.TestCase):
    """回归: git pathspec 相对仓根,不是相对 cwd。

    Relay 无自己的 .git(嵌在 PULSE 仓内,前缀 Relay/),
    传 `src/` 匹配不到任何东西 → git log 恒空 →
    **每条「真漂移」都被误判成「设计未落地」**,处置方向相反。
    2026-07-25 由重写实证分身发现;同型于本 session 早先的 `^src/` grep 锚点错。
    """

    def test_prefix_empty_when_repo_is_git_root(self) -> None:
        import subprocess as sp
        from anchors import _git_prefix, _GITROOT_CACHE
        fx = RepoFixture()
        try:
            sp.run(["git", "init", "-q"], cwd=fx.root, check=True, capture_output=True)
            _GITROOT_CACHE.clear()
            self.assertEqual(_git_prefix(fx.root), "")
        finally:
            fx.close()

    def test_prefix_computed_for_nested_subdir(self) -> None:
        import subprocess as sp
        from anchors import _git_prefix, _GITROOT_CACHE
        fx = RepoFixture()
        try:
            sp.run(["git", "init", "-q"], cwd=fx.root, check=True, capture_output=True)
            nested = fx.root / "sub" / "proj"
            nested.mkdir(parents=True)
            _GITROOT_CACHE.clear()
            self.assertEqual(_git_prefix(nested), "sub/proj/")
        finally:
            fx.close()

    def test_deleted_symbol_classified_as_real_drift_in_nested_repo(self) -> None:
        """端到端: 嵌套子目录里删掉的符号,必须判「真漂移」而非「设计未落地」。"""
        import subprocess as sp
        from anchors import _EVER_CACHE, _GITROOT_CACHE, _SOURCE_CACHE
        fx = RepoFixture()
        try:
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            import os
            e = {**os.environ, **env}
            sp.run(["git", "init", "-q"], cwd=fx.root, check=True, capture_output=True)
            proj = fx.root / "sub" / "proj"
            (proj / "src").mkdir(parents=True)
            f = proj / "src" / "a.ts"
            f.write_text("export function claimPendingAsk() {}\n", encoding="utf-8")
            sp.run(["git", "add", "-A"], cwd=fx.root, check=True, capture_output=True)
            sp.run(["git", "commit", "-qm", "add"], cwd=fx.root, check=True,
                   capture_output=True, env=e)
            f.write_text("export function other() {}\n", encoding="utf-8")
            sp.run(["git", "add", "-A"], cwd=fx.root, check=True, capture_output=True)
            sp.run(["git", "commit", "-qm", "del"], cwd=fx.root, check=True,
                   capture_output=True, env=e)

            _EVER_CACHE.clear(); _GITROOT_CACHE.clear(); _SOURCE_CACHE.clear()
            a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1,
                       target="claimPendingAsk")
            verify(a, proj, None)
            self.assertEqual(a.verdict, VERDICT_BROKEN)
            self.assertIn("真漂移", a.detail,
                          "嵌套仓里被删的符号误判成『设计未落地』= pathspec 前缀没算对")
        finally:
            fx.close()


class TestMonorepoSibling(unittest.TestCase):
    """回归: monorepo 里兄弟项目的同名符号,不算本项目「曾存在」。

    Relay 嵌在 PULSE 内,markPaid / ensureTenantLinuxUser 在 Platform/ 真实存在,
    但对 Relay 而言从未落地。裸搜全仓会把 5/16 条「设计未落地」误升为「真漂移」,
    而两者处置方向相反(改 spec vs 补实现)。
    """

    def test_sibling_project_symbol_not_counted(self) -> None:
        import os
        import subprocess as sp
        from anchors import _EVER_CACHE, _GITROOT_CACHE, _SOURCE_CACHE
        fx = RepoFixture()
        try:
            e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            sp.run(["git", "init", "-q"], cwd=fx.root, check=True, capture_output=True)
            mine = fx.root / "projA"
            (mine / "src").mkdir(parents=True)
            (mine / "src" / "a.ts").write_text("export function mineOnly() {}\n", encoding="utf-8")
            sib = fx.root / "projB"
            (sib / "src").mkdir(parents=True)
            (sib / "src" / "b.ts").write_text("export function markPaid() {}\n", encoding="utf-8")
            sp.run(["git", "add", "-A"], cwd=fx.root, check=True, capture_output=True)
            sp.run(["git", "commit", "-qm", "init"], cwd=fx.root, check=True,
                   capture_output=True, env=e)

            _EVER_CACHE.clear(); _GITROOT_CACHE.clear(); _SOURCE_CACHE.clear()
            a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1, target="markPaid")
            verify(a, mine, None)
            self.assertEqual(a.verdict, VERDICT_BROKEN)
            self.assertIn("设计未落地", a.detail,
                          "兄弟项目的符号被算成本项目曾存在 = monorepo 前缀没过滤")
        finally:
            fx.close()


class TestHistoryIndexSubstring(unittest.TestCase):
    """回归: 历史索引存的是完整标识符,spec 常写它的**前缀/简称**。

    实测 Relay: spec 写 `maxRateLimitRetries`,代码真实标识符是
    `maxRateLimitRetriesPerMessage`。纯精确匹配 → 「曾存在后被删」
    被误判成「从未落地」。`git log -S` 本来是子串语义,索引必须对齐。
    """

    def test_prefix_of_real_identifier_counts_as_existed(self) -> None:
        import os
        import subprocess as sp
        from anchors import _EVER_CACHE, _GITROOT_CACHE, _SOURCE_CACHE, _HIST_TOKENS
        fx = RepoFixture()
        try:
            e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            sp.run(["git", "init", "-q"], cwd=fx.root, check=True, capture_output=True)
            f = fx.root / "src" / "a.ts"
            f.write_text("const maxRateLimitRetriesPerMessage = 2;\n", encoding="utf-8")
            sp.run(["git", "add", "-A"], cwd=fx.root, check=True, capture_output=True)
            sp.run(["git", "commit", "-qm", "add"], cwd=fx.root, check=True,
                   capture_output=True, env=e)
            f.write_text("const other = 1;\n", encoding="utf-8")
            sp.run(["git", "add", "-A"], cwd=fx.root, check=True, capture_output=True)
            sp.run(["git", "commit", "-qm", "del"], cwd=fx.root, check=True,
                   capture_output=True, env=e)

            for c in (_EVER_CACHE, _GITROOT_CACHE, _SOURCE_CACHE, _HIST_TOKENS):
                c.clear()
            a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1,
                       target="maxRateLimitRetries")
            verify(a, fx.root, None)
            self.assertEqual(a.verdict, VERDICT_BROKEN)
            self.assertIn("真漂移", a.detail,
                          "spec 写简称时被误判成从未落地 = 索引没做子串匹配")
        finally:
            fx.close()


class TestTokenIndexEquivalence(unittest.TestCase):
    """倒排索引必须与逐行正则**等价**——优化不许改变发现。

    背景: `_grep` 逐符号全表扫,Relay 476 符号 × 48k 行 = 2286 万次 re.search,
    profile 实测占全程 95%(86.4s 中的 53s)。换倒排索引后 1.8s。
    但性能优化若改了判定结果,就是引入新缺陷——故锁等价性。
    """

    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_index_matches_regex_scan(self) -> None:
        import re as _re
        from anchors import _grep, _grep_symbol, _SRC_GLOBS, _TOKEN_INDEX
        self.fx.src("a.ts", "\n".join([
            "export function handleStart() {}",
            "const maxRetry = 8;",
            "// handleStart mentioned in a comment",
            "obj.handleStart();",
            "notHandleStartAtAll();",
        ]))
        _TOKEN_INDEX.clear()
        for sym in ("handleStart", "maxRetry", "missingSym"):
            via_regex = _grep(self.fx.root, rf"\b{_re.escape(sym)}\b", _SRC_GLOBS)
            via_index = _grep_symbol(self.fx.root, sym, _SRC_GLOBS)
            self.assertEqual(
                sorted(via_regex), sorted(via_index),
                f"{sym}: 倒排索引与正则扫描结果不一致 = 优化改变了发现",
            )

    def test_index_does_not_match_substring(self) -> None:
        """`\\bfoo\\b` 不该命中 `notFooAtAll` —— 索引按完整标识符分词,天然满足。"""
        from anchors import _grep_symbol, _SRC_GLOBS, _TOKEN_INDEX
        self.fx.src("a.ts", "notHandleStartAtAll();\n")
        _TOKEN_INDEX.clear()
        self.assertEqual(_grep_symbol(self.fx.root, "HandleStart", _SRC_GLOBS), [])


class TestConsistency(unittest.TestCase):
    """spec 自身内部矛盾 —— 不看代码就能发现的那类。"""

    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_status_verified_but_unchecked(self) -> None:
        """来源: finance-engine income-manager/tasks.md

        头部 status: "verified",正文 94 个任务框只勾了 1 个。
        抽查发现 ProfitSummary 已实现、calculate_revenue 有 3 个测试
        —— 活真干了,勾选框被弃用。读者按 status 以为已验收,按框以为没开工。
        """
        from consistency import check_spec
        p = self.fx.spec("t.md", "\n".join([
            "---", 'status: "verified"', "---",
            "- [x] 做了这个",
        ] + [f"- [ ] 没勾的第 {i} 条" for i in range(20)]))
        issues = check_spec(p, self.fx.root)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, "status-vs-checkbox")

    def test_status_draft_not_flagged(self) -> None:
        """未声称完成的 spec 有未勾项是正常的,不该报。"""
        from consistency import check_spec
        p = self.fx.spec("t.md", "\n".join([
            "---", 'status: "building"', "---",
        ] + [f"- [ ] 第 {i} 条" for i in range(20)]))
        self.assertEqual(len(check_spec(p, self.fx.root)), 0)

    def test_status_verified_and_checked_is_clean(self) -> None:
        from consistency import check_spec
        p = self.fx.spec("t.md", "\n".join([
            "---", 'status: "verified"', "---",
        ] + [f"- [x] 第 {i} 条" for i in range(20)]))
        self.assertEqual(len(check_spec(p, self.fx.root)), 0)

    def test_checkbox_abandoned_repo_wide(self) -> None:
        """全仓勾选率过低 → 勾选框失去信号价值。

        实测参照: Relay 63%(维护中) vs finance-engine 33%(已弃用)。
        """
        from consistency import check_repo
        self.fx.spec("a.md", "\n".join([f"- [ ] 第 {i} 条" for i in range(60)]))
        self.fx.spec("b.md", "\n".join([f"- [x] 第 {i} 条" for i in range(10)]))
        issues, stats = check_repo(self.fx.root, "specs/**/*.md")
        self.assertTrue(any(i.kind == "checkbox-abandoned" for i in issues))
        self.assertLess(stats["checked_ratio"], 0.4)

    def test_healthy_checkbox_repo_not_flagged(self) -> None:
        from consistency import check_repo
        self.fx.spec("a.md", "\n".join([f"- [x] 第 {i} 条" for i in range(60)]))
        self.fx.spec("b.md", "\n".join([f"- [ ] 第 {i} 条" for i in range(10)]))
        issues, stats = check_repo(self.fx.root, "specs/**/*.md")
        self.assertFalse(any(i.kind == "checkbox-abandoned" for i in issues))


class TestPytestDialect(unittest.TestCase):
    """跨仓移植: finance-engine 用 pytest,写法与 Relay 的 vitest 完全不同。

    实测教训: 推断层只认单一仓库方言时,换个项目覆盖率从 48.3% 塌到 5.8%。
    锚点思想通用,**写法必须按方言适配**。
    """

    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_python_test_names_extracted(self) -> None:
        """pytest 用例名是函数名,不是 it() 里的字符串。"""
        (self.fx.root / "tests").mkdir(exist_ok=True)
        (self.fx.root / "tests" / "test_x.py").write_text(
            "def test_load_cost_config():\n    pass\n", encoding="utf-8")
        idx = build_test_index(self.fx.root, ["tests/**/*.py"])
        names = set().union(*idx.values()) if idx else set()
        self.assertIn("test_load_cost_config", names)

    def test_infer_unit_test_colon_dialect(self) -> None:
        """finance-engine 写法: `Unit Test: \\`test_foo()\\` - 说明`"""
        p = self.fx.spec("t.md", "*   Unit Test: `test_load_cost_config()` - 验证读取 YAML\n")
        anchors = infer_anchors(p, self.fx.root)
        self.assertTrue(any(a.claim == "test_load_cost_config" for a in anchors))

    def test_test_fn_not_double_counted_as_symbol(self) -> None:
        """回归: 同一个 test_xxx() 曾同时产出 test 锚点和 symbol 锚点,漂移数翻倍。"""
        p = self.fx.spec("t.md", "*   Unit Test: `test_calc_fee()` - 说明\n")
        anchors = infer_anchors(p, self.fx.root)
        self.assertEqual(sum(1 for a in anchors if a.type == "symbol"), 0)

    def test_missing_pytest_fn_is_actionable(self) -> None:
        """spec 点名的测试函数全仓不存在 → 测试未落地,是可行动读数。"""
        (self.fx.root / "tests").mkdir(exist_ok=True)
        (self.fx.root / "tests" / "test_x.py").write_text(
            "def test_other():\n    pass\n", encoding="utf-8")
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="tests/", claim="test_nonexistent")
        verify(a, self.fx.root, build_test_index(self.fx.root, ["tests/**/*.py"]))
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("测试未落地", a.detail)

    def test_symbol_search_includes_tests(self) -> None:
        """spec 点名的常是计划中的测试函数,只搜 src/ 会误报。"""
        (self.fx.root / "tests").mkdir(exist_ok=True)
        (self.fx.root / "tests" / "test_x.py").write_text(
            "def test_discrepancy_creation():\n    pass\n", encoding="utf-8")
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1,
                   target="test_discrepancy_creation")
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_HOLDS)


class TestRepoDialect(unittest.TestCase):
    """回归: 仓库布局是**方言**,不是常量。

    2026-08-01 实测: spec-anchor 硬编码 `src/**/*.{ts,js,py}` + `git log -- src/ tests/`,
    Stats 两条都不满足(Python 在 exporter/pulse_stats/、Swift 在 PulseStats/)
    → 53 条锚点 100% 判 broken,抽样回源 grep 误报率 87.5%。
    根因是**工具看不见代码**,不是判定逻辑松紧。故本组只验「看得见」,
    不验「判得松」——判定逻辑一行未动。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _SOURCE_CACHE.clear()
        _EVER_CACHE.clear()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, body: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        _SOURCE_CACHE.clear()

    # --- 1. 自定义 src-glob 生效 ---

    def test_default_globs_blind_to_nonstandard_layout(self) -> None:
        """基线(证明问题真实存在): 不传 src-glob 时,非 src/ 布局的符号判 broken。"""
        self._write("exporter/pulse_stats/producer.py", "def capability_id():\n    pass\n")
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1, target="capability_id")
        verify(a, self.root, None)
        self.assertEqual(a.verdict, VERDICT_BROKEN,
                         "默认 glob 本就看不见 exporter/ —— 这是本次要修的前提")

    def test_custom_src_glob_makes_symbol_visible(self) -> None:
        """同一个符号、同一套判定逻辑,只是 glob 指对了 → holds。"""
        self._write("exporter/pulse_stats/producer.py", "def capability_id():\n    pass\n")
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1, target="capability_id")
        verify(a, self.root, None, src_globs=["exporter/pulse_stats/**/*.py"])
        self.assertEqual(a.verdict, VERDICT_HOLDS)

    def test_custom_src_glob_applies_to_const_and_wire(self) -> None:
        """贯通必须覆盖全部校验器,不能只修 symbol 一条路。"""
        self._write("exporter/pulse_stats/cfg.py",
                    "MAX_RETRIES = 5\n\ndef caller():\n    return compute_total()\n"
                    "\ndef compute_total():\n    return 1\n")
        globs = ["exporter/**/*.py"]

        c = Anchor(type="const", raw="", spec_file="s.md", spec_line=1,
                   target="MAX_RETRIES", claim="5")
        verify(c, self.root, None, src_globs=globs)
        self.assertEqual(c.verdict, VERDICT_HOLDS, "const 校验器没吃到 src_globs")

        w = Anchor(type="wire", raw="", spec_file="s.md", spec_line=1,
                   target="compute_total", claim="wired")
        verify(w, self.root, None, src_globs=globs)
        self.assertEqual(w.verdict, VERDICT_HOLDS, "wire 校验器没吃到 src_globs")

    def test_custom_src_glob_does_not_loosen_judgement(self) -> None:
        """反向对照: glob 指对了,真不存在的符号仍judged broken(没放宽判据)。"""
        self._write("exporter/pulse_stats/producer.py", "def capability_id():\n    pass\n")
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1,
                   target="never_implemented_thing")
        verify(a, self.root, None, src_globs=["exporter/pulse_stats/**/*.py"])
        self.assertEqual(a.verdict, VERDICT_BROKEN)

    # --- 2. swift 文件被索引到 ---

    def test_swift_source_is_indexed(self) -> None:
        """Swift 源码必须能进源码索引 —— 默认 glob 连 .swift 后缀都不认。"""
        self._write("PulseStats/Core/SnapshotDecoder.swift",
                    "struct SnapshotDecoder {\n    func decode() -> Int { 0 }\n}\n")
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1,
                   target="SnapshotDecoder")
        verify(a, self.root, None, src_globs=["PulseStats/**/*.swift"])
        self.assertEqual(a.verdict, VERDICT_HOLDS)

    def test_swift_xctest_case_names_extracted(self) -> None:
        """Swift XCTest 用例名是 `func testXxx()`,与 pytest/JS 两种方言都不同。"""
        self._write("PulseStatsTests/LampRulesTests.swift",
                    "import XCTest\n"
                    "final class LampRulesTests: XCTestCase {\n"
                    "    func testAmberWhenStale() throws {\n        XCTAssertTrue(true)\n    }\n"
                    "    func testRedWhenDown() async throws {}\n"
                    "    func helperNotATest() {}\n"
                    "}\n")
        idx = build_test_index(self.root, ["PulseStatsTests/**/*.swift"])
        names = set().union(*idx.values()) if idx else set()
        self.assertIn("testAmberWhenStale", names)
        self.assertIn("testRedWhenDown", names)
        self.assertNotIn("helperNotATest", names, "非 test 前缀的辅助函数不是用例")

    def test_swift_test_anchor_resolves_against_swift_host(self) -> None:
        """端到端: 指向 .swift 测试文件的 test 锚点能判 holds。"""
        self._write("PulseStatsTests/LampRulesTests.swift",
                    "final class LampRulesTests: XCTestCase {\n"
                    "    func testAmberWhenStale() throws {}\n}\n")
        idx = build_test_index(self.root, ["PulseStatsTests/**/*.swift"])
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="PulseStatsTests/LampRulesTests.swift",
                   claim="testAmberWhenStale")
        verify(a, self.root, idx)
        self.assertEqual(a.verdict, VERDICT_HOLDS)

    def test_missing_swift_test_file_is_drift(self) -> None:
        """点名的 .swift 测试文件不存在 → 真漂移(与 .ts/.py 同等对待)。"""
        a = Anchor(type="test", raw="", spec_file="s.md", spec_line=1,
                   target="PulseStatsTests/GoneTests.swift", claim="testSomething")
        verify(a, self.root, {})
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("不存在", a.detail)

    # --- 3. history-path 生效 ---

    def _git_repo_with_deleted_symbol(self) -> None:
        """建一个把符号删掉的历史,代码在非 src/ 的目录下。"""
        import os
        import subprocess as sp
        e = {**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        sp.run(["git", "init", "-q"], cwd=self.root, check=True, capture_output=True)
        self._write("Stats/exporter/pulse_stats/a.py", "def removed_helper():\n    pass\n")
        sp.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        sp.run(["git", "commit", "-qm", "add"], cwd=self.root, check=True,
               capture_output=True, env=e)
        self._write("Stats/exporter/pulse_stats/a.py", "def other():\n    pass\n")
        sp.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        sp.run(["git", "commit", "-qm", "del"], cwd=self.root, check=True,
               capture_output=True, env=e)

    def test_history_path_finds_deleted_symbol_outside_src(self) -> None:
        """传了 history-path → 认出「曾存在后被删」= 真漂移。"""
        from anchors import _HIST_TOKENS
        self._git_repo_with_deleted_symbol()
        _HIST_TOKENS.clear(); _EVER_CACHE.clear(); _SOURCE_CACHE.clear()
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1,
                   target="removed_helper")
        verify(a, self.root, None,
               src_globs=["Stats/exporter/pulse_stats/**/*.py"],
               history_paths=["Stats/"])
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("真漂移", a.detail,
                      "history-path 没贯通 → 被删的符号误判成『从未落地』,处置方向相反")

    def test_default_history_path_misses_symbol_outside_src(self) -> None:
        """基线对照: 不传 history-path,同一符号被误判「设计未落地」。"""
        from anchors import _HIST_TOKENS
        self._git_repo_with_deleted_symbol()
        _HIST_TOKENS.clear(); _EVER_CACHE.clear(); _SOURCE_CACHE.clear()
        a = Anchor(type="symbol", raw="", spec_file="s.md", spec_line=1,
                   target="removed_helper")
        verify(a, self.root, None, src_globs=["Stats/exporter/pulse_stats/**/*.py"])
        self.assertEqual(a.verdict, VERDICT_BROKEN)
        self.assertIn("设计未落地", a.detail)

    def test_history_paths_are_cache_keyed(self) -> None:
        """不同 history_paths 必须分开缓存,否则第二次扫描吃到第一次的结果。"""
        from anchors import _HIST_TOKENS, _history_tokens
        self._git_repo_with_deleted_symbol()
        _HIST_TOKENS.clear()
        hit = _history_tokens(self.root, history_paths=["Stats/"])
        miss = _history_tokens(self.root, history_paths=["nonexistent-dir/"])
        self.assertIsNotNone(hit)
        self.assertIn("removed_helper", hit or set())
        self.assertNotIn("removed_helper", miss or set(),
                         "缓存键没带 pathspec → 两次不同范围的扫描互相污染")


class TestAbsentAnchor(unittest.TestCase):
    """删除锚点(absent): 断言某符号不该再存在。

    上游 Ken 落码,2026-08-08 移植进本副本(wi_20260805_616)。
    来源是我 2026-08-01 报的设计缺口: 五类锚点全是存在性断言,
    表达不了「不该存在」→ 删除类清理拿不到机器闸保护。

    **本类第一条是阳性对照,是这批用例里唯一决定性的那条**:
    只验「真删了判 holds」证明不了闸有用——一个永远返回 holds 的实现
    也能让那条绿。必须验「加回来判 broken」。
    """

    def setUp(self) -> None:
        self.fx = RepoFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def _verdict(self, body: str, sym: str = "MAX_ALERTS", claim: str = "") -> Anchor:
        self.fx.src("live.py", body)
        tail = f" :: {claim}" if claim else ""
        sp = self.fx.spec("s.md", f"<!-- anchor: absent {sym}{tail} -->")
        anchors = parse_anchors(sp, self.fx.root)
        self.assertEqual(len(anchors), 1, "absent 没被 _ANCHOR_RE 认出——类型词表漏改")
        return verify(anchors[0], self.fx.root, None)

    def test_revived_constant_is_broken(self) -> None:
        """阳性对照(决定性): 已删的常量被加回来,必须判 broken。

        2026-07-28 大鸟定「不用老化功能」删掉六个死配置,但没有任何闸
        能防它们被照着仍在漂的旧 spec 加回来。这条就是那个闸。
        """
        a = self._verdict("MAX_ALERTS = 20\n", claim="2026-07-28 定不用老化功能")
        self.assertEqual(a.verdict, VERDICT_BROKEN,
                         "常量被加回来却没判 broken → 闸是假的,删除类清理仍无保护")
        self.assertIn("回来了", a.detail or "")
        self.assertIn("2026-07-28", a.detail or "", "claim 未带进 detail,人无从复核依据")

    def test_truly_deleted_holds(self) -> None:
        """阴性对照: 真删干净了判 holds。"""
        self.assertEqual(self._verdict("OTHER = 1\n").verdict, VERDICT_HOLDS)

    def test_archival_comment_is_not_revival(self) -> None:
        """留档注释里怀念它不算复活——这一条依赖 P0 的命中行分类。

        实测支撑(2026-08-08 跑真 Stats 仓): 六个死配置里五个都有留档注释,
        共 6 处。没有命中行分类,这 6 处会逐条变成误报 broken——
        故 P0 是 absent 的**前置件**而非可选项,收益排序在这里不适用。
        """
        a = self._verdict("# MAX_ALERTS = 20 已于 2026-07-28 删除,留档\nOTHER = 1\n")
        self.assertEqual(a.verdict, VERDICT_HOLDS,
                         "留档注释被当成复活 → P0 命中行分类没生效")

    def test_string_literal_is_not_revival(self) -> None:
        """字符串字面量里出现也不算复活(同上,靠 P0 分类)。"""
        a = self._verdict('LOG = "MAX_ALERTS was removed"\nOTHER = 1\n')
        self.assertEqual(a.verdict, VERDICT_HOLDS)

    def test_dangling_reference_flagged_for_human(self) -> None:
        """有活代码读它但没人定义它 = 残留引用,不判 broken 但要人看一眼。"""
        a = self._verdict("print(MAX_ALERTS)\n")
        self.assertEqual(a.verdict, VERDICT_HOLDS)
        self.assertTrue(a.needs_human_review, "残留引用应标记待人复核")

    def test_dialect_globs_respected(self) -> None:
        """移植时的实质改写: 取数走 _code_globs(src_globs) 而非上游的模块级常量。

        上游 _verify_absent 用硬编的 _CODE_GLOBS,那个符号在本副本不存在
        (本副本是方言参数化的函数)。2026-08-05 我核出这点时,上游给的
        「只需新增三样、不碰你的方言路径」不成立——故这条钉住透传没断。
        """
        (self.fx.root / "custom").mkdir()
        (self.fx.root / "custom" / "app.py").write_text("MAX_ALERTS = 20\n", encoding="utf-8")
        self.fx.src("live.py", "OTHER = 1\n")
        sp = self.fx.spec("s.md", "<!-- anchor: absent MAX_ALERTS -->")
        a = parse_anchors(sp, self.fx.root)[0]
        # 默认 glob 看不到 custom/ → holds
        verify(a, self.fx.root, None)
        self.assertEqual(a.verdict, VERDICT_HOLDS, "默认路径读数应与参数化前一致")
        # 传了方言 glob → 必须看见 custom/ 里的复活
        b = parse_anchors(sp, self.fx.root)[0]
        verify(b, self.fx.root, None, src_globs=["custom/**/*.py"])
        self.assertEqual(b.verdict, VERDICT_BROKEN,
                         "src_globs 没透传进 _verify_absent → 方言仓的复活抓不到")


if __name__ == "__main__":
    unittest.main(verbosity=2)
