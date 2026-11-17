"""Privacy-safe lineage derived from explicit Claude Code transcript facts."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Mapping

from .events import Lineage


_CC_ROOT_DOMAIN = "pulse.compute_cost.lineage.v1\0cc_root_session\0"


def _root_run_ref(raw_root_session_id: str) -> str:
    material = f"{_CC_ROOT_DOMAIN}{raw_root_session_id}".encode("utf-8")
    return f"lineage_v1_{sha256(material).hexdigest()}"


def _main_root_session(path: Path, session_id: str) -> str | None:
    """Return a verified root key for an explicit non-sidechain transcript row."""

    if path.parent.name == "subagents":
        return None
    if path.name == "main.jsonl":
        root_session_id = path.parent.name
    else:
        root_session_id = path.stem
    if root_session_id != session_id:
        return None
    return root_session_id


def _sidechain_root_session(path: Path, agent_id: str) -> str | None:
    """Validate the producer layout and return its root-session directory key."""

    if path.parent.name != "subagents":
        return None
    if path.name != f"agent-{agent_id}.jsonl":
        return None

    root_dir = path.parent.parent
    root_session_id = root_dir.name
    if not root_session_id:
        return None

    # Claude Code has used both a flat parent transcript and a session directory
    # with main.jsonl. The child path alone is not enough evidence: require the
    # matching root transcript to exist before grouping its cost under that root.
    flat_main = root_dir.parent / f"{root_session_id}.jsonl"
    nested_main = root_dir / "main.jsonl"
    if not flat_main.is_file() and not nested_main.is_file():
        return None
    return root_session_id


def cc_transcript_lineage(path: Path, row: Mapping[str, object]) -> Lineage:
    """Build lineage only from explicit, mutually consistent producer evidence.

    `isSidechain` proves the session role. Claude Code currently does not expose
    a stable direct-parent key in a child transcript, so parent_run_ref remains
    unknown. Raw session/agent ids and paths are validation inputs only and are
    never returned.
    """

    is_sidechain = row.get("isSidechain")
    session_id = row.get("sessionId")
    if not isinstance(is_sidechain, bool):
        return Lineage()
    if not isinstance(session_id, str) or not session_id.strip():
        return Lineage()

    if not is_sidechain:
        root_session_id = _main_root_session(path, session_id.strip())
        if root_session_id is None:
            return Lineage()
        return Lineage(
            session_role="main",
            root_run_ref=_root_run_ref(root_session_id),
        )

    agent_id = row.get("agentId")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return Lineage()
    root_session_id = _sidechain_root_session(path, agent_id.strip())
    if root_session_id is None:
        return Lineage()
    return Lineage(
        session_role="sub_agent",
        root_run_ref=_root_run_ref(root_session_id),
    )
