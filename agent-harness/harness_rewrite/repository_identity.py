"""Repository identity excludes HTTP authentication; original receipts stay immutable."""

from __future__ import annotations

from typing import Any, overload
from urllib.parse import urlsplit, urlunsplit
import re


@overload
def remote_identity(value: str) -> str: ...


@overload
def remote_identity(value: None) -> None: ...


def remote_identity(value: str | None) -> str | None:
    if value is None:
        return None
    parts = urlsplit(value)
    if parts.scheme.lower() in {"http", "https"} and "@" in parts.netloc:
        return urlunsplit(
            (parts.scheme, parts.netloc.rsplit("@", 1)[1], parts.path, parts.query, parts.fragment)
        )
    return value


def repository_values(value: Any) -> Any:
    """Copy nested repository bindings, without altering any original fact bytes."""
    if isinstance(value, dict):
        return {
            k: remote_identity(v)
            if k == "remote_url" and isinstance(v, str)
            else repository_values(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [repository_values(v) for v in value]
    return value


def public_response(value: Any) -> Any:
    """Old credential-bearing facts remain replayable but cannot leak to callers."""
    if isinstance(value, dict):
        return {k: public_response(v) for k, v in value.items()}
    if isinstance(value, list):
        return [public_response(v) for v in value]
    if isinstance(value, str):
        value = re.sub(
            r"https?://[^/\s@]+@",
            lambda m: m.group().split("://", 1)[0] + "://",
            value,
            flags=re.IGNORECASE,
        )
        return re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[redacted]", value)
    return value


def semantic_identity(value: dict[str, Any]) -> dict[str, Any]:
    """Credential rotation does not change an already committed business request."""
    return {
        k: repository_values(v)
        if k in {"repositories", "publication_targets", "source_consumers"}
        else v
        for k, v in value.items()
    }
