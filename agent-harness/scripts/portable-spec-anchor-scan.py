#!/usr/bin/env python3
"""Portable explicit-anchor scanner used only when the canonical PULSE tool is absent.

The shared CollaborateWithAI repository can be checked out without a nested
PULSE repository.  The normal guard still prefers PULSE's canonical scanner;
this fallback prevents a missing sibling tool from turning contract drift into
a false green/fail-open in standalone CI.

It intentionally implements only the explicit anchor dialect used by Harness
specs: symbol, test, absent, const, wire, and manual.  Manual anchors are
reported as unverifiable but are not drift, matching the purpose of an explicit
manual gate.  All other enforced anchors fail closed when their claimed target
cannot be mechanically supported.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path
import re
from typing import Iterable


ANCHOR_RE = re.compile(
    r"@anchor\s+(symbol|test|absent|const|wire|manual)\s+([^`\n]+)"
)
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\.]*$")


@dataclass(frozen=True)
class Anchor:
    spec: str
    line: int
    kind: str
    target: str
    claim: str


@dataclass(frozen=True)
class Verdict:
    anchor: Anchor
    verdict: str
    reason: str


def _matches(path: str, patterns: list[str]) -> bool:
    """Match repo-relative paths with globstar allowing zero directories.

    ``fnmatch`` treats ``**`` like ``*`` and therefore ``a/**/*.py`` misses
    direct children such as ``a/models.py``.  The guard intentionally uses
    globstar patterns, so evaluate the original pattern plus progressively
    collapsed ``/**/`` variants.
    """
    for pattern in patterns:
        candidate = pattern
        while True:
            if fnmatch.fnmatch(path, candidate):
                return True
            if "/**/" not in candidate:
                break
            candidate = candidate.replace("/**/", "/", 1)
    return False


def _repo_files(root: Path, patterns: list[str]) -> list[Path]:
    result: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if _matches(rel, patterns):
                result.append(path)
    return sorted(result)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _parse_anchors(spec_path: Path, root: Path) -> list[Anchor]:
    text = _read_text(spec_path)
    result: list[Anchor] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in ANCHOR_RE.finditer(line):
            kind = match.group(1)
            payload = match.group(2).strip()
            if "::" in payload:
                target, claim = (part.strip() for part in payload.split("::", 1))
            else:
                target, claim = payload, ""
            result.append(
                Anchor(
                    spec=spec_path.relative_to(root).as_posix(),
                    line=line_no,
                    kind=kind,
                    target=target,
                    claim=claim,
                )
            )
    return result


def _token_occurrences(texts: Iterable[str], target: str) -> int:
    if not target:
        return 0
    if IDENT_RE.fullmatch(target):
        leaf = target.rsplit(".", 1)[-1]
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(leaf)}(?![A-Za-z0-9_])")
        return sum(len(pattern.findall(text)) for text in texts)
    return sum(text.count(target) for text in texts)


def _path_claim_supported(root: Path, target: str, claim: str) -> bool:
    path = root / target
    if not path.is_file():
        return False
    if not claim:
        return True
    text = _read_text(path)
    normalized = " ".join(claim.split())
    if normalized and normalized in " ".join(text.split()):
        return True
    # Path-scoped test anchors often describe the assertion in prose after ::.
    # Require at least one meaningful claim token to be present rather than
    # accepting file existence alone.
    tokens = [
        token.casefold()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|\d+", claim)
        if token.casefold() not in {"the", "and", "with", "from", "when", "then"}
    ]
    lowered = text.casefold()
    return bool(tokens) and any(token in lowered for token in tokens)


def _const_supported(texts: list[str], target: str, claim: str) -> bool:
    if _token_occurrences(texts, target) == 0:
        return False
    if not claim:
        return True
    claim_tokens = [token for token in re.findall(r"[-+]?[0-9]+(?:\.[0-9]+)?|true|false|none|[A-Za-z_][A-Za-z0-9_]*", claim, re.I)]
    if not claim_tokens:
        return False
    leaf = target.rsplit(".", 1)[-1]
    for text in texts:
        for match in re.finditer(re.escape(leaf), text):
            window = text[max(0, match.start() - 160): match.end() + 240].casefold()
            if all(token.casefold() in window for token in claim_tokens[:2]):
                return True
    return False


def evaluate(
    anchor: Anchor,
    *,
    root: Path,
    source_texts: list[str],
    test_texts: list[str],
) -> Verdict:
    all_texts = source_texts + test_texts
    if anchor.kind == "manual":
        return Verdict(anchor, "unverifiable", "explicit manual gate")

    if anchor.kind == "absent":
        count = _token_occurrences(all_texts, anchor.target)
        if count == 0:
            return Verdict(anchor, "supported", "target remains absent")
        return Verdict(anchor, "broken", f"absent target occurs {count} time(s)")

    if anchor.kind == "test":
        if "/" in anchor.target or anchor.target.endswith((".py", ".ts")):
            ok = _path_claim_supported(root, anchor.target, anchor.claim)
        else:
            ok = _token_occurrences(test_texts, anchor.target) > 0
        return Verdict(
            anchor,
            "supported" if ok else "broken",
            "test evidence found" if ok else "test evidence not found",
        )

    if anchor.kind == "symbol":
        ok = _token_occurrences(all_texts, anchor.target) > 0
        return Verdict(
            anchor,
            "supported" if ok else "broken",
            "symbol found" if ok else "symbol not found",
        )

    if anchor.kind == "const":
        ok = _const_supported(all_texts, anchor.target, anchor.claim)
        return Verdict(
            anchor,
            "supported" if ok else "broken",
            "constant claim found" if ok else "constant claim not supported",
        )

    if anchor.kind == "wire":
        count = _token_occurrences(all_texts, anchor.target)
        if anchor.claim.casefold().startswith("unwired"):
            ok = count >= 1
            reason = "explicitly frozen as unwired" if ok else "wire target not found"
        else:
            ok = count >= 2
            reason = "definition and wiring occurrences found" if ok else "wiring occurrence missing"
        return Verdict(anchor, "supported" if ok else "broken", reason)

    return Verdict(anchor, "error", f"unsupported anchor kind {anchor.kind}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--spec-glob", action="append", default=[])
    parser.add_argument("--src-glob", action="append", default=[])
    parser.add_argument("--test-glob", action="append", default=[])
    parser.add_argument("--history-path", action="append", default=[])
    parser.add_argument("--json", required=True)
    parser.add_argument("--md", required=True)
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    spec_files = _repo_files(root, args.spec_glob)
    source_files = _repo_files(root, args.src_glob)
    test_files = _repo_files(root, args.test_glob)
    source_texts = [_read_text(path) for path in source_files]
    test_texts = [_read_text(path) for path in test_files]

    anchors = [
        anchor
        for spec_path in spec_files
        for anchor in _parse_anchors(spec_path, root)
    ]
    verdicts = [
        evaluate(
            anchor,
            root=root,
            source_texts=source_texts,
            test_texts=test_texts,
        )
        for anchor in anchors
    ]
    counts = Counter(item.verdict for item in verdicts)
    drift = counts.get("broken", 0)
    payload = {
        "summary": {
            "spec_files": len(spec_files),
            "anchors": len(anchors),
            "drift_count": drift,
            "verdicts": {
                "supported": counts.get("supported", 0),
                "broken": counts.get("broken", 0),
                "unverifiable": counts.get("unverifiable", 0),
                "error": counts.get("error", 0),
            },
            "scanner": "portable-explicit-anchor-v1",
        },
        "verdicts": [
            {
                "spec": item.anchor.spec,
                "line": item.anchor.line,
                "kind": item.anchor.kind,
                "target": item.anchor.target,
                "claim": item.anchor.claim,
                "verdict": item.verdict,
                "reason": item.reason,
            }
            for item in verdicts
        ],
    }
    json_path = Path(args.json)
    md_path = Path(args.md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Portable Harness spec-anchor report",
        "",
        f"- spec_files: {len(spec_files)}",
        f"- anchors: {len(anchors)}",
        f"- drift_count: {drift}",
        f"- errors: {counts.get('error', 0)}",
        "",
    ]
    for item in verdicts:
        if item.verdict in {"broken", "error"}:
            lines.append(
                f"- {item.verdict}: {item.anchor.spec}:{item.anchor.line} "
                f"{item.anchor.kind} {item.anchor.target} — {item.reason}"
            )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
