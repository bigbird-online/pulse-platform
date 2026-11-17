"""Bounded immutable fact objects; no transcript or private-byte publication."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

from .model import Json, Rejected, digest

SCHEMA = "harness.production-ledger/v2"
PREFIX = ".harness/core"
CHUNK = 1024 * 1024


def checksum(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Rejected("ledger_invalid", "Invalid content address")
    return value


def object_path(sha: str) -> str:
    return f"{PREFIX}/objects/{checksum(sha)[:2]}/{sha}"


def pack(content: bytes, updates: dict[str, bytes]) -> Json:
    parts = [content[i:i + CHUNK] for i in range(0, len(content), CHUNK)]
    chunks = []
    for part in parts:
        sha = hashlib.sha256(part).hexdigest()
        chunks.append(sha)
        updates[object_path(sha)] = part
    return {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "chunks": chunks}


def unpack(ref: Json, read: Callable[[str], bytes]) -> bytes:
    if (set(ref) != {"sha256", "size", "chunks"} or type(ref["size"]) is not int
            or ref["size"] < 0 or not isinstance(ref["chunks"], list)
            or len(ref["chunks"]) != (ref["size"] + CHUNK - 1) // CHUNK):
        raise Rejected("ledger_invalid", "Invalid bounded object reference")
    checksum(ref["sha256"])
    parts = []
    for index, sha in enumerate(ref["chunks"]):
        content = read(object_path(sha))
        if (hashlib.sha256(content).hexdigest() != sha
                or len(content) != min(CHUNK, ref["size"] - index * CHUNK)):
            raise Rejected("ledger_invalid", "Bounded object differs from its reference")
        parts.append(content)
    result = b"".join(parts)
    if hashlib.sha256(result).hexdigest() != ref["sha256"]:
        raise Rejected("ledger_invalid", "Original object digest differs")
    return result


def entry_path(wi: str, revision: int) -> str:
    return f"{PREFIX}/journals/{wi}/{revision:012d}.json"


def request_path(namespace: str, request: str) -> str:
    return f"{PREFIX}/requests/{digest([namespace, request])}.json"


def usage_prefix(principal: Json) -> str:
    return f"{PREFIX}/usage/{digest(principal)}/"


def pull_request_prefix(name: str) -> str:
    """Claims of one repository#number; it belongs to a single WI."""
    return f"{PREFIX}/pull-requests/{digest(name)}/"


def merge_authority_prefix(source: str) -> str:
    """Claims of one human approval; it registers a single merge authority."""
    return f"{PREFIX}/merge-authority/{digest(source)}/"


def child_prefix(parent: str) -> str:
    """Claims of the child WorkItems created under one parent."""
    return f"{PREFIX}/children/{parent}/"


def private_digests(history: list[Json]) -> set[str]:
    result = set()
    for entry in history:
        semantics = entry["semantics"]
        source = semantics.get("source")
        if source:
            result.add(source["sha256"])
        result.update(a["sha256"] for a in semantics.get("artifacts", []) if a["access"] == "private")
    return result


def load_entry(path: str, read: Callable[[str], bytes]) -> tuple[Json, Json]:
    try:
        wrapper = json.loads(read(path))
        if ("entry" in wrapper) == ("record" in wrapper):
            raise ValueError("Ambiguous decision encoding")
        entry = wrapper["record"] if "record" in wrapper else json.loads(unpack(wrapper["entry"], read))
        if entry_path(entry["wi"], entry["revision"]) != path:
            raise ValueError("Wrong entry")
        refs = wrapper["originals"]
        if set(refs) != set(entry["originals"]):
            raise ValueError("Wrong originals")
        for sha, ref in refs.items():
            checksum(sha)
            if type(ref["size"]) is not int or ref["size"] < 0:
                raise ValueError("Wrong size")
            if ref["access"] == "private":
                if set(ref) != {"access", "size"}:
                    raise ValueError("Private reference contains payload")
            elif ref["access"] == "git":
                if set(ref) != {"access", "size", "object"} or ref["object"]["sha256"] != sha:
                    raise ValueError("Wrong public reference")
            else:
                raise ValueError("Unknown evidence custody")
        return entry, refs
    except (ValueError, TypeError, KeyError) as exc:
        raise Rejected("ledger_invalid", "Invalid immutable decision segment") from exc
