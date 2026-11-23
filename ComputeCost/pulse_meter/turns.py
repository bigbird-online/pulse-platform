"""回合起点的来源（合同第 4 节 v_event 第 2b 级）：人类请求、系统投递，还是上一请求的延续。

平台规则：新的人类请求（及系统投递的独立任务）各自声明 WI，不继承上一请求；
非人类发起的回合（子代理完成通知、后台任务通知、Host 续接、Relay 重启续接、
上下文压缩后的续写、system-reminder 等）是上一请求的延续，沿用同会话最近一次声明。

表 `turn_origins` 由节点抽取时按回合首条用户消息写入（线①），`classify` 是判定规则。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping

ORIGINS = ('human', 'delivery', 'continuation')

DDL = """
CREATE TABLE IF NOT EXISTS turn_origins(
  node TEXT NOT NULL, runtime TEXT NOT NULL, session_ref TEXT NOT NULL, turn_id TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN('human','delivery','continuation')),
  occurred_at TEXT NOT NULL,                          -- 回合首条用户消息时间，UTC ISO8601
  change_seq INTEGER NOT NULL,
  PRIMARY KEY(node, runtime, session_ref, turn_id));
CREATE INDEX IF NOT EXISTS turn_origin_session ON turn_origins(session_ref, occurred_at);
CREATE INDEX IF NOT EXISTS turn_origin_turn ON turn_origins(session_ref, turn_id);
CREATE INDEX IF NOT EXISTS turn_origin_seq ON turn_origins(change_seq);
"""

# 回合首条用户消息以这些开头即为延续（去掉前导空白后比较）。
CONTINUATION_PREFIXES = (
    '<task-notification>', '<system-reminder>', '[Host session continuation',
    '系统提示：Relay 已重启', 'This session is being continued from a previous conversation',
    '<local-command', '<command-name>', '<command-message>', 'Caveat: The messages below were generated',
)
DELIVERY_PREFIXES = ('[系统投递',)
# Codex 的 turn_trigger：composer / remote_ios 是人在界面里发的；exec 看正文（Relay 投递的人类消息
# 带 Host human input reference）；其余（如 send_user_message_async_question）按正文判定。
CODEX_HUMAN_TRIGGERS = ('composer', 'remote_ios')


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def classify(text: str | None, *, meta: bool = False, origin_kind: str | None = None,
             trigger: str | None = None) -> str:
    """一条回合起点消息 → 'human' | 'delivery' | 'continuation'。"""
    if trigger in CODEX_HUMAN_TRIGGERS:
        return 'human'
    if meta or origin_kind == 'task-notification':
        return 'continuation'
    body = (text or '').lstrip()
    if body.startswith(DELIVERY_PREFIXES):
        return 'delivery'
    if body.startswith(CONTINUATION_PREFIXES):
        return 'continuation'
    return 'human'


def message_text(content: Any) -> str:
    """CC/Codex 消息正文：字符串，或内容块里 text/input_text 的拼接。"""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, Mapping) and block.get('type') in ('text', 'input_text'):
            parts.append(str(block.get('text') or ''))
    return '\n'.join(parts)


def classify_cc_line(record: Mapping[str, Any]) -> str:
    """CC jsonl 里带 promptId 的 type=user 行。"""
    message = record.get('message') if isinstance(record.get('message'), Mapping) else {}
    origin = record.get('origin') if isinstance(record.get('origin'), Mapping) else {}
    return classify(message_text(message.get('content')), meta=bool(record.get('isMeta')),
                    origin_kind=origin.get('kind'))
