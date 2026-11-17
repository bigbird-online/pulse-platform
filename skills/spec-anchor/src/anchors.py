#!/usr/bin/env python3
"""
anchors.py — spec 锚点解析与校验

锚点 = spec 里一条契约声明所绑定的、机器可查的东西。
设计依据（2026-07-25 PULSE Relay 采证实证）:

  - 近 90 天 302 次 src 提交中,62.6% 带了 tests,只有 9.3% 带了 specs。
    同一个 Agent、同一套纪律 → 差别不是自觉,是「测试有机器闸,spec 没有」。
    结论: spec 的契约要搭上测试这趟已经在跑的车,而不是另立一套文档纪律。

  - 22 份 spec 已有 26 份引用测试,但只到「文件」粒度
    (`**测试文件**：tests/metering.test.ts`)。文件存在 ≠ 契约成立。
    实证: session-management 标 `[x] Unit Test-002 ... 包含 sessionId(已覆盖,确认)`,
    该测试真实存在且通过,但断言的 6 个字段里没有 sessionId,
    且 ProcessStatusInfo 全部历史版本从未有过该字段 → 引用真实测试的假勾。
    结论: 锚点粒度必须到「断言」,且必须能被独立执行/查证,而不是靠人读。

  - 漂移形态排行: 值 > 接线 > 形状 > 名字。「spec 准确记录打了什么字,
    不准确记录接没接上、设成多少」→ 锚点类型要覆盖「值」和「接线」,
    只绑名字抓不到主要漂移。

五种锚点类型,按「能不能不靠人判断就得出真/假」选取:

  test    — 测试用例名(不是文件)。跑它,绿=契约成立。最强锚点。
  symbol  — 导出符号/接口字段。存在性 + 可选的形状断言。
  const   — 常量或默认值。抓「值漂移」这个第二烈的形态。
  wire    — 接线: 某符号在生产代码里有无调用点/订阅者。抓 metering 那类空接线。
  manual  — 人类可观察现象。**不可自动验**,只登记不判定,并强制要求写明观察步骤。

manual 是刻意保留的诚实出口: 采证实证 32% 的断言是性能/属性类
(「每进程约 250MB」「无跨租户泄漏」),全仓无计时器无指标。
把它们标成 manual 好过假装能自动验——假装可验正是假勾的温床。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

ANCHOR_TYPES = ("test", "symbol", "const", "wire", "manual", "absent")

# spec 正文里的锚点写法(块级):
#   <!-- anchor: test tests/metering.test.ts :: "records result message to daily JSONL file" -->
# 或行内简写:
#   `@anchor test tests/pool.test.ts::"getStatus() returns correct fields"`
_ANCHOR_RE = re.compile(
    r"""(?:<!--\s*anchor:|@anchor)\s+
        (?P<type>test|symbol|const|wire|manual|absent)\s+
        (?P<body>.+?)
        (?:\s*-->|\s*$|\s*`)""",
    re.VERBOSE | re.MULTILINE,
)

VERDICT_HOLDS = "holds"        # 锚点成立
VERDICT_BROKEN = "broken"      # 锚点失效 = 漂移
VERDICT_UNVERIFIABLE = "unverifiable"  # manual,或工具无法判定
VERDICT_ERROR = "error"        # 校验过程本身出错(区别于 broken)


@dataclass
class Anchor:
    """一条 spec 契约绑定的可查证据。"""
    type: str
    raw: str
    spec_file: str
    spec_line: int
    target: str = ""            # 主目标: 文件路径 / 符号名 / 常量名
    claim: str = ""             # 断言: 测试用例名 / 期望值 / 期望接线状态
    verdict: str = ""
    detail: str = ""
    inferred: bool = False      # True = 由既有写法推断,非人明确写下(误判风险更高,分开统计)
    needs_human_review: bool = False  # holds 但只证明了「锚点存在」,未证明「断言相符」

    def to_dict(self) -> dict:
        return asdict(self)


def parse_anchors(spec_path: Path, repo_root: Path) -> list[Anchor]:
    """从一份 spec 文件里抽出所有锚点声明。"""
    try:
        text = spec_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [
            Anchor(
                type="manual", raw=str(exc),
                spec_file=str(spec_path.relative_to(repo_root)),
                spec_line=0, verdict=VERDICT_ERROR,
                detail=f"读取失败: {exc}",
            )
        ]

    anchors: list[Anchor] = []
    for match in _ANCHOR_RE.finditer(text):
        line_no = text[: match.start()].count("\n") + 1
        body = match.group("body").strip().rstrip("`").strip()
        atype = match.group("type")
        target, claim = _split_body(atype, body)
        anchors.append(
            Anchor(
                type=atype,
                raw=match.group(0).strip(),
                spec_file=str(spec_path.relative_to(repo_root)),
                spec_line=line_no,
                target=target,
                claim=claim,
            )
        )
    return anchors


def _split_body(atype: str, body: str) -> tuple[str, str]:
    """把锚点正文拆成 target 与 claim。分隔符 `::`,缺省时整体为 target。"""
    if "::" in body:
        target, claim = body.split("::", 1)
        return target.strip(), claim.strip().strip('"').strip("'")
    if atype == "const" and "=" in body:
        target, claim = body.split("=", 1)
        return target.strip(), claim.strip()
    return body.strip(), ""


# ---------------------------------------------------------------- 校验器


def verify(
    anchor: Anchor,
    repo_root: Path,
    test_index: dict[str, set[str]] | None,
    src_globs: list[str] | None = None,
    test_globs: list[str] | None = None,
    history_paths: list[str] | None = None,
) -> Anchor:
    """就地判定一条锚点,写入 verdict/detail。

    src_globs / test_globs / history_paths 为仓库方言参数,全部缺省时行为与
    参数化之前逐字节一致(默认 `src/` 布局 + `git log -- src/ tests/`)。
    """
    try:
        if anchor.type == "test":
            _verify_test(anchor, repo_root, test_index)
        elif anchor.type == "symbol":
            _verify_symbol(anchor, repo_root, src_globs, test_globs, history_paths)
        elif anchor.type == "const":
            _verify_const(anchor, repo_root, src_globs)
        elif anchor.type == "wire":
            _verify_wire(anchor, repo_root, src_globs)
        elif anchor.type == "absent":
            _verify_absent(anchor, repo_root, src_globs, test_globs)
        elif anchor.type == "manual":
            anchor.verdict = VERDICT_UNVERIFIABLE
            anchor.detail = "人工观察项,工具不判定" + (f"; 观察步骤: {anchor.claim}" if anchor.claim else "; **缺观察步骤**")
        else:
            anchor.verdict = VERDICT_ERROR
            anchor.detail = f"未知锚点类型: {anchor.type}"
    except Exception as exc:  # 校验器自身出错必须与「漂移」区分
        anchor.verdict = VERDICT_ERROR
        anchor.detail = f"校验器异常: {type(exc).__name__}: {exc}"
    return anchor


def _verify_test(anchor: Anchor, repo_root: Path, test_index: dict[str, set[str]] | None) -> None:
    """测试锚点: 文件要在,且**用例名要真的存在于该文件**。

    只查文件存在是不够的——采证实证的假勾正是「文件在、测试通过、但不测这件事」。
    """
    target = anchor.target.strip()

    # spec 没点名测试文件时(target 是目录/空),不能凭空指定宿主——
    # 早期实现把 target 落成字面量 "tests/",读出 0 个用例,于是把「全仓有无此标签」
    # 误判成「该文件未采用约定」,直接漏掉了 session-management 那条真假勾。
    # 正确做法: 无宿主时在**全部测试文件**里找这个 ID。
    # 点名了具体测试文件但文件不存在 → 真漂移(如 b346287 删了测试,spec 未同步)
    if target.endswith((".ts", ".js", ".py", ".swift")) and not (repo_root / target).is_file():
        anchor.verdict = VERDICT_BROKEN
        anchor.detail = f"spec 引用的测试文件不存在: {target}"
        return

    if not target or target.rstrip("/") in ("tests", "test") or not (repo_root / target).is_file():
        if anchor.claim and test_index:
            for fpath, names_ in test_index.items():
                hit = [n for n in names_ if anchor.claim in n]
                if hit:
                    # 能力边界(诚实声明): 锚点解析只能证明「这个 ID 指向一个真实存在的测试」,
                    # **不能**证明该测试断言的正是 spec 那句话。2026-07-25 实证:
                    # session-management 标「Unit Test-002 验证 getStatus 包含 sessionId」,
                    # [Unit Test-002] 确实存在且通过,但它断言的 6 个字段无一是 sessionId
                    # (且该字段全部历史版本从未存在)。这类「引用真实测试的假勾」
                    # 需要理解散文语义,超出机器判定范围 → 判 holds 但强制标注待人工复核。
                    anchor.verdict = VERDICT_HOLDS
                    anchor.needs_human_review = True
                    anchor.detail = (
                        f"ID 指向真实用例: {Path(fpath).name} :: {hit[0]!r}。"
                        f"**仅证明该 ID 存在,未证明该测试断言的正是本条 spec 声明**"
                        f"(假勾无法机检,需人工比对断言内容)"
                    )
                    return
            # 全仓都没有: 若约定本身在用(别处有带标签的用例),这就是断了的引用
            # 只有当 claim 本身是 [ID] 形态、且该约定在本仓确实被使用时,
            # 「全仓找不到」才构成假勾。pytest 函数名(test_xxx)不走这条——
            # 找不到就是「未实现/已改名」,由下面的通用分支处理。
            is_id_claim = bool(re.match(r"^\[(?:Unit|Integration|E2E)\s+Test-", anchor.claim))
            convention_anywhere = is_id_claim and any(
                re.search(r"\[(?:Unit|Integration|E2E)\s+Test-", n)
                for names_ in test_index.values() for n in names_
            )
            if convention_anywhere:
                anchor.verdict = VERDICT_BROKEN
                anchor.detail = (
                    f"**假勾嫌疑**: spec 声称 {anchor.claim} 已覆盖,但全仓 "
                    f"{len(test_index)} 个测试文件中无任何用例带此 ID"
                    f"(该 [ID] 标签约定在本仓确实被使用) → 断言未被任何测试支撑"
                )
                return
        # 点名了具体测试函数(pytest 方言)但全仓不存在 → 测试未落地,是可行动的读数
        if anchor.claim and re.match(r"^test_[a-z0-9_]+$", anchor.claim):
            anchor.verdict = VERDICT_BROKEN
            anchor.detail = (
                f"**测试未落地**: spec 声称由 {anchor.claim}() 验证,"
                f"但全仓 {len(test_index or {})} 个测试文件中无此函数"
            )
            return
        anchor.verdict = VERDICT_UNVERIFIABLE
        anchor.detail = f"spec 未点名具体测试文件(target={target!r}),无法定位宿主 → 覆盖缺口"
        return

    path = (repo_root / target).resolve()
    if not anchor.claim:
        anchor.verdict = VERDICT_UNVERIFIABLE
        anchor.detail = f"仅绑到文件粒度,未指定用例名 → 无法判定契约是否成立(文件存在: {target})"
        return

    names = (test_index or {}).get(str(path), None)
    if names is None:
        names = _extract_test_names(path)

    if anchor.claim in names:
        anchor.verdict = VERDICT_HOLDS
        anchor.detail = f"用例存在于 {target}"
        return

    # 容错: 允许 spec 写用例名的子串(测试名常带 [ID] 后缀)
    partial = [n for n in names if anchor.claim in n or n in anchor.claim]
    if partial:
        anchor.verdict = VERDICT_HOLDS
        anchor.detail = f"用例名部分匹配: {partial[0]!r}"
        return

    # 关键区分(2026-07-25 实测踩到):
    #   若该测试文件**整体就没采用 [ID] 标签约定**,那么 spec 里的 Test-ID 只是
    #   spec 内部编号,从未与测试建立绑定 → 这是「未绑定」(覆盖缺口),不是「绑了但断了」。
    #   若该文件**用了这套约定**(别的 ID 能对上),唯独这条对不上 → 才是真漂移/假勾。
    # 把两者都判成 broken 会制造大批误报:bot-commands 的 8 条 Unit Test-00x
    # 全属前者,而 session-management 的 Unit Test-002 属后者(pool.test.ts 确实带标签)。
    convention_used = any(re.search(r"\[(?:Unit|Integration|E2E)\s+Test-", n) for n in names)
    if not convention_used and re.match(r"^\[(?:Unit|Integration|E2E)\s+Test-", anchor.claim or ""):
        anchor.verdict = VERDICT_UNVERIFIABLE
        anchor.detail = (
            f"spec 用了 {anchor.claim} 编号,但 {target} 未采用 [ID] 标签约定"
            f"({len(names)} 个用例无一带标签) → **该断言从未与测试绑定**,属覆盖缺口而非漂移"
        )
        return

    anchor.verdict = VERDICT_BROKEN
    anchor.detail = (
        f"文件存在且已采用 [ID] 标签约定,但**无此用例**: {anchor.claim!r} 不在 {target} 的 "
        f"{len(names)} 个用例中 → 疑似假勾(引用真实测试但该测试不测此事)"
    )


# JS/TS: it('name') / describe("name")
_TEST_NAME_RE = re.compile(r"""\b(?:it|test|describe)\s*\(\s*(['"`])(?P<name>.+?)\1""", re.DOTALL)
# Python/pytest: def test_name(...)  —— 用例名就是函数名,不是字符串
_PY_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>test_[A-Za-z0-9_]*)\s*\(", re.MULTILINE)
# Swift/XCTest: func testName() [throws|async throws] { —— 同样是函数名即用例名。
# 2026-08-01 实测 Stats PulseStatsTests/ 共 170 个此形态用例;不认这条方言,
# Swift 侧的 test 锚点全部落到「找不到宿主」分支。
_SWIFT_TEST_DEF_RE = re.compile(
    r"^\s*(?:@\w+\s+)*(?:public\s+|private\s+|internal\s+)?func\s+(?P<name>test[A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)


def _extract_test_names(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    names = {m.group("name").strip() for m in _TEST_NAME_RE.finditer(text)}
    names |= {m.group("name").strip() for m in _PY_TEST_DEF_RE.finditer(text)}
    if path.suffix == ".swift":
        names |= {m.group("name").strip() for m in _SWIFT_TEST_DEF_RE.finditer(text)}
    return names


def build_test_index(repo_root: Path, test_globs: list[str]) -> dict[str, set[str]]:
    """预扫所有测试文件的用例名,避免逐锚点重复读盘。"""
    index: dict[str, set[str]] = {}
    for pattern in test_globs:
        for path in repo_root.glob(pattern):
            if path.is_file():
                index[str(path.resolve())] = _extract_test_names(path)
    return index


# 源码行索引: 一次读盘,全部锚点共用。
# 早期实现是每条锚点 spawn 一次 ripgrep,80 份 spec × 数百锚点 → 分钟级不返回。
# 源码只有 2.4 万行,全量读进内存做正则远比反复 spawn 进程便宜。
_SOURCE_CACHE: dict[tuple[str, tuple[str, ...]], list[tuple[str, int, str]]] = {}

# 生产代码(判「接线」用,必须排除测试——测试里的调用不算生产接线)
# **仓库方言**: 默认值是 Relay/finance-engine 那种 `src/` 布局。别的仓不长这样
# (2026-08-01 实测 Stats: Python 在 exporter/pulse_stats/、Swift 在 PulseStats/,
# 且 spec 与代码跨目录分居)→ 53 条锚点 100% 判 broken,抽样误报率 87.5%。
# 根因不是判定逻辑,是**工具看不见代码**。故 glob 一路参数化到 CLI(--src-glob),
# 不传则沿用下面的默认值,既有仓读数不变。
_SRC_GLOBS = ["src/**/*.ts", "src/**/*.js", "src/**/*.py"]
# 全部代码(判「符号是否存在」用,含测试)
_TEST_CODE_GLOBS = ["tests/**/*.py", "tests/**/*.ts", "tests/**/*.js", "test/**/*.py"]
_CODE_GLOBS = _SRC_GLOBS + _TEST_CODE_GLOBS
# `git log` 的 pathspec(判「符号曾否存在」用)。同样是方言,同样参数化(--history-path)。
_HISTORY_PATHS = ["src/", "tests/"]


def _code_globs(src_globs: list[str] | None, test_globs: list[str] | None = None) -> list[str]:
    """由生产代码 glob 派生「符号是否存在」的搜索范围(生产 + 测试)。

    symbol 锚点要含测试代码: spec 里点名的常是**计划中的测试函数**,只搜生产码会误报。
    默认路径(src_globs 为空)完全走老的 _CODE_GLOBS 常量,读数一字不变。
    自定义 src_globs 时,测试目录多半也不在默认位置 —— 用调用方传下来的
    test_globs(即 CLI 已有的 --test-glob),而不是硬拼 `tests/**/*.py`。
    """
    if not src_globs:
        return _CODE_GLOBS
    return list(src_globs) + list(test_globs or _TEST_CODE_GLOBS)


def build_source_index(repo_root: Path, src_globs: list[str]) -> list[tuple[str, int, str]]:
    """返回 [(相对路径, 行号, 行文本)],缓存到进程内。按 glob 组合分别缓存。"""
    key = (str(repo_root), tuple(src_globs))
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    rows: list[tuple[str, int, str]] = []
    for pattern in src_globs:
        for path in repo_root.glob(pattern):
            if not path.is_file() or "node_modules" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(path.relative_to(repo_root))
            for i, line in enumerate(text.splitlines(), 1):
                rows.append((rel, i, line))
    _SOURCE_CACHE[key] = rows
    return rows


def _grep(repo_root: Path, pattern: str, globs: list[str] | None = None) -> list[str]:
    """在源码索引里查,返回 `path:line:text` 形式的命中行。"""
    rows = build_source_index(repo_root, globs or _SRC_GLOBS)
    rx = re.compile(pattern)
    return [f"{p}:{n}:{t}" for p, n, t in rows if rx.search(t)]


# 倒排索引: 标识符 → 命中行号集合。
# 取代「每个符号全表扫一遍」——profile 实测 Relay 476 个 symbol 锚点 × 48k 行
# = 2286 万次 re.search = 53 秒,占全程 95%。
# 建索引本身只需把每行分词一次,之后查符号是 O(1)。
_TOKEN_INDEX: dict[tuple[str, tuple[str, ...]], dict[str, list[int]]] = {}


def _token_index(repo_root: Path, globs: list[str]) -> dict[str, list[int]]:
    key = (str(repo_root), tuple(globs))
    if key in _TOKEN_INDEX:
        return _TOKEN_INDEX[key]
    rows = build_source_index(repo_root, globs)
    idx: dict[str, list[int]] = {}
    for i, (_p, _n, text) in enumerate(rows):
        for tok in set(_TOKEN_RE.findall(text)):
            idx.setdefault(tok, []).append(i)
    _TOKEN_INDEX[key] = idx
    return idx


def _grep_symbol(repo_root: Path, symbol: str, globs: list[str] | None = None) -> list[str]:
    """按**完整标识符**查符号命中行。等价于 _grep(rf"\\b{symbol}\\b") 但走倒排索引。

    只适用于「符号是一个完整标识符」的场景(symbol/const/wire 三类锚点都是)。
    需要正则语义的地方仍走 _grep。
    """
    g = globs or _SRC_GLOBS
    rows = build_source_index(repo_root, g)
    hits = _token_index(repo_root, g).get(symbol, ())
    return [f"{rows[i][0]}:{rows[i][1]}:{rows[i][2]}" for i in hits]


_LINE_COMMENT = {
    ".py": ("#",),
    ".ts": ("//",), ".js": ("//",), ".tsx": ("//",), ".jsx": ("//",),
    ".swift": ("//",),
    ".sh": ("#",), ".bash": ("#",), ".yaml": ("#",), ".yml": ("#",),
    ".go": ("//",), ".rs": ("//",), ".java": ("//",), ".c": ("//",), ".h": ("//",),
}
# 块注释成对符号。Python 的三引号既是块注释(docstring)也是字符串,归到字符串处理。
_BLOCK_COMMENT = {
    ".ts": ("/*", "*/"), ".js": ("/*", "*/"), ".tsx": ("/*", "*/"), ".jsx": ("/*", "*/"),
    ".swift": ("/*", "*/"), ".go": ("/*", "*/"), ".rs": ("/*", "*/"),
    ".java": ("/*", "*/"), ".c": ("/*", "*/"), ".h": ("/*", "*/"),
}

HIT_CODE = "code"
HIT_COMMENT = "comment"
HIT_STRING = "string"


def _segments(line: str, ext: str) -> list[tuple[int, int, str]]:
    """把一行切成 [(起, 止, 种类)] 区段,种类 ∈ code/comment/string。

    单趟扫描,状态机只认三种转移: 进字符串 / 进行注释 / 进块注释起止。
    刻意不处理的边界(都会让结果偏向判 code,即偏红,不会造成假绿):
      - 转义引号 \\" 会提前结束字符串段
      - 正则字面量 /foo/ 不认
      - f-string 里的 {表达式} 整体算字符串
    """
    line_marks = _LINE_COMMENT.get(ext, ())
    blk = _BLOCK_COMMENT.get(ext)
    segs: list[tuple[int, int, str]] = []
    i, n = 0, len(line)
    start, kind = 0, HIT_CODE

    def flush(to: int, k: str) -> None:
        if to > start:
            segs.append((start, to, k))

    while i < n:
        ch = line[i]
        # 字符串: 三引号优先(Python docstring),再单双引号与反引号
        if ch in "\"'`":
            quote = line[i:i + 3] if line[i:i + 3] in ('"""', "'''") else ch
            flush(i, kind)
            j = line.find(quote, i + len(quote))
            end = n if j < 0 else j + len(quote)   # 未闭合 → 到行尾都算字符串
            segs.append((i, end, HIT_STRING))
            i = end
            start, kind = i, HIT_CODE
            continue
        # 行注释: 到行尾
        if any(line.startswith(m, i) for m in line_marks):
            flush(i, kind)
            segs.append((i, n, HIT_COMMENT))
            return segs
        # 块注释起
        if blk and line.startswith(blk[0], i):
            flush(i, kind)
            j = line.find(blk[1], i + len(blk[0]))
            end = n if j < 0 else j + len(blk[1])
            segs.append((i, end, HIT_COMMENT))
            i = end
            start, kind = i, HIT_CODE
            continue
        i += 1
    flush(n, kind)
    return segs


def classify_hit(line: str, symbol: str, ext: str, in_block: bool = False) -> str:
    """这一行里 symbol 出现的位置属于 code / comment / string。

    符号在一行里可能出现多次(`FOO = 1  # FOO 的说明`)。
    判据: **只要有任何一次出现在 code 段,就算 code**——
    代码命中是实打实的,不该被同行的注释稀释。
    """
    if in_block:
        return HIT_COMMENT
    segs = _segments(line, ext)
    best = None
    for m in re.finditer(rf"(?<![\w]){re.escape(symbol)}(?![\w])", line):
        pos = m.start()
        for s, e, k in segs:
            if s <= pos < e:
                if k == HIT_CODE:
                    return HIT_CODE
                best = k if best is None else best
                break
    # 符号没被 \b 边界匹配上(如它是更长标识符的一部分)→ 交给上层按原逻辑处理
    return best or HIT_CODE


def _block_comment_lines(repo_root: Path, rel_path: str, ext: str) -> set[int]:
    """预扫一个文件,返回「整行落在多行块注释内部」的行号集合。

    单看一行认不出 `/* ... */` 中间那些行,必须按文件扫。
    Python 的三引号 docstring 同理(它在语法上是字符串,但用途是注释,
    对本工具而言两者都属「不是活代码」,统一按块处理)。
    """
    blk = _BLOCK_COMMENT.get(ext)
    triple = ext == ".py"
    if not blk and not triple:
        return set()
    try:
        text = (repo_root / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    inside: set[int] = set()
    open_tok = None
    for n, line in enumerate(text.splitlines(), 1):
        if open_tok is None:
            if triple:
                for q in ('"""', "'''"):
                    c = line.count(q)
                    if c % 2 == 1:                       # 开了没闭
                        open_tok = q
                        break
            if open_tok is None and blk:
                oi = line.find(blk[0])
                if oi >= 0 and line.find(blk[1], oi + len(blk[0])) < 0:
                    open_tok = blk[1]
        else:
            inside.add(n)                                 # 收尾行本身仍算注释内
            if open_tok in line:
                open_tok = None
    return inside


def classify_hits(repo_root: Path, hits: list[str], symbol: str) -> dict[str, list[str]]:
    """把 `path:line:text` 命中行按 code/comment/string 分组。"""
    out: dict[str, list[str]] = {HIT_CODE: [], HIT_COMMENT: [], HIT_STRING: []}
    blk_cache: dict[str, set[int]] = {}
    for h in hits:
        parts = h.split(":", 2)
        if len(parts) < 3:
            out[HIT_CODE].append(h)
            continue
        rel, lineno, text = parts[0], parts[1], parts[2]
        ext = Path(rel).suffix
        if rel not in blk_cache:
            blk_cache[rel] = _block_comment_lines(repo_root, rel, ext)
        try:
            in_block = int(lineno) in blk_cache[rel]
        except ValueError:
            in_block = False
        out[classify_hit(text, symbol, ext, in_block)].append(h)
    return out


_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
# snake_case 符号若所在行是 shell 代码块场景 / URL 路径参数 / JSON 字段表,
# 大概率不是 TS/JS 源码里该存在的符号——而是 shell 变量名、REST 路径占位符、
# 字段名文档。TS 代码库的真实符号不会用 snake_case(camelCase 是约定)。
# 2026-07-29 Pulse 实测采证: 43 条 symbol 类误报里 10 条属此族
#   (push_now 是 shell 函数、task_id 是 `GET /a2a/task/{task_id}` 的路径参数)。
_SHELL_CTX_RE = re.compile(r"^\s{4,}\S|^\s*```|_curl_|_relay_|os\.environ|\$\{|^\s*#")
_URL_PATH_RE = re.compile(r"\{[a-z][a-z0-9_]*\}|^\s*\|\s*`?(?:GET|POST|PUT|DELETE|PATCH)\s")
_JSON_FIELD_RE = re.compile(r"^\s*[-*]\s*`?\w+\s*[`:：]")


def _verify_symbol(
    anchor: Anchor,
    repo_root: Path,
    src_globs: list[str] | None = None,
    test_globs: list[str] | None = None,
    history_paths: list[str] | None = None,
) -> None:
    """符号锚点: 某符号在源码中存在。claim 非空时额外要求同处出现该字符串(形状断言)。

    搜索范围含测试代码: spec 里点名的常常是**计划中的测试函数**
    (finance-engine 实测 `test_discrepancy_creation` 这类),只搜 src/ 会误报。
    """
    sym = anchor.target
    hits = _grep_symbol(repo_root, sym, _code_globs(src_globs, test_globs))
    if not hits:
        # snake_case + 上下文像 shell/URL路径/字段表 → 大概率根本不是本仓语言的符号,
        # 判定它「未落地」没有意义(它压根不该在 src/ 里出现)。降级为 unverifiable,
        # 而不是让读者去纠结「要不要补实现」这个伪问题。
        line = anchor.raw or ""
        if _SNAKE_CASE_RE.match(sym) and (
            _SHELL_CTX_RE.search(line) or _URL_PATH_RE.search(line) or _JSON_FIELD_RE.match(line)
        ):
            anchor.verdict = VERDICT_UNVERIFIABLE
            anchor.detail = (
                f"符号 {sym} 是 snake_case 且所在行像 shell 代码 / URL 路径参数 / 字段表,"
                f"非本仓源码符号(TS/JS 约定 camelCase) → 不判定为漂移或未落地"
            )
            return
        # 区分两种「不存在」——处置方式完全不同:
        #   曾存在后被删 → 真漂移,spec 落后于代码,该改 spec
        #   从未存在     → 设计未落地,spec 描述的是没造出来的东西,该改代码或降级 spec
        # 实测: persistJobs 有 3 次历史提交(真漂移),updateJob/stopAgent 等 0 次(从未落地)。
        ever = _ever_existed(repo_root, sym, history_paths=history_paths)
        # 默认方言下措辞保持与参数化之前逐字一致(既有报告/断言不受影响);
        # 只有显式传了 history_paths 才在 detail 里点明真实搜索范围。
        scope = " ".join(history_paths) if history_paths else "src/"
        anchor.verdict = VERDICT_BROKEN
        if ever:
            anchor.detail = f"**真漂移**: 符号 {sym} 曾存在于 {scope} 但已被移除,spec 未同步"
        else:
            anchor.detail = (
                f"**设计未落地**: 符号 {sym} 在 {scope} 不存在,且 git 全history 从未出现"
                f" → spec 描述了未实现的设计(非漂移,需决定是补实现还是降级 spec)"
            )
        return
    if not anchor.claim:
        anchor.verdict = VERDICT_HOLDS
        anchor.detail = f"符号存在({len(hits)} 处命中)"
        return
    shape_hits = [h for h in hits if anchor.claim in h]
    if shape_hits:
        anchor.verdict = VERDICT_HOLDS
        anchor.detail = f"符号存在且形状匹配: {shape_hits[0][:120]}"
    else:
        anchor.verdict = VERDICT_BROKEN
        anchor.detail = f"符号存在但形状断言不成立(期望含 {anchor.claim!r},{len(hits)} 处命中均不含)"


_EVER_CACHE: dict[tuple[str, str, tuple[str, ...]], bool] = {}
# 历史标识符全集: 一次 git 扫描建好,所有符号共用。
# 取代「每个缺失符号跑一次 git log -S」——后者在大 monorepo 上是 O(符号数 × 全history),
# 实测 finance-engine 单次 30.8s × 37 个 = 9 分钟,直接让"拷走即跑"超时。
_HIST_TOKENS: dict[tuple[str, tuple[str, ...]], set[str] | None] = {}
# 标识符分词: 3 字符起的 ASCII 标识符。Swift 与 Python/TS 的标识符字符集相同
# (字母/数字/下划线,首字符非数字),故同一条正则对 .swift 一样适用;
# Swift 特有的反引号转义标识符(`default`)极罕见,不为它复杂化。
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _history_tokens(
    repo_root: Path, timeout: float = 90.0, history_paths: list[str] | None = None
) -> set[str] | None:
    """本项目代码文件在 git 历史中出现过的全部标识符。

    实现: `git log -p -- <history_paths>` 扫一遍新增/删除行,分词入集合。
    只跑一次,之后每个符号是 O(1) 的 set 查询。
    返回 None 表示扫描失败/超时 —— 调用方据此标注"无法判定",而不是当成"不存在"。

    pathspec 是**相对 cwd**(见 _git_prefix 的踩坑记录),cwd 即 repo_root,
    所以 Stats 那种「代码在 Stats/ 但 git 根是 PULSE」的布局,
    传 `Stats/` 这类相对 repo_root 的路径即可。
    """
    paths = list(history_paths) if history_paths else list(_HISTORY_PATHS)
    key = (str(repo_root), tuple(paths))
    if key in _HIST_TOKENS:
        return _HIST_TOKENS[key]
    tokens: set[str] | None = None
    try:
        proc = subprocess.run(
            ["git", "log", "-p", "--no-color", "--no-merges", "--", *paths],
            cwd=repo_root, capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode == 0:
            tokens = set()
            for line in proc.stdout.splitlines():
                if not line or line[0] not in "+-":
                    continue
                if line.startswith(("+++", "---")):
                    continue
                tokens.update(_TOKEN_RE.findall(line))
    except Exception:
        tokens = None
    _HIST_TOKENS[key] = tokens
    return tokens


_GITROOT_CACHE: dict[str, str | None] = {}


def _git_prefix(repo_root: Path) -> str | None:
    """repo_root 相对 git 仓根的前缀;仓根即 repo_root 时返回 ''。

    **注意 git pathspec 的真实语义**(2026-07-25 实测澄清): 当 `cwd` 设为子目录时,
    pathspec 是**相对 cwd** 的,不是相对仓根。所以嵌套仓(如 Relay 嵌在 PULSE 内)
    传裸 `src/` 是**正确**的;反而给它拼上 `Relay/` 前缀会变成
    `Relay/Relay/src/` 而匹配不到。

    本函数保留是为了在 detail 里说明搜索范围,以及给 `:/` 绝对式留出扩展位;
    **不用它拼 pathspec**。

    踩坑记录: 重写实证分身报「pathspec 缺陷导致真漂移全被误判」,我在 PULSE 仓根
    跑 `git log -- src/` 得 0 就采信了——但那是 cwd 不同造成的,在 Relay 目录下
    跑同一条命令得 3。**分身的否定性断言同样要在正确条件下复算**,
    换个 cwd 就能证伪的结论不算证据。
    """
    key = str(repo_root)
    if key in _GITROOT_CACHE:
        return _GITROOT_CACHE[key]
    prefix: str | None = None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_root, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            top = Path(proc.stdout.strip()).resolve()
            rr = repo_root.resolve()
            if rr == top:
                prefix = ""
            elif top in rr.parents:
                prefix = str(rr.relative_to(top)) + "/"
    except Exception:
        prefix = None
    _GITROOT_CACHE[key] = prefix
    return prefix


def _ever_existed(
    repo_root: Path, symbol: str, timeout: float = 8.0,
    history_paths: list[str] | None = None,
) -> bool:
    """该符号是否在 git 历史中出现过。用于区分「被删」与「从未有过」。

    性能: `git log --all -S` 在大 monorepo 上极慢——实测 Coevolution 仓
    单次 30.8 秒,37 个缺失符号 = 9 分钟,让"拷走即跑"直接超时。
    故加两道约束:
      1. `--all` 改为只扫当前分支历史(绝大多数删除都在主线上)
      2. 硬超时,超时视作「查不出」而非「不存在」,并在 detail 里标明不确定
    宁可少断言,也不要拖死或误判。
    """
    paths = list(history_paths) if history_paths else list(_HISTORY_PATHS)
    key = (str(repo_root), symbol, tuple(paths))
    if key in _EVER_CACHE:
        return _EVER_CACHE[key]

    # 走一次性建好的历史标识符全集(O(1) 查询);拿不到再退回逐符号 git log。
    toks = _history_tokens(repo_root, history_paths=paths)
    if toks is not None:
        if symbol in toks:
            _EVER_CACHE[key] = True
            return True
        # 精确命中失败时再做一次子串匹配,与 `git log -S` 的语义对齐。
        # 实测 spec 写 `maxRateLimitRetries`,代码里的真实标识符是
        # `maxRateLimitRetriesPerMessage`——纯精确匹配会把「曾存在后被删」
        # 误判成「从未落地」。子串扫 8.5k 个 token 仍是毫秒级。
        result = any(symbol in t for t in toks)
        _EVER_CACHE[key] = result
        return result

    result = False
    try:
        proc = subprocess.run(
            ["git", "log", "-S", symbol, "--oneline", "--", *paths],
            cwd=repo_root, capture_output=True, text=True, timeout=timeout,
        )
        result = bool(proc.stdout.strip())
    except Exception:
        result = False
    _EVER_CACHE[key] = result
    return result


_ASSIGN_FORMS = [
    r"^\s*{sym}\s*(?::[^=]*)?=(?!=)",          # FOO = 1 / FOO: int = 1
    r"^\s*(?:const|let|var)\s+{sym}\b",         # const FOO = / let FOO
    r"^\s*(?:def|func|fn|function)\s+{sym}\b",  # def FOO( / func FOO(
    r"^\s*(?:class|struct|enum|protocol|interface|type)\s+{sym}\b",
    r"^\s*(?:static\s+|final\s+|public\s+|private\s+)*(?:let|var)\s+{sym}\b",
    r"^\s*{sym}\s*:\s*\w",                      # 对象字面量/字典键形态 FOO: value
]


def _is_assignment_site(body: str, sym: str) -> bool:
    """这一行是不是在给 sym 赋值 / 定义它(而非仅仅提到它)。"""
    esym = re.escape(sym)
    return any(re.search(p.format(sym=esym), body) for p in _ASSIGN_FORMS)


def _verify_absent(
    anchor: Anchor,
    repo_root: Path,
    src_globs: list[str] | None = None,
    test_globs: list[str] | None = None,
) -> None:
    """删除锚点: 断言某符号**不该再存在于代码里**。

    上游 Ken 2026-08-04 落码,2026-08-08 移植进本副本(wi_20260805_616)。
    立此类的根因是我 2026-08-01 报的设计缺口: 五类锚点全是存在性断言,
    没有一类能说「这东西不该再存在」→ 删除类清理拿不到机器闸保护。
    我实测删掉 6 个死配置(TIER_ACT_LABELS / TIER_ESCALATE_COUNT=20 / MAX_ALERTS=20 …),
    死因可溯(2026-07-28 大鸟定「不用老化功能」),但没有任何闸能防它们被照着
    仍在漂的旧 spec 加回来,只能落成 manual + 靠人跑 grep。

    **本副本与上游的唯一差异(移植时的实质改写,不是拷贝)**: 取数口径走
    `_code_globs(src_globs, test_globs)` 而非上游的模块级常量 `_CODE_GLOBS`,
    并把 src_globs/test_globs 从 verify() 一路透传进来。上游那个常量在本副本
    根本不存在(我这边是方言参数化的函数)——2026-08-05 我核出这一点时,
    上游给的「只需新增三样、不碰你的方言路径」不成立。
    同理不用 `_apply_scope`/`anchor.scope`(那是 P3,本轮决定暂不合)。

    claim(可选): 写明为什么该删,会原样带进 detail 供人复核。
    """
    sym = anchor.target
    hits = _grep_symbol(repo_root, sym, _code_globs(src_globs, test_globs))
    if not hits:
        anchor.verdict = VERDICT_HOLDS
        anchor.detail = f"确认已删除: {sym} 在代码中零命中"
        return

    # 先滤掉注释/字符串——留档注释里怀念它不算复活(依赖 P0 的命中行分类)。
    groups = classify_hits(repo_root, hits, sym)
    code_hits = groups[HIT_CODE]

    # 再从活代码里挑「真在赋值/定义它」的行。仅仅被读取/引用不算复活——
    # 那说明还有残留调用点,是另一个问题(且会被 symbol/wire 锚点抓到)。
    revived = [h for h in code_hits if _is_assignment_site(h.split(":", 2)[-1], sym)]

    if revived:
        anchor.verdict = VERDICT_BROKEN
        anchor.detail = (
            f"**已删除的东西回来了**: {sym} 在代码中重新出现定义/赋值"
            f"({len(revived)} 处)。首处: {revived[0].strip()[:140]}"
            + (f"; 依据: {anchor.claim}" if anchor.claim else "")
        )
        return

    if code_hits:
        # 有活代码提到它但没人定义它 —— 多半是残留的引用(读一个已不存在的名字),
        # 或是同名孪生。不判 broken(它确实没被重新定义),但必须让人看一眼。
        anchor.verdict = VERDICT_HOLDS
        anchor.needs_human_review = True
        anchor.detail = (
            f"未见重新定义,但仍有 {len(code_hits)} 处活代码提到 {sym}"
            f"(可能是残留引用或同名符号)。首处: {code_hits[0].strip()[:120]}"
        )
        return

    # 命中全在注释/字符串里 = 留档怀念,不算复活。
    c, s = len(groups[HIT_COMMENT]), len(groups[HIT_STRING])
    where = " + ".join(w for w in (f"{c} 处注释" if c else "", f"{s} 处字符串" if s else "") if w)
    anchor.verdict = VERDICT_HOLDS
    anchor.detail = (
        f"确认已删除: {sym} 无活代码命中,仅 {where}(留档注释不算复活)"
        + (f"; 依据: {anchor.claim}" if anchor.claim else "")
    )


def _verify_const(anchor: Anchor, repo_root: Path, src_globs: list[str] | None = None) -> None:
    """常量锚点: 抓「值漂移」——采证实证的第三烈形态(timeout 12×/20× 失真)。"""
    name = anchor.target
    expected = anchor.claim
    hits = _grep_symbol(repo_root, name, src_globs)
    if not hits:
        anchor.verdict = VERDICT_BROKEN
        anchor.detail = f"常量/配置项在 src/ 中不存在: {name}"
        return
    if not expected:
        anchor.verdict = VERDICT_UNVERIFIABLE
        anchor.detail = f"未声明期望值,只能确认符号存在({len(hits)} 处)"
        return
    for hit in hits:
        # 取 name 之后的片段找字面值,避免匹配到行首无关数字
        idx = hit.find(name)
        tail = hit[idx + len(name):] if idx >= 0 else hit
        if re.search(rf"(?<![\w.]){re.escape(expected)}(?![\w.])", tail):
            anchor.verdict = VERDICT_HOLDS
            anchor.detail = f"值匹配: {hit.strip()[:140]}"
            return
    anchor.verdict = VERDICT_BROKEN
    anchor.detail = (
        f"**值漂移**: 期望 {name}={expected},但 {len(hits)} 处命中均无此值。"
        f" 首处: {hits[0].strip()[:140]}"
    )


def _verify_wire(anchor: Anchor, repo_root: Path, src_globs: list[str] | None = None) -> None:
    """接线锚点: 某符号在生产代码(src/,排除定义文件与测试)里有无真实调用点。

    直接对应 metering 那条实证: checkBalance 有定义、有测试、零生产调用点。
    claim 语法: `wired`(默认,要求有调用点) / `unwired`(要求确实没有,用于登记已知空接线)
    """
    sym = anchor.target
    want = (anchor.claim or "wired").lower()
    hits = _grep_symbol(repo_root, sym, src_globs)

    # 剔除「定义/声明/导出」行,剩下的才算调用点。
    # 这些排除模式是踩出来的(见 test_unwired_symbol_detected):
    #   TS interface 里的方法简写 `checkBalance(id: string): Promise<boolean>;`
    #   长得和调用一模一样,必须靠「有类型标注的形参 + 以 ; 收尾」把它认出来。
    esym = re.escape(sym)
    decl_patterns = [
        rf"\bfunction\s+{esym}\b",                    # function checkBalance(
        rf"\b(const|let|var)\s+{esym}\b",             # const checkBalance =
        rf"{esym}\s*[?]?\s*:\s*\(",                   # checkBalance?: (a) => b
        # 接口方法简写 `name(a: T): R;` —— 形参可能自带嵌套括号(回调类型
        # `handler: (event: E) => void`),所以不能用 [^)]* 匹配形参,
        # 改判特征: 有类型标注的形参 + 行尾是 `): 类型;`(声明,不是调用)。
        # 漏了这条会把 onLowBalance 的声明误当调用点,导致零订阅者被判 holds。
        rf"{esym}\s*\(.*:\s*.*\)\s*:\s*[\w<>\[\]| ]+\s*[;{{]",
        rf"^\s*(export\s+)?(interface|type)\b",       # 整行是接口/类型声明
    ]
    call_like = []
    for h in hits:
        body = h.split(":", 2)[-1]
        if any(re.search(p, body) for p in decl_patterns):
            continue
        # `return { checkBalance };` 这类导出/装配也不算调用点
        if re.search(rf"[{{,]\s*{esym}\s*[,}}]", body):
            continue
        if re.search(rf"\b{esym}\s*\(", body) or re.search(rf"\.\s*{esym}\b", body):
            call_like.append(h)

    if want == "unwired":
        if call_like:
            anchor.verdict = VERDICT_BROKEN
            anchor.detail = f"声明为未接线,但发现 {len(call_like)} 处疑似调用点: {call_like[0].strip()[:120]}"
        else:
            anchor.verdict = VERDICT_HOLDS
            anchor.detail = "确认无生产调用点(与声明一致)"
        return

    if call_like:
        anchor.verdict = VERDICT_HOLDS
        anchor.detail = f"有 {len(call_like)} 处生产调用点,首处: {call_like[0].strip()[:120]}"
    elif not hits:
        # 一处都没命中 = 符号根本不在 src/,与「有定义但没接」是两回事
        anchor.verdict = VERDICT_BROKEN
        anchor.detail = f"符号 {sym} 在 src/ 中完全不存在(非空接线,是缺失)"
    else:
        anchor.verdict = VERDICT_BROKEN
        anchor.detail = (
            f"**空接线**: {sym} 在 src/ 有定义但**无生产调用点**"
            f"(总命中 {len(hits)} 处,均为定义/声明) → 契约声称已接,实际未接"
        )


def verdict_counts(anchors: list[Anchor]) -> dict[str, int]:
    counts = {VERDICT_HOLDS: 0, VERDICT_BROKEN: 0, VERDICT_UNVERIFIABLE: 0, VERDICT_ERROR: 0}
    for a in anchors:
        counts[a.verdict] = counts.get(a.verdict, 0) + 1
    return counts
