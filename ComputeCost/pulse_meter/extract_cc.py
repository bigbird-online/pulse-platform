"""CC 会话记录抽取：用量、回合、回合声明、子代理标记（合同第 2 节 CC）。"""
from __future__ import annotations

import json
import re
from typing import Any

from pulse_compute_cost.receipts import reference

from . import turns
from .store import utc_text

DECLARE_TOOL = "mcp__pulse-request-purpose__declare_session_wi"
MARKER = re.compile(r"\[WI: (wi_\d{8}_\d{3})\]")
ENTRYPOINT = re.compile(rb'"entrypoint"\s*:\s*"([^"\\]*)"')
TOKEN_MAP = (("input_uncached", "input_tokens"), ("cache_read", "cache_read_input_tokens"),
             ("cache_write", "cache_creation_input_tokens"), ("output", "output_tokens"))


def count(value: object) -> int | None:
    """原生 Token 数：非负整数照取，缺失或不合法为 NULL（未知），不补 0。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def first_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                return block["text"]
    return None


class Extractor:
    PARSER_VERSION = 3

    def __init__(self, ctx, state: dict[str, Any]):
        self.ctx = ctx
        parts = ctx.path.parts
        self.subagent_file = len(parts) >= 3 and parts[-2] == "subagents"
        self.path_session = parts[-3] if self.subagent_file else ctx.path.stem
        self.path_agent = ctx.path.stem[len("agent-"):] if self.subagent_file else ""
        self.turn = state.get("turn", "")
        self.pending: dict[str, dict[str, Any]] = state.get("pending", {})
        self.started = state.get("started", False)
        self.entrypoints: list[str] = state.get("entrypoints", [])

    def state(self) -> dict[str, Any]:
        return {"turn": self.turn, "pending": self.pending, "started": self.started, "entrypoints": self.entrypoints}

    def wants(self, line: bytes) -> bool:
        return (not self.started or b'"usage"' in line or b'"promptId"' in line or self._new_entrypoint(line)
                or b"declare_session_wi" in line or (bool(self.pending) and b"tool_use_id" in line))

    def _new_entrypoint(self, line: bytes) -> bool:
        """预筛：行里任一处 entrypoint 值本文件还没见过就解析；是否算数由 feed 只看顶层字段决定。"""
        return any(v.decode("utf-8", "replace") not in self.entrypoints for v in ENTRYPOINT.findall(line))

    def feed(self, line: bytes) -> list[tuple[str, dict[str, Any]]]:
        try:
            row = json.loads(line)
        except ValueError:
            self.ctx.note("cc_invalid_json")
            return []
        if not isinstance(row, dict):
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        first, self.started = not self.started, True
        session = row.get("sessionId") if isinstance(row.get("sessionId"), str) else self.path_session
        sidechain = self.subagent_file or row.get("isSidechain") is True
        agent = (row.get("agentId") if isinstance(row.get("agentId"), str) else self.path_agent) if sidechain else ""
        scope = reference("native_session", ["cc", session])
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        kind = row.get("type")
        native = row.get("entrypoint")
        if isinstance(native, str) and native and native not in self.entrypoints:
            # CC 记录自带入口：cli＝终端里交互；sdk-cli 等＝程序化启动（Relay 经 SDK），渠道另由启动记录判
            self.entrypoints.append(native)
            out.append(("session_entrypoints", {"node": self.ctx.node, "runtime": "cc", "session_ref": scope,
                                                "entrypoint": "cli" if native == "cli" else "sdk",
                                                "source": "cc_entrypoint"}))
        if kind == "user":
            prompt = row.get("promptId")
            if isinstance(prompt, str) and prompt and prompt != self.turn:
                self.turn = prompt
                # 回合首条用户消息定回合起点来源；子代理文件首行的 promptId 属于发起它的主线回合，不在此判
                if not (first and self.subagent_file):
                    out.append(("turn_origins", {
                        "node": self.ctx.node, "runtime": "cc", "session_ref": scope, "turn_id": prompt,
                        "origin": turns.classify_cc_line(row),
                        "occurred_at": utc_text(row.get("timestamp")) or "1970-01-01T00:00:00.000Z"}))
            if first and self.subagent_file and row.get("parentUuid") is None:
                text = first_text(message.get("content"))
                match = MARKER.match(text.split("\n", 1)[0]) if text else None
                if match:
                    out.append(("wi_claims", {
                        "node": self.ctx.node, "runtime": "cc", "claim_id": f"marker:{session}:{agent}",
                        "scope": "subagent", "session_ref": scope, "turn_id": self.turn, "agent_id": agent,
                        "wi": match[1], "status": "accepted",
                        "occurred_at": utc_text(row.get("timestamp")) or "1970-01-01T00:00:00.000Z"}))
            if self.pending and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result" \
                            and block.get("tool_use_id") in self.pending:
                        claim = self.pending.pop(block["tool_use_id"])
                        claim["status"] = "rejected" if block.get("is_error") else "accepted"
                        out.append(("wi_claims", claim))
        elif kind == "assistant":
            for block in message.get("content") if isinstance(message.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == DECLARE_TOOL \
                        and isinstance(block.get("id"), str):
                    wi = (block.get("input") or {}).get("wi") if isinstance(block.get("input"), dict) else None
                    claim = {"node": self.ctx.node, "runtime": "cc", "claim_id": block["id"], "scope": "turn",
                             "session_ref": scope, "turn_id": self.turn, "agent_id": agent,
                             "wi": wi if isinstance(wi, str) and wi else "invalid", "status": "pending",
                             "occurred_at": utc_text(row.get("timestamp")) or "1970-01-01T00:00:00.000Z"}
                    self.pending[block["id"]] = dict(claim)
                    out.append(("wi_claims", claim))
            usage = message.get("usage")
            if isinstance(usage, dict) and message.get("model") != "<synthetic>":
                event = message.get("id") if isinstance(message.get("id"), str) else row.get("uuid")
                occurred = utc_text(row.get("timestamp"))
                if not isinstance(event, str) or occurred is None:
                    self.ctx.note("cc_usage_without_identity")
                else:
                    out.append(("usage_events", {
                        "node": self.ctx.node, "runtime": "cc", "event_id": event,
                        "request_id": row.get("requestId") if isinstance(row.get("requestId"), str) else "",
                        "source": "native", "granularity": "request", "session_id": session,
                        "session_ref": scope, "agent_id": agent, "turn_id": self.turn, "occurred_at": occurred,
                        "model": message.get("model") if isinstance(message.get("model"), str) else None,
                        "cwd": row.get("cwd") if isinstance(row.get("cwd"), str) else None,
                        **{col: count(usage.get(name)) for col, name in TOKEN_MAP},
                        "final": 1 if message.get("stop_reason") else 0}))
        return out
