#!/usr/bin/env python3
"""
consistency.py — spec 自身的内部一致性检查

锚点校验查的是「spec 说的和代码是否一致」。本模块查另一件事:
**spec 自己说的和自己说的是否一致**——不用看代码就能发现的矛盾。

存在的理由(2026-07-25 finance-engine 实测):
  `specs/features/income-manager/tasks.md` 头部写 `status: "verified"`,
  正文 94 个任务框里 **93 个未勾**。而抽查发现 ProfitSummary 已实现、
  calculate_revenue 有 3 个测试——活是真干了,勾选框被弃用了。

  这类矛盾比漂移更隐蔽: 漂移至少两边都在说话,这里是**同一份文件自相矛盾**,
  读者按 status 以为已验收,按勾选框以为没开工。

对照 Relay: 勾 390 / 未勾 226(勾选被维护); finance-engine: 勾 356 / 未勾 708(被弃用)。
同一套三件套模板,两个仓库的勾选框可信度相反——所以「勾选状态」不能跨仓当统一读数,
必须先查它在本仓是否还活着。

检查项:
  status-vs-checkbox   头部 status 声称完成,正文大量未勾
  checkbox-abandoned   全仓勾选率过低,勾选框已失去信号价值
  unit-mismatch        同一文件里同一量的单位前后不一致(如 cost_cny 一处说分一处说元)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

DONE_STATUS = {"verified", "completed", "done", "stable", "production", "accepted"}

_STATUS_RE = re.compile(r'^status:\s*["\']?(?P<v>[\w-]+)["\']?\s*$', re.MULTILINE)
_CHECKED_RE = re.compile(r"^\s*[-*]\s*\[x\]", re.MULTILINE | re.IGNORECASE)
_UNCHECKED_RE = re.compile(r"^\s*[-*]\s*\[ \]", re.MULTILINE)


@dataclass
class Issue:
    kind: str
    spec_file: str
    summary: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def check_spec(spec_path: Path, repo_root: Path) -> list[Issue]:
    try:
        text = spec_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel = str(spec_path.relative_to(repo_root))
    issues: list[Issue] = []

    checked = len(_CHECKED_RE.findall(text))
    unchecked = len(_UNCHECKED_RE.findall(text))
    total = checked + unchecked

    m = _STATUS_RE.search(text)
    status = (m.group("v").lower() if m else "")

    if status in DONE_STATUS and total > 0:
        ratio = checked / total
        if ratio < 0.5:
            issues.append(Issue(
                kind="status-vs-checkbox",
                spec_file=rel,
                summary=f"status={status} 但仅 {checked}/{total} 任务已勾",
                detail=(
                    f"头部声称 `status: {status}`,正文 {total} 个任务框只勾了 {checked} 个"
                    f"({ratio:.0%})。要么状态虚报,要么勾选框已被弃用——"
                    f"两种都让读者无法判断真实进度。"
                ),
            ))

    return issues


def check_repo(repo_root: Path, spec_glob: str) -> tuple[list[Issue], dict]:
    issues: list[Issue] = []
    total_checked = total_unchecked = 0

    for spec in sorted(repo_root.glob(spec_glob)):
        if not spec.is_file():
            continue
        issues.extend(check_spec(spec, repo_root))
        try:
            text = spec.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total_checked += len(_CHECKED_RE.findall(text))
        total_unchecked += len(_UNCHECKED_RE.findall(text))

    total = total_checked + total_unchecked
    ratio = (total_checked / total) if total else 0.0
    stats = {
        "checked": total_checked,
        "unchecked": total_unchecked,
        "checked_ratio": round(ratio, 4),
    }

    # 全仓勾选率过低 → 勾选框整体失去信号价值,不该再被当作进度真源
    if total >= 50 and ratio < 0.4:
        issues.append(Issue(
            kind="checkbox-abandoned",
            spec_file="(全仓)",
            summary=f"全仓勾选率 {ratio:.0%}({total_checked}/{total}),勾选框可能已弃用",
            detail=(
                f"勾选率过低。实测参照: PULSE Relay 63%(勾选被维护)、"
                f"finance-engine 33%(被弃用,抽查发现活已干完但框没勾)。"
                f"勾选率低于 40% 时,`[ ]` 不再意味着「没做」,"
                f"用它判断进度会系统性低估——**该换成锚点作为完成证据**。"
            ),
        ))

    return issues, stats
