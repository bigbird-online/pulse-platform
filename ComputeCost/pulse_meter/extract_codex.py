"""Codex rollout 抽取：累计用量取差、分叉拷贝段只更新基线、回合声明（合同第 2 节 Codex）。"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from pulse_compute_cost.receipts import reference

from . import turns
from .extract_cc import count
from .store import utc_text

KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
ZERO = [0, 0, 0, 0]
MCP_DECLARE = re.compile(
    r'\s*text\(\s*await\s+tools\.mcp__pulse_request_purpose__declare_session_wi\(\s*\{\s*"?wi"?\s*:\s*"([^"]+)"\s*\}\s*\)\s*\)\s*;')
TARGET = re.compile(r"--target\s+(\S+)")
SESSION_SUFFIX = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


def counter(value: object) -> list[int] | None:
    """累计计数四项；CLI 协议没有写缓存一类，缺省记 0；其余缺失即无法取差。"""
    if not isinstance(value, dict):
        return None
    result = [count(value.get(k)) for k in KEYS]
    if result[2] is None and value.get("cache_write_input_tokens") is None:
        result[2] = 0
    return None if any(v is None for v in result) else result


def session_suffix(path: Path) -> str | None:
    match = SESSION_SUFFIX.search(path.name.removesuffix(".zst").removesuffix(".jsonl"))
    return match[1] if match else None


def _declaration(payload: dict[str, Any]) -> str | None:
    """首条语句即声明的 exec，或直接的 MCP 调用；返回声明的 WI。"""
    kind, name = payload.get("type"), payload.get("name")
    if kind == "custom_tool_call" and name == "exec" and isinstance(payload.get("input"), str):
        script = payload["input"]
        match = MCP_DECLARE.match(script)
        if match:
            return match[1]
        from pulse_compute_cost.native_request_purpose import _leading_declaration_command
        command = _leading_declaration_command(payload)
        target = TARGET.search(command) if command else None
        return target[1] if target else None
    if kind == "function_call" and isinstance(name, str) and name.endswith("declare_session_wi"):
        try:
            arguments = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            return None
        wi = arguments.get("wi") if isinstance(arguments, dict) else None
        return wi if isinstance(wi, str) else None
    return None


def _output_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(b.get("text", "") for b in output if isinstance(b, dict) and isinstance(b.get("text"), str))
    return json.dumps(output) if output is not None else ""


def _accepted(text: str) -> bool:
    if "codex_session_wi/v1" in text:
        return True
    return re.search(r'"exit_code"\s*:\s*0\b', text) is not None and "Error" not in text[:200]


# 回合首条用户消息之前 Codex 注入的上下文，不是请求本身
CONTEXT_PREFIXES = ("<environment_context>", "# AGENTS.md instructions", "<user_instructions>", "<permissions")


def _first_meta(path: Path) -> dict[str, Any] | None:
    from compression import zstd
    opener = zstd.open if path.name.endswith(".zst") else open
    try:
        with opener(path, "rb") as handle:
            row = json.loads(handle.readline())
    except (OSError, ValueError):
        return None
    return row.get("payload") if isinstance(row, dict) and row.get("type") == "session_meta" else None


def _spawn_parent(payload: dict[str, Any]) -> str | None:
    """子代理线程（thread_spawn）的发起线程；界面里的普通分叉不算。"""
    source = payload.get("source")
    spawn = source.get("subagent", {}).get("thread_spawn") if isinstance(source, dict) \
        and isinstance(source.get("subagent"), dict) else None
    parent = payload.get("parent_thread_id") or (spawn or {}).get("parent_thread_id")
    return parent if isinstance(parent, str) and parent else None


def root_session(ctx, parent: str) -> str:
    """沿 parent_thread_id 上溯到根线程；父文件找不到就停在已知的最上层。"""
    shared = ctx.shared
    if "codex_by_session" not in shared:
        index: dict[str, Path] = {}
        for path in shared.get("codex_files", []):
            sid = session_suffix(path)
            if sid:
                index.setdefault(sid, path)
        shared["codex_by_session"] = index
    roots = shared.setdefault("codex_roots", {})
    current, seen = parent, set()
    while current not in seen and len(seen) < 8:
        seen.add(current)
        if current in roots:
            current = roots[current]
            break
        path = shared["codex_by_session"].get(current)
        meta = _first_meta(path) if path else None
        upper = _spawn_parent(meta) if meta else None
        if not upper:
            break
        current = upper
    for sid in seen:
        roots[sid] = current
    return current


class Extractor:
    PARSER_VERSION = 3

    def __init__(self, ctx, state: dict[str, Any]):
        self.ctx = ctx
        self.session: str | None = state.get("session")
        self.parent: str | None = state.get("parent")
        self.cwd = state.get("cwd")
        self.model = state.get("model")
        self.turn = state.get("turn", "")
        self.base: list[int] = state.get("base", list(ZERO))
        self.copy = bool(state.get("copy"))
        self.pending: dict[str, dict[str, Any]] = state.get("pending", {})
        self.root: str | None = state.get("root")          # 子代理线程所属的根线程；主线程为 None
        self.origin: dict[str, Any] | None = state.get("origin")   # 等待首条用户消息的回合

    def state(self) -> dict[str, Any]:
        return {"session": self.session, "parent": self.parent, "cwd": self.cwd, "model": self.model,
                "turn": self.turn, "base": self.base, "copy": self.copy, "pending": self.pending,
                "root": self.root, "origin": self.origin}

    def wants(self, line: bytes) -> bool:
        return ((self.origin is not None and b'"user"' in line) or b'"session_meta"' in line or b'"turn_context"' in line
                or b'"token_count"' in line or b'"task_started"' in line or b"declare" in line
                or (bool(self.pending) and b"_output" in line))

    # 分叉拷贝段：子会话自己的 session_meta 之后紧跟父会话的 session_meta 与（压缩过的）历史。
    # 实测（11 月 3 个分叉文件）拷贝段的时间戳与序号被改写、内容被压缩，逐行哈希比对
    # （_copied_parent_positions）只能对上 0–8 行；改为按 ID 时序判定：Codex 的会话与回合 ID
    # 都是按时间递增的 UUIDv7，拷贝段里的回合早于子会话创建，子会话自己的第一个回合
    # （turn_id 大于子会话 ID）开始即拷贝段结束。拷贝段内的累计计数只更新基线，不出事件。
    def feed(self, line: bytes) -> list[tuple[str, dict[str, Any]]]:
        try:
            row = json.loads(line)
        except ValueError:
            self.ctx.note("codex_invalid_json")
            return []
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            return []
        kind = row.get("type")
        if kind == "session_meta":
            sid = payload.get("id") or payload.get("session_id")
            if self.session is None and isinstance(sid, str):
                self.session = sid
                self.cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else self.cwd
                parent = payload.get("forked_from_id") or payload.get("parent_thread_id")
                self.parent = parent if isinstance(parent, str) else None
                spawn = _spawn_parent(payload)
                self.root = root_session(self.ctx, spawn) if spawn else None
                originator = payload.get("originator")
                if not self.root and isinstance(originator, str) and originator:
                    # Codex App（"Codex Desktop"）与命令行 exec（多为 Relay 启动）；子代理线程归根线程，不单记
                    return [("session_entrypoints", {
                        "node": self.ctx.node, "runtime": "codex",
                        "session_ref": reference("native_session", ["codex", sid]),
                        "entrypoint": "codex_app" if originator == "Codex Desktop" else
                                      "codex_exec" if originator == "codex_exec" else "codex:" + originator[:40],
                        "source": "codex_originator"})]
            elif isinstance(sid, str) and sid != self.session and sid == self.parent:
                self.copy = True
            return []
        if kind == "turn_context":
            if isinstance(payload.get("model"), str):
                self.model = payload["model"]
            if isinstance(payload.get("cwd"), str) and not self.copy:
                self.cwd = payload["cwd"]
            return []
        if kind == "event_msg" and payload.get("type") == "task_started":
            turn = payload.get("turn_id")
            out: list[tuple[str, dict[str, Any]]] = []
            if isinstance(turn, str):
                if self.copy and turn > (self.session or ""):
                    self.copy = False
                if not self.copy:
                    out.extend(self._close_origin())
                    attribution = payload.get("turn_attribution") if isinstance(payload.get("turn_attribution"), dict) else {}
                    if self.root:
                        # 子代理线程的回合由根线程的某个回合发起：沿用那个回合（同 CC 子代理沿用发起回合）
                        parent_turn = attribution.get("parent_turn_id")
                        self.turn = parent_turn if isinstance(parent_turn, str) and parent_turn else turn
                    else:
                        self.turn = turn
                        self.origin = {"turn": turn, "trigger": attribution.get("turn_trigger"),
                                       "at": utc_text(row.get("timestamp")) or "1970-01-01T00:00:00.000Z"}
                        if self.origin["trigger"] in turns.CODEX_HUMAN_TRIGGERS:
                            out.extend(self._close_origin(""))
            return out
        if (kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user"
                and self.origin is not None and not self.copy):
            text = turns.message_text(payload.get("content"))
            if not text.lstrip().startswith(CONTEXT_PREFIXES):
                return self._close_origin(text)
            return []
        if kind == "event_msg" and payload.get("type") == "token_count":
            return self._usage(row, payload, bool(self.copy))
        if kind == "response_item" and not self.copy:
            return self._claims(row, payload)
        return []

    def _own(self) -> str:
        return self.session or session_suffix(self.ctx.path) or self.ctx.path.stem

    def _session(self) -> str:
        """事件所属会话：子代理线程记在根线程名下，自己的线程号记 agent_id。"""
        return self.root or self._own()

    def _agent(self) -> str:
        return self._own() if self.root else ""

    def _close_origin(self, text: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        """回合起点来源：首条用户消息（或回合结束仍没有用户消息时为空正文）交 turns.classify 判定。"""
        origin, self.origin = self.origin, None
        if origin is None:
            return []
        return [("turn_origins", {
            "node": self.ctx.node, "runtime": "codex",
            "session_ref": reference("native_session", ["codex", self._session()]), "turn_id": origin["turn"],
            "origin": turns.classify(text, trigger=origin.get("trigger")), "occurred_at": origin["at"]})]

    def _usage(self, row, payload, copied) -> list[tuple[str, dict[str, Any]]]:
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total = counter(info.get("total_token_usage"))
        if total is None:
            if info.get("total_token_usage") is not None:
                self.ctx.note("codex_counter_invalid")
            return []
        delta = [a - b for a, b in zip(total, self.base)]
        if any(d < 0 for d in delta):
            if total == counter(info.get("last_token_usage")):
                delta = list(total)           # 计数器从 0 重新开始
            else:
                self.ctx.note("codex_counter_decrease")
                self.base = total
                return []
        self.base = total
        if copied or not any(delta):
            return []
        uncached = delta[0] - delta[1] - delta[2]
        if uncached < 0:
            self.ctx.note("codex_input_split_negative")
        session, own = self._session(), self._own()
        occurred = utc_text(row.get("timestamp"))
        if occurred is None:
            self.ctx.note("codex_usage_without_time")
            return []
        return [("usage_events", {
            "node": self.ctx.node, "runtime": "codex", "event_id": own + ":" + "-".join(map(str, total)),
            "request_id": "", "source": "native", "granularity": "request", "session_id": session,
            "session_ref": reference("native_session", ["codex", session]), "agent_id": self._agent(),
            "turn_id": self.turn, "occurred_at": occurred, "model": self.model, "cwd": self.cwd,
            "input_uncached": uncached if uncached >= 0 else None, "cache_read": delta[1],
            "cache_write": delta[2], "output": delta[3], "final": 1})]

    def _claims(self, row, payload) -> list[tuple[str, dict[str, Any]]]:
        call = payload.get("call_id")
        if payload.get("type") in ("custom_tool_call_output", "function_call_output") and call in self.pending:
            claim = self.pending.pop(call)
            claim["status"] = "accepted" if _accepted(_output_text(payload.get("output"))) else "rejected"
            return [("wi_claims", claim)]
        wi = _declaration(payload)
        if wi is None or not isinstance(call, str):
            return []
        session = self._session()
        # 子代理线程自己的声明只管它自己（scope=subagent），不改写发起回合的归属
        claim = {"node": self.ctx.node, "runtime": "codex", "claim_id": call,
                 "scope": "subagent" if self.root else "turn",
                 "session_ref": reference("native_session", ["codex", session]), "turn_id": self.turn,
                 "agent_id": self._agent(), "wi": wi, "status": "pending",
                 "occurred_at": utc_text(row.get("timestamp")) or "1970-01-01T00:00:00.000Z"}
        self.pending[call] = dict(claim)
        return [("wi_claims", claim)]
