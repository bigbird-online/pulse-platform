"""Pi 会话记录抽取：四类 Token、回合、声明配对（合同第 2 节 Pi）。"""
from __future__ import annotations

import json
from typing import Any

from pulse_compute_cost.receipts import reference

from .extract_cc import count
from . import turns
from .store import utc_text

TOKEN_MAP = (("input_uncached", "input"), ("cache_read", "cacheRead"), ("cache_write", "cacheWrite"),
             ("output", "output"))
USAGE_TYPES = ("compaction", "branch_summary")


def pi_entrypoint(ctx) -> str:
    """Pi 会话没有自带入口字段，按登记与目录判：Relay 会话根（经运行清单登记）下的租户目录名带
    _feishu-／_telegram- 即该渠道；其余 Relay 会话为 relay（渠道看启动记录）；手动的 ~/.pi 会话为 pi_cli。"""
    if not ctx.source.get("runtime_manifest") and "pi-sessions" not in ctx.path.parts:
        return "pi_cli"
    tenant = ctx.path.parent.name
    for channel in ("feishu", "telegram"):
        if f"_{channel}-" in tenant:
            return channel
    return "relay"


class Extractor:
    PARSER_VERSION = 3

    def __init__(self, ctx, state: dict[str, Any]):
        self.ctx = ctx
        self.session: str | None = state.get("session")
        self.cwd = state.get("cwd")
        self.model = state.get("model")
        self.turn = state.get("turn", "")
        self.pending: dict[str, dict[str, Any]] = state.get("pending", {})

    def state(self) -> dict[str, Any]:
        return {"session": self.session, "cwd": self.cwd, "model": self.model, "turn": self.turn,
                "pending": self.pending}

    def wants(self, line: bytes) -> bool:
        return (self.session is None or b'"usage"' in line or b'"user"' in line or b"model_change" in line
                or b"declare_session_wi" in line)

    def _ref(self) -> str:
        return reference("native_session", ["pi", self._sid()])

    def _sid(self) -> str:
        return self.session or self.ctx.path.stem

    def feed(self, line: bytes) -> list[tuple[str, dict[str, Any]]]:
        try:
            row = json.loads(line)
        except ValueError:
            self.ctx.note("pi_invalid_json")
            return []
        if not isinstance(row, dict):
            return []
        kind = row.get("type")
        if kind == "session":
            if self.session is None and isinstance(row.get("id"), str):
                self.session = row["id"]
                self.cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else None
                return [("session_entrypoints", {"node": self.ctx.node, "runtime": "pi", "session_ref": self._ref(),
                                                 "entrypoint": pi_entrypoint(self.ctx), "source": "pi_path"})]
            return []
        if kind == "model_change":
            if isinstance(row.get("modelId"), str):
                self.model = row["modelId"]
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        role = message.get("role")
        usage = None
        if kind == "message":
            if role == "user" and isinstance(row.get("id"), str):
                self.turn = row["id"]
                out.append(("turn_origins", {
                    "node": self.ctx.node, "runtime": "pi", "session_ref": self._ref(), "turn_id": row["id"],
                    "origin": turns.classify(turns.message_text(message.get("content"))),
                    "occurred_at": utc_text(row.get("timestamp")) or "1970-01-01T00:00:00.000Z"}))
            if role == "assistant":
                out.extend(self._declarations(row, message))
            if role == "toolResult" and message.get("toolCallId") in self.pending:
                claim = self.pending.pop(message["toolCallId"])
                claim["status"] = "rejected" if message.get("isError") else "accepted"
                out.append(("wi_claims", claim))
            if role in ("assistant", "toolResult"):
                usage = message.get("usage")
        elif kind in USAGE_TYPES:
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else message.get("usage")
        if isinstance(usage, dict):
            occurred = utc_text(row.get("timestamp"))
            if not isinstance(row.get("id"), str) or occurred is None:
                self.ctx.note("pi_usage_without_identity")
                return out
            model = message.get("model") if isinstance(message.get("model"), str) else self.model
            out.append(("usage_events", {
                "node": self.ctx.node, "runtime": "pi", "event_id": f"{self._sid()}:{row['id']}", "request_id": "",
                "source": "native", "granularity": "request", "session_id": self._sid(), "session_ref": self._ref(),
                "agent_id": "", "turn_id": self.turn, "occurred_at": occurred, "model": model, "cwd": self.cwd,
                **{col: count(usage.get(name)) for col, name in TOKEN_MAP}, "final": 1}))
        return out

    def _declarations(self, row, message) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "toolCall" and block.get("name") == "declare_session_wi" \
                    and isinstance(block.get("id"), str):
                args = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}
                wi = args.get("wi") if isinstance(args.get("wi"), str) and args.get("wi") else "invalid"
                claim = {"node": self.ctx.node, "runtime": "pi", "claim_id": block["id"], "scope": "turn",
                         "session_ref": self._ref(), "turn_id": self.turn, "agent_id": "", "wi": wi,
                         "status": "pending",
                         "occurred_at": utc_text(row.get("timestamp")) or "1970-01-01T00:00:00.000Z"}
                self.pending[block["id"]] = dict(claim)
                out.append(("wi_claims", claim))
        return out
