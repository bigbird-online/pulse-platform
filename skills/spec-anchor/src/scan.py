#!/usr/bin/env python3
"""
scan.py — 全量漂移图扫描器

对一个仓库的 spec 集合产出漂移图:每份 spec 有多少契约声明、其中多少绑了锚点、
锚点里多少成立/失效/不可验。

两类产出,缺一不可:
  1. 锚点判定  — 已绑锚点的契约,现在还成立吗(broken = 漂移)
  2. 覆盖缺口  — **没绑锚点的契约声明有多少**。这是比 1 更重要的读数:
     采证实证「pool 的 11 处漂移里 8 处、6 处缺失里 4 处,一条断言测试就能抓到
     ——漂移不是难以发现,是根本没设检查」。只报 1 会让一份零锚点的烂 spec
     显示为「100% 健康」,那正是假勾的机制。

用法:
  python3 scan.py --repo <仓库根> --spec-glob 'specs/**/*.md' [--json out.json] [--md out.md]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from anchors import (  # noqa: E402
    Anchor, build_test_index, parse_anchors, verify, verdict_counts,
    VERDICT_HOLDS, VERDICT_BROKEN, VERDICT_UNVERIFIABLE, VERDICT_ERROR,
)
from infer import infer_anchors  # noqa: E402
from consistency import check_repo  # noqa: E402

# 「契约声明」的识别: spec 里看起来像可验证断言的行。
# 依据采证: 真正被消费的是任务 ID / 验收标准 / 接口定义,不是散文段落。
CLAIM_PATTERNS = [
    re.compile(r"^\s*[-*]\s*\[[ xX]\]\s+(?P<text>.+)$"),          # - [x] 勾选项
    re.compile(r"^\s*\|\s*(?P<id>(?:FR|AC|REQ|T|P[0-9T]*)[-–][\w.-]+)\s*\|(?P<text>.+)$"),  # 表格里带 ID 的行
    re.compile(r"^\s*[-*]\s*\*\*(?P<id>(?:FR|AC|REQ|Unit Test|Integration Test)[-–\s][\w.-]*)\*\*[：:]\s*(?P<text>.+)$"),
]

# 明显不是契约的行(标题、纯说明)
_NOISE = re.compile(r"^\s*(#|>|\||---|\*\*(说明|背景|参考)\*\*)")

# ---------------------------------------------------------------------------
# 方言 `prose`: 散文体 spec 的契约声明识别
# ---------------------------------------------------------------------------
# 为什么要另立方言而不是直接加进 CLAIM_PATTERNS:
#   上面三条模式全是 Relay 方言(勾选框 / 带 FR-AC-REQ 编号的表格行 / 粗体 ID)。
#   PULSE Stats 的 spec 用散文 + 无编号表格写契约,一条都匹配不上 → claims=0 →
#   覆盖率 0.0%、未绑锚点 = 0-53 = **负数**。而 README/SPEC-RULES 都强调
#   「未绑锚点比漂移更重要」,这个更重要的数正是坏的。
#
# 判据核心 = **规范动词**,不是「凡列表行都算」。
#   实读三份 spec 后确认: 无序列表里既有契约也有环境说明,靠「是不是列表」区分不了。
#   例(specs/pulse-stats.md):
#     :33  `- 开发机:macOS 15.7.3、Xcode 26.3、Swift 6.2.4。`     ← 环境事实,不是契约
#     :29  `- ...S1 逐请求兑现率与 E2E P95/P99 必须为 unknown;`   ← 契约(可验证的约束)
#   只有出现规范动词(必须/不得/禁止/...)才构成「可被证伪的约束」,这正是锚点该绑的对象。
#
# 默认关闭(--claim-dialect 不传即 Relay 原读数逐字节不变);
# 与既有 --src-glob / --history-path 同为「仓库方言」参数,沿用同一设计。

# 规范动词: 汉语 spec 里表达 MUST / MUST NOT 的写法。
# 只收「硬约束」词,不收「建议/可选」词(可以/建议/推荐/尽量),
# 否则会把讨论性文字算成契约,把覆盖缺口这个数冲淡。
_NORMATIVE = re.compile(
    r"(必须|必需|不得|禁止|严禁|不许|不能|永不|绝不|只允许|仅允许|唯一允许|一律|不再|应当|需保证|保证不)"
)

# 结构上「像一条条目」的行: 无序列表 / 有序列表 / 表格数据行。
_PROSE_BULLET = re.compile(r"^\s*[-*]\s+(?P<text>\S.*)$")
_PROSE_ORDERED = re.compile(r"^\s*\d+[.)]\s+(?P<text>\S.*)$")
_PROSE_TABLE_ROW = re.compile(r"^\s*\|(?P<text>.+)\|\s*$")
# 表格的表头分隔行 `|---|---|` 不是数据行
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


@dataclass
class SpecReport:
    path: str
    claims: int = 0
    anchored_claims: int = 0
    anchors: list[Anchor] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        # 注意: 不截顶。逐份 spec 的 >100% 是**有信息量的信号** ——
        # 它当场暴露「这份 spec 单行绑了多个锚点」(如 Relay 的
        # retrospective-2026-06-10.md 4 条声明 / 13 个锚点 = 325%)。
        # 截顶成 100% 反而把口径不一致藏起来,且会改动既有仓报告。
        # 总览层的负数缺口另行处置(见 summarize 的 unanchored_basis_mismatch)。
        return (self.anchored_claims / self.claims) if self.claims else 0.0


def _is_prose_claim(line: str) -> bool:
    """方言 `prose`: 这一行是不是散文体 spec 的契约声明。

    两个条件同时成立才算:
      1. 结构上是「一条条目」(无序/有序列表项,或表格数据行);
      2. 含规范动词(必须/不得/禁止/...) —— 即它断言了一个可被证伪的约束。

    条件 2 是关键。只看条件 1 会把环境说明、背景陈述全算成契约
    (`- 开发机:macOS 15.7.3` 那种),把「未绑锚点」这个读数灌水。
    """
    if _TABLE_SEP.match(line):
        return False
    m = _PROSE_BULLET.match(line) or _PROSE_ORDERED.match(line) or _PROSE_TABLE_ROW.match(line)
    if not m:
        return False
    return bool(_NORMATIVE.search(m.group("text")))


def count_claims(spec_path: Path, dialects: set[str] | None = None) -> int:
    """数一份 spec 里的契约声明条数。

    dialects: 额外启用的方言集合。留空 = 只用 CLAIM_PATTERNS(既有仓读数不变)。
              `prose` = additionally 识别散文体写法,见模块顶部依据。
    """
    try:
        text = spec_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    dialects = dialects or set()
    want_prose = "prose" in dialects
    n = 0
    in_fence = False
    for line in text.splitlines():
        # 代码块里的内容是示例/数据结构,不是契约声明。
        # 既有三条模式在 Relay 上碰巧没被围栏内容命中,但散文方言的判据宽得多
        # (任何含「必须」的列表行),不排除围栏就会把示例代码算成契约。
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = False
        if not (_NOISE.match(line) and not re.match(r"^\s*\|\s*(FR|AC|REQ|T|P)", line)):
            for pat in CLAIM_PATTERNS:
                if pat.match(line):
                    matched = True
                    break
        # 散文方言独立判断: _NOISE 会把所有表格行(`|` 开头)挡掉,
        # 而 Stats 的契约大量写在无编号表格里,必须绕开那道 noise 闸。
        if not matched and want_prose and _is_prose_claim(line):
            matched = True
        if matched:
            n += 1
    return n


def scan(
    repo: Path,
    spec_glob: str,
    test_globs: list[str],
    use_infer: bool = True,
    src_globs: list[str] | None = None,
    history_paths: list[str] | None = None,
    claim_dialects: set[str] | None = None,
) -> list[SpecReport]:
    """扫一个仓的 spec 集合。

    src_globs / history_paths / claim_dialects 是**仓库方言**参数
    (CLI: --src-glob / --history-path / --claim-dialect)。
    留空 = 沿用 anchors.py 里的默认 `src/` 布局与 Relay 方言的声明判据,既有仓读数不变。
    """
    test_index = build_test_index(repo, test_globs)
    reports: list[SpecReport] = []

    spec_files = sorted(p for p in repo.glob(spec_glob) if p.is_file())
    for spec in spec_files:
        rep = SpecReport(path=str(spec.relative_to(repo)))
        rep.claims = count_claims(spec, claim_dialects)
        anchors = parse_anchors(spec, repo)
        if use_infer:
            # 存量 spec 不会用新语法写锚点。不推断则读数恒为 0,工具在真实场景无用。
            explicit_keys = {(a.type, a.target, a.claim) for a in anchors}
            for a in infer_anchors(spec, repo):
                if (a.type, a.target, a.claim) not in explicit_keys:
                    anchors.append(a)
        for a in anchors:
            verify(a, repo, test_index, src_globs, test_globs, history_paths)
        rep.anchors = anchors
        rep.anchored_claims = len(anchors)
        reports.append(rep)
    return reports


def summarize(reports: list[SpecReport]) -> dict:
    all_anchors = [a for r in reports for a in r.anchors]
    counts = verdict_counts(all_anchors)
    total_claims = sum(r.claims for r in reports)
    total_anchored = sum(r.anchored_claims for r in reports)
    explicit = [a for a in all_anchors if not a.inferred]
    inferred = [a for a in all_anchors if a.inferred]

    # 「未绑锚点」= 声明数 - 已绑锚点数。两个数的口径并不同源:
    #   claims  按**行**数(一行一条声明);
    #   anchors 按**符号**数(一行可以抽出多个锚点,如一行提到 2 个函数 = 2 个锚点)。
    # 所以 anchored > claims 是可能的,差值会变负 —— 负的「覆盖缺口」是自相矛盾的读数。
    # 处置: **不** max(0, x) 一盖了之(那会把口径不一致伪装成「缺口为 0 = 已覆盖」,
    # 正好掩盖 README 强调的最重要读数)。改为:
    #   - 缺口只在口径可比时(claims >= anchored)给数字;
    #   - 否则显式置 None 并标 basis_mismatch,呈现层印「口径不可比」而不是印一个假数。
    unanchored = total_claims - total_anchored
    basis_mismatch = unanchored < 0
    return {
        "spec_files": len(reports),
        "total_claims": total_claims,
        "anchored_claims": total_anchored,
        "unanchored_claims": None if basis_mismatch else unanchored,
        "unanchored_basis_mismatch": basis_mismatch,
        # 覆盖率同样会因口径不一致 >100%,截顶到 1.0 并靠上面的 flag 说明原因。
        "anchor_coverage": (
            round(min(total_anchored / total_claims, 1.0), 4) if total_claims else 0.0
        ),
        "verdicts": counts,
        "drift_count": counts.get(VERDICT_BROKEN, 0),
        "explicit_anchors": len(explicit),
        "inferred_anchors": len(inferred),
        "explicit_verdicts": verdict_counts(explicit),
        "inferred_verdicts": verdict_counts(inferred),
        "needs_human_review": sum(1 for a in all_anchors if a.needs_human_review),
    }


def render_md(reports: list[SpecReport], summary: dict, repo: Path, issues=None) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# 漂移图 — {repo}")
    add("")
    add("## 总览")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    add(f"| spec 文件数 | {summary['spec_files']} |")
    add(f"| 契约声明总数 | {summary['total_claims']} |")
    add(f"| 已绑锚点 | {summary['anchored_claims']} |")
    if summary.get("unanchored_basis_mismatch"):
        # 不印负数,也不印假的 0。直说口径不可比,并给出该看什么。
        add("| **未绑锚点(覆盖缺口)** | **口径不可比** |")
    else:
        add(f"| **未绑锚点(覆盖缺口)** | **{summary['unanchored_claims']}** |")
    add(f"| 锚点覆盖率 | {summary['anchor_coverage']:.1%} |")
    add("")
    if summary.get("unanchored_basis_mismatch"):
        add("> ⚠ 已绑锚点数 > 契约声明数,覆盖缺口无法计算(会得到负数)。")
        add("> 原因: 声明按**行**数、锚点按**符号**数,一行可抽出多个锚点,两者口径不同源。")
        add("> 若本仓 spec 不是勾选框/编号表格写法,补 `--claim-dialect prose` 让声明计数认得散文体契约;")
        add("> 传了仍不可比,说明该 spec 单行绑了多个锚点,此时应看逐份 spec 表而非总览缺口。")
        add("")
    v = summary["verdicts"]
    add("| 锚点判定 | 条数 |")
    add("|---|---|")
    add(f"| 成立 holds | {v.get(VERDICT_HOLDS, 0)} |")
    add(f"| **失效 broken(=漂移)** | **{v.get(VERDICT_BROKEN, 0)}** |")
    add(f"| 不可自动验 unverifiable | {v.get(VERDICT_UNVERIFIABLE, 0)} |")
    add(f"| 校验器出错 error | {v.get(VERDICT_ERROR, 0)} |")
    add(f"| *其中 holds 但需人工复核* | *{summary.get('needs_human_review', 0)}* |")
    add("")
    add(f"锚点来源: 显式 {summary['explicit_anchors']} / 推断 {summary['inferred_anchors']}"
        f"(推断项由既有写法反推,有误判风险,判定需人工复核)")
    add("")

    cb = summary.get("checkbox") or {}
    if cb.get("checked") is not None:
        tot = cb["checked"] + cb["unchecked"]
        add(f"勾选框: {cb['checked']}/{tot} 已勾({cb['checked_ratio']:.0%})"
            + ("  ← 勾选率低,`[ ]` 不再意味着「没做」" if cb["checked_ratio"] < 0.4 else ""))
        add("")

    if issues:
        add("## spec 内部矛盾(不看代码就能发现)")
        add("")
        for it in issues:
            add(f"- **[{it.kind}]** {it.spec_file} — {it.summary}")
            add(f"  - {it.detail}")
        add("")

    broken = [(r, a) for r in reports for a in r.anchors if a.verdict == VERDICT_BROKEN]
    if broken:
        add("## 漂移明细(broken)")
        add("")
        for r, a in broken:
            add(f"- **{a.spec_file}:{a.spec_line}** [{a.type}] `{a.target}`"
                + (f" :: `{a.claim}`" if a.claim else ""))
            add(f"  - {a.detail}")
        add("")

    review = [(r, a) for r in reports for a in r.anchors if a.needs_human_review]
    if review:
        add("## 判 holds 但需人工复核(锚点存在 ≠ 断言相符)")
        add("")
        add("> 工具只能证明「这个 ID/符号指向真实存在的东西」,不能证明"
            "「它断言的正是 spec 那句话」。**假勾正藏在这里**——")
        add("> 实证: `session-management` 标称 Unit Test-002 验证 getStatus 含 sessionId,"
            "该测试真实存在且通过,但断言的 6 个字段无一是 sessionId。")
        add("")
        for r, a in review:
            add(f"- **{a.spec_file}:{a.spec_line}** [{a.type}] `{a.target}` :: `{a.claim}`")
            add(f"  - {a.detail}")
        add("")

    unver = [(r, a) for r in reports for a in r.anchors if a.verdict == VERDICT_UNVERIFIABLE]
    if unver:
        add("## 不可自动验(需人工观察或锚点粒度不足)")
        add("")
        for r, a in unver:
            add(f"- {a.spec_file}:{a.spec_line} [{a.type}] `{a.target}` — {a.detail}")
        add("")

    add("## 逐份 spec")
    add("")
    add("| spec | 契约声明 | 已绑锚点 | 覆盖率 | 漂移 |")
    add("|---|---:|---:|---:|---:|")
    for r in sorted(reports, key=lambda x: (-x.claims, x.path)):
        if r.claims == 0 and not r.anchors:
            continue
        d = sum(1 for a in r.anchors if a.verdict == VERDICT_BROKEN)
        add(f"| {r.path} | {r.claims} | {r.anchored_claims} | {r.coverage:.0%} | {d} |")
    add("")
    add("> 覆盖率 0% 的 spec 不代表健康,代表**根本没设检查**——")
    add("> 一份零锚点的 spec 在「漂移数」列永远是 0。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="spec 漂移图扫描器")
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--spec-glob", default="specs/**/*.md")
    ap.add_argument("--test-glob", action="append", default=None,
                    help="测试文件 glob,可重复(默认 tests/**/*.ts)")
    ap.add_argument("--src-glob", action="append", default=None,
                    help="生产源码 glob,相对 --repo,可重复。"
                         "不传则用默认 src/ 布局(src/**/*.{ts,js,py})。"
                         "非 src/ 布局的仓必须传,否则工具看不见代码、锚点会全判 broken。"
                         "例: --src-glob 'Stats/exporter/pulse_stats/**/*.py'")
    ap.add_argument("--history-path", action="append", default=None,
                    help="git log 的 pathspec(相对 --repo),可重复,用于判「符号曾否存在」。"
                         "不传则默认 src/ tests/。例: --history-path 'Stats/'")
    ap.add_argument("--claim-dialect", action="append", default=None,
                    choices=["prose"],
                    help="额外启用的「契约声明」识别方言,可重复。"
                         "不传 = 只认 Relay 方言(勾选框 / 带 FR-AC-REQ 编号的表格行 / 粗体 ID),既有仓读数不变。"
                         "`prose` = additionally 认散文体写法:含规范动词(必须/不得/禁止/…)的"
                         "列表项与无编号表格行。散文体 spec(如 PULSE Stats)不传会得到 claims=0、"
                         "覆盖率为零、未绑锚点为负。")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--md", type=Path)
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"仓库不存在: {repo}", file=sys.stderr)
        return 2
    test_globs = args.test_glob or ["tests/**/*.ts", "tests/**/*.js", "test/**/*.py"]

    reports = scan(repo, args.spec_glob, test_globs,
                   src_globs=args.src_glob, history_paths=args.history_path,
                   claim_dialects=set(args.claim_dialect or []))
    summary = summarize(reports)
    issues, cb_stats = check_repo(repo, args.spec_glob)
    summary["checkbox"] = cb_stats
    summary["consistency_issues"] = len(issues)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "repo": str(repo),
                    "summary": summary,
                    "specs": [
                        {
                            "path": r.path, "claims": r.claims,
                            "anchored_claims": r.anchored_claims,
                            "anchors": [a.to_dict() for a in r.anchors],
                        }
                        for r in reports
                    ],
                    "consistency_issues": [i.to_dict() for i in issues],
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    md = render_md(reports, summary, repo, issues)
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(md, encoding="utf-8")
    else:
        print(md)

    print(
        f"\n[scan] spec={summary['spec_files']} claims={summary['total_claims']} "
        f"anchored={summary['anchored_claims']} drift={summary['drift_count']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
