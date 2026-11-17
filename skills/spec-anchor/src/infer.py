#!/usr/bin/env python3
"""
infer.py — 从既有 spec 里推断隐式锚点

存在的理由: 一个只认新语法的工具,在存量 spec 上读数永远是 0,
而 PULSE 恰恰是「22 份既有 spec 半准」的存量场景。要求先把 80 份 spec
按新语法重写一遍才能体检,等于要求先治好病才能看病。

所以扫描器必须能**读懂 spec 已经在用的写法**。2026-07-25 实测 Relay
现有 spec 中天然存在四种隐式锚点:

  1. `**测试文件**：\\`tests/metering.test.ts\\``     → test 锚点(文件粒度)
  2. `**Unit Test-002**：<描述>`                      → test 锚点(用例粒度,靠 [ID] 标签回连)
  3. 反引号包的 `someFunction()`                       → symbol 锚点
  4. 表格行 `| P0-001 | <任务> | [x] |`                → 任务 ID,回连测试名里的 [P0-001]

推断出的锚点标 `inferred=True`,在报告里与显式锚点分开统计——
推断有误判风险,不能和人明确写下的锚点混为一谈。
"""

from __future__ import annotations

import re
from pathlib import Path

from anchors import Anchor

# 1. **测试文件**：`tests/xxx.test.ts`
_TEST_FILE_RE = re.compile(
    r"\*\*测试文件\*\*[：:]\s*`?(?P<path>tests?/[\w./-]+\.(?:test|spec)\.[jt]s)`?"
)
# 裸引用: `tests/xxx.test.ts`
_BARE_TEST_RE = re.compile(r"`(?P<path>tests?/[\w./-]+\.(?:test|spec)\.[jt]s)`")

# 2. **Unit Test-002**：描述   /   - [x] **Unit Test-002**：描述
_TEST_CASE_RE = re.compile(
    r"\*\*(?P<id>(?:Unit|Integration|E2E)\s+Test-[\w.]+)\*\*[：:]\s*(?P<desc>[^\n]+)"
)

# 2b. 另一种方言(finance-engine 实测): `Unit Test: \`test_load_cost_config()\` - 说明`
# 推断层若只认单一仓库的写法,换个项目覆盖率就塌(实测 48.3% → 5.8%)。
# 锚点思想是通用的,**写法必须按仓库方言适配**——这是本工具最需要移植成本的部分。
_TEST_FUNC_RE = re.compile(
    r"(?:Unit|Integration|E2E)\s*Test\s*[:：]\s*`(?P<fn>[A-Za-z_][\w]*)\s*\(\)`"
)

# 2c. 裸测试函数名 `test_xxx()` —— pytest 约定
_PYTEST_FN_RE = re.compile(r"`(?P<fn>test_[a-z0-9_]+)\s*\(\)`")

# 3. 反引号里的函数调用 `foo()` 或 `Obj.foo()`
_SYMBOL_RE = re.compile(r"`(?P<sym>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)\(\)`")

# 3b. 裸符号: 没加反引号但明显是标识符的 camelCase / snake_case。
# 实测 Relay 23.4% 的声明属此类(handleStart / listIds / calculateCost 等,
# 逐个查证代码里都真实存在)——它们本来就可锚,只是写的人没加反引号。
# 这说明「60% 是散文绑不上」是伪结论: 大部分是**写法没到位**,不是内容不可锚。
_BARE_SYMBOL_RE = re.compile(
    r"(?<![`\w.])(?P<sym>[a-z][a-zA-Z0-9]{3,}[A-Z][a-zA-Z0-9]*|[a-z][a-z0-9]{2,}_[a-z0-9_]+)(?![`\w])"
)
# 中文语境里的常见误命中(不是标识符)
_BARE_STOPWORDS = {
    "javascript", "typescript", "changelog", "workspace", "timestamp",
    "username", "hostname", "filename", "pathname", "endpoint", "runtime",
    "playbook", "codebase", "baseline", "rollback", "fallback",
}

# 4. 表格里的任务 ID
_TASK_ID_RE = re.compile(r"^\s*\|\s*(?P<id>(?:P[0-9T]*|FR|AC|T)[-–][\w.-]+)\s*\|", re.MULTILINE)

# 常量/默认值: `FOO = 8` 或 `foo: 300`(带单位后缀也认)
_CONST_RE = re.compile(
    r"`(?P<name>[A-Z][A-Z0-9_]{3,}|[a-z][A-Za-z0-9]*(?:Timeout|Retries|Limit|Max|Min))`"
    r"\s*(?:=|为|默认|:)\s*`?(?P<val>\d+(?:\.\d+)?)"
)

# 反例: 明确写「未实现/待实现/计划」的行不推断为契约锚点
_TODO_HINT = re.compile(r"(待实现|未实现|计划|TODO|尚未|N/A)")


