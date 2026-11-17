"""Read recorded canonical WI Outcomes; never re-run acceptance or mutate SCL.

A registered Outcome is an observation of an existing verdict, not a new review.
Only the canonical WI receipt is imported; supporting Markdown and task-level
receipts are not additional WI Outcomes. Optional prose is privacy-filtered.
"""
from __future__ import annotations
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping
from .events import parse_timestamp, utc_text
from .receipts import Receipt, ReceiptError, reference

FEATURE = "wi_outcome_context.v1"
CONTEXT_SCHEMA = "recorded_wi_outcome/v1"
CHECKS = {"baseline", "mechanism", "identity", "primary", "counterexample", "regression", "consumer"}
MAX_BYTES = 1024 * 1024


def display_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    # Never transport prompts, credential-shaped values or raw private paths.
    if re.search(r"(?:sk-[A-Za-z0-9_-]{12,}|(?:api[_ -]?key|oauth_token|authorization)\s*[:=]|Bearer\s+\S+)", value, re.I):
        return None
    value = re.sub(r"(?:/Users/|/home/|/root/|/private/|/tmp/|/Volumes/)[^\s`）)\]]+", "[私有路径]", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value).strip()
    return value if len(value) <= limit else value[:limit-1] + "…"


def parse_recorded_outcome(raw: bytes, wi: str, observed: str) -> Receipt:
    """Validate recorded v2 structure without dereferencing private evidence."""
    import yaml
    if len(raw) > MAX_BYTES or not re.fullmatch(r"wi_\d{8}_\d{3}", wi):
        raise ReceiptError("invalid canonical Outcome identity or size")
    match = re.fullmatch(r"---\s*\n(.*?)\n---\s*\n(.*)", raw.decode("utf-8"), re.S)
    if not match:
        raise ReceiptError("canonical Outcome frontmatter is missing")
    try:
        data = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise ReceiptError("canonical Outcome frontmatter is invalid") from exc
    if not isinstance(data, Mapping) or data.get("schema") != "verify-work-outcome/v2" or data.get("claim_id") != wi:
        raise ReceiptError("canonical Outcome schema or WI differs")
    for name in ("claim", "producer", "verifier", "verified_at", "acceptance_fingerprint"):
        if not isinstance(data.get(name), str) or not data[name].strip():
            raise ReceiptError("canonical Outcome identity is incomplete")
    fingerprint = data["acceptance_fingerprint"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
        raise ReceiptError("canonical Outcome acceptance identity is invalid")
    at = utc_text(parse_timestamp(data["verified_at"], "Outcome verified_at"))
    if parse_timestamp(at, "Outcome time") > parse_timestamp(observed, "observed_at"):
        raise ReceiptError("canonical Outcome is from the future")
    status, level = data.get("status"), data.get("verification_level")
    if status not in {"accepted", "partial", "rejected", "blocked"} or level not in {"independent", "human", "self_with_controls"}:
        raise ReceiptError("invalid recorded Outcome verdict")
    if data.get("change_type") not in {"runtime", "rule", "document", "data", "mixed"} or data.get("risk") not in {"low", "medium", "high"}:
        raise ReceiptError("invalid recorded Outcome scope")
    target, checks = data.get("target"), data.get("checks")
    if not isinstance(target, Mapping) or any(not isinstance(target.get(k), str) or not target[k] for k in ("environment", "revision", "consumer")):
        raise ReceiptError("canonical Outcome target is invalid")
    if not isinstance(checks, Mapping) or set(checks) != CHECKS:
        raise ReceiptError("canonical Outcome checks are incomplete")
    refs = []
    for name in ("change_set", "acceptance_source", "residual_risks"):
        values = data.get(name)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ReceiptError("canonical Outcome declarations are incomplete")
    for check in checks.values():
        if not isinstance(check, Mapping) or check.get("result") not in {"pass", "fail", "blocked", "not_applicable"} or not check.get("note"):
            raise ReceiptError("invalid recorded Outcome check")
        evidence = check.get("evidence", [])
        if not isinstance(evidence, list) or any(not isinstance(e, str) or not e for e in evidence) or (check["result"] == "pass" and not evidence):
            raise ReceiptError("recorded Outcome check lacks references")
        refs.extend(reference("outcome_evidence", value) for value in evidence)
    if any(heading not in match[2] for heading in ("## 声明边界", "## 验收矩阵", "## 结论")):
        raise ReceiptError("canonical Outcome body is incomplete")
    if type(data.get("unresolved_high_risk")) is not bool:
        raise ReceiptError("Outcome risk declaration must be a boolean")
    if status == "accepted" and (level == "self_with_controls" or data["producer"] == data["verifier"]
            or data.get("unresolved_high_risk") is not False or any(c["result"] != "pass" for c in checks.values())):
        raise ReceiptError("recorded accepted Outcome lacks its required checks")
    # Retain the native declaration identity. This hash does not claim that
    # runtime artifacts have just been verified or approve a different candidate.
    candidate = reference("candidate", [fingerprint, target["revision"], data["change_set"]])
    body = match[2].split("## 结论", 1)[1].split("\n## ", 1)[0].strip()
    return Receipt.make("delivery", wi, {
        "wi": wi, "responsible": data["producer"], "candidate_ref": candidate,
        "artifact_ref": reference("artifact", data["change_set"]),
        "acceptance_ref": reference("acceptance", fingerprint), "status": status,
        "accepted_at": at if status == "accepted" else None,
        "outcome_context": {"schema": CONTEXT_SCHEMA, "title": display_text(data["claim"], 240),
            "summary": display_text(body, 1200), "verification_level": level},
    }, occurred_at=at, observed_at=observed, producer="canonical_outcome.v1",
        evidence=tuple(sorted(set(refs + [reference("outcome_source", sha256(raw).hexdigest())]))))


def read_canonical_outcomes(root: Path, observed: str, into: list[Receipt], detail: dict | None) -> list[Path]:
    """Inspect direct canonical filenames only, not nested evidence copies."""
    folder = root / "evidence/outcomes"
    if (root / "evidence").is_symlink() or folder.is_symlink():
        raise ReceiptError("unsafe canonical Outcome directory")
    files = []
    if not folder.is_dir():
        return files
    for path in sorted(folder.glob("wi_*.md")):
        if not re.fullmatch(r"wi_\d{8}_\d{3}", path.stem):
            continue
        if detail is not None:
            detail["files_seen"] += 1
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ReceiptError("unsafe canonical Outcome file")
            raw = path.read_bytes()
            files.append(path)
            if detail is not None:
                detail["files_read"] += 1
            into.append(parse_recorded_outcome(raw, path.stem, observed))
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            if detail is None:
                raise
            detail["files_invalid"] += 1
            errors = detail["errors"]
            errors["invalid_native_record"] = errors.get("invalid_native_record", 0) + 1
    return files
