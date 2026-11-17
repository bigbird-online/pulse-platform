"""Bounded usage extraction from the authenticated native session's fixed bytes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path
from typing import Any

from .model import ArtifactInput, Json, Rejected, digest, fields

MAX_RANGE = 16 * 1024 * 1024
COUNTERS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
DETAIL_COUNTERS = ("reasoning", "cacheWrite1h")
COST_FIELDS = {"input", "output", "cacheRead", "cacheWrite", "total", "currency"}


def cursor(path: Path) -> int:
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        window = min(size, 65536)
        while window:
            stream.seek(size - window)
            data = stream.read(window)
            newline = data.rfind(b"\n")
            if newline >= 0:
                return size - window + newline + 1
            if window >= min(size, MAX_RANGE):
                break
            window = min(size, MAX_RANGE, window * 2)
    raise Rejected(
        "source_unavailable", "Native session has no bounded complete record"
    )


def boundaries(path: Path, start: int = 0) -> Json:
    if type(start) is not int or start < 0:
        raise Rejected("invalid_input", "Usage range starts at a nonnegative byte")
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if start > size:
            raise Rejected(
                "source_unavailable", "Session is shorter than this boundary"
            )
        stream.seek(start)
        content = stream.read(MAX_RANGE)
    end = content.rfind(b"\n") + 1
    return {
        "start_byte": start,
        "end_byte": start + end,
        "observed_size": size,
        "incomplete_tail_bytes": len(content) - end,
        "has_more": start + len(content) < size,
    }


def counter(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise Rejected(
            "source_invalid", "Native usage counter is not an exact nonnegative integer"
        )
    return value


def amount(value: Any) -> float:
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or not 0 <= value <= 10**12
    ):
        raise Rejected(
            "source_invalid", "Native cost is not a finite nonnegative amount"
        )
    return float(value)


def extract(raw: bytes) -> list[Json]:
    groups: dict[str, Json] = {}
    try:
        for line in raw.splitlines():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row")
            message = row.get("message")
            if (
                row.get("type") != "message"
                or not isinstance(message, dict)
                or message.get("role") != "assistant"
            ):
                continue
            configuration = {
                key: message.get(key) for key in ("provider", "model", "api")
            }
            if any(
                value is not None
                and (not isinstance(value, str) or not 1 <= len(value) <= 256)
                for value in configuration.values()
            ):
                raise ValueError("configuration")
            # Public configuration is a digest, not a provider account/session identity.
            key = digest(configuration)
            group = groups.setdefault(
                key,
                {
                    "configuration": key,
                    "messages": 0,
                    "observed_messages": 0,
                    "tokens": {
                        name: {"value": None, "observed_messages": 0}
                        for name in COUNTERS
                    },
                    "costs": {},
                },
            )
            group["messages"] += 1
            usage = message.get("usage")
            if usage is None:
                continue
            fields(usage, set(), set(COUNTERS + DETAIL_COUNTERS) | {"cost"})
            group["observed_messages"] += 1
            for name in DETAIL_COUNTERS:
                if name in usage:
                    group["tokens"].setdefault(
                        name, {"value": None, "observed_messages": 0}
                    )
            for name in COUNTERS + DETAIL_COUNTERS:
                if name in usage and usage[name] is not None:
                    observed = counter(usage[name])
                    item = group["tokens"][name]
                    item["value"] = (
                        observed if item["value"] is None else item["value"] + observed
                    )
                    item["observed_messages"] += 1
            cost = usage.get("cost")
            if cost is not None:
                fields(cost, set(), COST_FIELDS)
                for name in COST_FIELDS - {"currency"}:
                    if name in cost and cost[name] is not None:
                        amount(cost[name])
                currency = cost.get("currency")
                if currency is not None:
                    if (
                        not isinstance(currency, str)
                        or len(currency) != 3
                        or not currency.isascii()
                        or not currency.isupper()
                        or not currency.isalpha()
                    ):
                        raise ValueError("currency")
                    if cost.get("total") is not None:
                        item = group["costs"].setdefault(
                            currency, {"value": 0.0, "observed_messages": 0}
                        )
                        item["value"] += amount(cost["total"])
                        item["observed_messages"] += 1
        for group in groups.values():
            for item in [*group["tokens"].values(), *group["costs"].values()]:
                item["coverage"] = (
                    "unavailable"
                    if item["observed_messages"] == 0
                    else "complete"
                    if item["observed_messages"] == group["messages"]
                    else "partial"
                )
            if not group["costs"]:
                group["cost_coverage"] = "unavailable"
            else:
                group["cost_coverage"] = (
                    "complete"
                    if sum(
                        item["observed_messages"] for item in group["costs"].values()
                    )
                    == group["messages"]
                    else "partial"
                )
        return list(groups.values())
    except Rejected as exc:
        if exc.code != "invalid_input":
            raise
        raise Rejected("source_invalid", "Native usage schema is unsupported") from exc
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Rejected(
            "source_invalid", "Native usage format is unsupported or incomplete"
        ) from exc


def extraction(groups: list[Json]) -> str:
    # Previously accepted ranges must retain the exact extraction packet and
    # request identity. Detail-bearing ranges were rejected by the v1 reader.
    version = 2 if any(set(g["tokens"]) & set(DETAIL_COUNTERS) for g in groups) else 1
    return f"deterministic-native-pi-jsonl/v{version}"


def coverage(groups: list[Json]) -> str:
    if not groups or not any(
        any(item["observed_messages"] for item in group["tokens"].values())
        or any(item["observed_messages"] for item in group["costs"].values())
        for group in groups
    ):
        return "unavailable"
    return (
        "complete"
        if all(
            all(item["coverage"] == "complete" for item in group["tokens"].values())
            and group["cost_coverage"] == "complete"
            for group in groups
        )
        else "partial"
    )


class SourceRejected(Rejected):
    """A refused capture with a fixed, content-free cause for the caller.

    The cause names what to fix; it never carries session bytes or paths.
    """

    CAUSES = frozenset(
        {
            "invalid_range",
            "source_missing",
            "path_mismatch",
            "owner_mismatch",
            "header_invalid",
            "header_identity_mismatch",
            "range_not_row_boundary",
            "incomplete_tail",
            "record_invalid",
        }
    )

    def __init__(self, cause: str, code: str, reason: str):
        assert cause in self.CAUSES
        super().__init__(code, reason)
        self.cause = cause


def session_identity(header: Any) -> tuple[str, str] | None:
    """Pi session identity is (type, id); other header fields are descriptive.

    Session discovery admits a Pi log by exactly these fields, and the Host may
    register only them. version, timestamp and cwd describe the session.
    """
    if not isinstance(header, dict):
        return None
    kind, native_id = header.get("type"), header.get("id")
    if kind != "session" or not isinstance(native_id, str) or not native_id:
        return None
    return kind, native_id


def same_session(expected: Any, observed: Any) -> bool:
    identity = session_identity(expected)
    return identity is not None and identity == session_identity(observed)


def capture(
    path: Path, expected_header: Json, start: int, end: int
) -> tuple[ArtifactInput, Json]:
    if (
        any(type(value) is not int for value in (start, end))
        or start < 0
        or end <= start
        or end - start > MAX_RANGE
    ):
        raise SourceRejected(
            "invalid_range",
            "invalid_input",
            "Usage requires a bounded nonempty byte range",
        )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise SourceRejected(
            "source_missing", "source_unavailable", "Original native session is unavailable"
        ) from exc
    except OSError as exc:
        raise SourceRejected(
            "path_mismatch", "authority_denied", "Native session path is redirected"
        ) from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or Path(path).resolve() != Path(path):
            raise SourceRejected(
                "path_mismatch",
                "authority_denied",
                "Native session path is not its original regular file",
            )
        if info.st_uid != os.getuid():
            raise SourceRejected(
                "owner_mismatch",
                "authority_denied",
                "Native session is owned by another account",
            )
        first = stream.readline()
        try:
            header = json.loads(first) if first.endswith(b"\n") else None
        except (ValueError, UnicodeError):
            header = None
        if not isinstance(header, dict):
            raise SourceRejected(
                "header_invalid", "source_invalid", "Native session header is unreadable"
            )
        if not same_session(expected_header, header):
            raise SourceRejected(
                "header_identity_mismatch",
                "source_invalid",
                "Original native session identity changed",
            )
        if start:
            stream.seek(start - 1)
            if stream.read(1) != b"\n":
                raise SourceRejected(
                    "range_not_row_boundary",
                    "invalid_input",
                    "Usage range must start at an original row boundary",
                )
        stream.seek(start)
        raw = stream.read(end - start)
    if len(raw) != end - start or not raw.endswith(b"\n"):
        raise SourceRejected(
            "incomplete_tail",
            "source_unavailable",
            "Usage range is truncated; preserve its original boundary",
        )
    artifact = ArtifactInput("native/usage/range", f"{start}-{end}", raw, "private")
    try:
        groups = extract(raw)
    except Rejected as exc:
        raise SourceRejected("record_invalid", exc.code, exc.reason) from exc
    return artifact, {
        "schema": "harness.native-usage/v1",
        "runtime": "pi",
        "start_byte": start,
        "end_byte": end,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "evidence": artifact.reference(),
        "groups": groups,
        "coverage": coverage(groups),
        "extraction": extraction(groups),
    }