def infer_anchors(spec_path: Path, repo_root: Path) -> list[Anchor]:
    """从一份既有 spec 推断隐式锚点。返回的 Anchor 均带 inferred 标记。"""
    try:
        text = spec_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel = str(spec_path.relative_to(repo_root))
    out: list[Anchor] = []
    seen: set[tuple[str, str, str]] = set()

    def _line_of(pos: int) -> int:
        return text[:pos].count("\n") + 1

    def _line_text(pos: int) -> str:
        start = text.rfind("\n", 0, pos) + 1
        end = text.find("\n", pos)
        return text[start: end if end != -1 else len(text)]

    def _add(a: Anchor) -> None:
        key = (a.type, a.target, a.claim)
        if key in seen:
            return
        seen.add(key)
        a.raw = f"[inferred] {a.raw}"
        a.inferred = True
        out.append(a)

    # --- 2 先跑: 用例级锚点比文件级强,优先 ---
    # 把 Test-ID 与测试文件关联: 同一份 spec 里出现的测试文件作为候选宿主
    host_files = [m.group("path") for m in _TEST_FILE_RE.finditer(text)]
    host_files += [m.group("path") for m in _BARE_TEST_RE.finditer(text)]
    host = host_files[0] if host_files else ""

    for m in _TEST_CASE_RE.finditer(text):
        line = _line_text(m.start())
        if _TODO_HINT.search(line):
            continue
        # 只对已勾选的做推断——未勾选的本就不声称成立
        if re.search(r"\[\s\]", line):
            continue
        tid = m.group("id").strip()
        _add(Anchor(
            type="test", raw=line.strip()[:160], spec_file=rel,
            spec_line=_line_of(m.start()),
            target=host or "tests/",
            claim=f"[{tid}]",   # 回连测试名里的 [Unit Test-002] 标签
        ))

    # --- 2b/2c. 测试函数名(pytest 方言) ---
    for rx in (_TEST_FUNC_RE, _PYTEST_FN_RE):
        for m in rx.finditer(text):
            line = _line_text(m.start())
            if _TODO_HINT.search(line):
                continue
            _add(Anchor(
                type="test", raw=line.strip()[:160], spec_file=rel,
                spec_line=_line_of(m.start()),
                target="tests/", claim=m.group("fn"),
            ))

    # --- 1. 测试文件级 ---
    for m in list(_TEST_FILE_RE.finditer(text)) + list(_BARE_TEST_RE.finditer(text)):
        line = _line_text(m.start())
        if _TODO_HINT.search(line):
            continue
        _add(Anchor(
            type="test", raw=line.strip()[:160], spec_file=rel,
            spec_line=_line_of(m.start()),
            target=m.group("path"), claim="",
        ))

    # --- 3. 符号 ---
    for m in _SYMBOL_RE.finditer(text):
        line = _line_text(m.start())
        if _TODO_HINT.search(line):
            continue
        sym = m.group("sym").split(".")[-1]
        if len(sym) < 4:
            continue
        # 测试函数名已由上面的 test 锚点覆盖(且判定更强:能定位宿主文件)。
        # 不跳过会让同一条断言产出两个锚点,漂移数翻倍虚高。
        if sym.startswith("test_"):
            continue
        _add(Anchor(
            type="symbol", raw=line.strip()[:160], spec_file=rel,
            spec_line=_line_of(m.start()), target=sym, claim="",
        ))

    # --- 3b. 裸符号(仅在契约声明行上找,避免扫到散文段落) ---
    # 只在**已勾选**的声明行上找: 未勾项没声称任何事成立,
    # 里头的标识符常是测试入参名(addJob(invalidCron) / removeJob(id, wrongOwner)),
    # 不是对代码的契约断言。对未勾行推断会把"计划中的测试场景"误报成"符号缺失"。
    _CLAIM_LINE = re.compile(r"^\s*(?:[-*]\s*\[[xX]\]|\|)")
    for m in _BARE_SYMBOL_RE.finditer(text):
        line = _line_text(m.start())
        if not _CLAIM_LINE.match(line) or _TODO_HINT.search(line):
            continue
        sym = m.group("sym")
        if sym.lower() in _BARE_STOPWORDS or sym.startswith("test_"):
            continue
        # 行内出现在 `foo(bar)` 形参位置的,是调用示例的入参,不是被断言的符号
        if re.search(rf"\w+\s*\([^)]*\b{re.escape(sym)}\b[^)]*\)", line):
            continue
        _add(Anchor(
            type="symbol", raw=line.strip()[:160], spec_file=rel,
            spec_line=_line_of(m.start()), target=sym, claim="",
        ))

    # --- 5. 常量值 ---
    for m in _CONST_RE.finditer(text):
        line = _line_text(m.start())
        if _TODO_HINT.search(line):
            continue
        _add(Anchor(
            type="const", raw=line.strip()[:160], spec_file=rel,
            spec_line=_line_of(m.start()),
            target=m.group("name"), claim=m.group("val"),
        ))

    for a in out:
        a.detail = "由既有写法推断"
    return out
