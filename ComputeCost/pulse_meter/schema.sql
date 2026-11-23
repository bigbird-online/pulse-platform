CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- 键：schema_version='1'；node；db_epoch（建库时生成的随机串）；next_seq（本库变更序号计数器）

CREATE TABLE IF NOT EXISTS usage_events(
  node TEXT NOT NULL, runtime TEXT NOT NULL,          -- runtime: cc | codex | pi
  event_id TEXT NOT NULL, request_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL CHECK(source IN('native','host')),
  granularity TEXT NOT NULL CHECK(granularity IN('request','turn')),
  session_id TEXT NOT NULL DEFAULT '',                -- 原始会话号；host 来源为空
  session_ref TEXT NOT NULL,                          -- reference('native_session',[runtime,session_id])
  agent_id TEXT NOT NULL DEFAULT '',                  -- 子代理号；主线为空
  turn_id TEXT NOT NULL DEFAULT '',                   -- 所属回合
  occurred_at TEXT NOT NULL,                          -- UTC ISO8601
  model TEXT, cwd TEXT,
  input_uncached INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER,  -- NULL＝未知，不补 0
  final INTEGER NOT NULL DEFAULT 0,
  change_seq INTEGER NOT NULL,
  PRIMARY KEY(node, runtime, event_id, request_id));
CREATE INDEX IF NOT EXISTS usage_session ON usage_events(session_ref, source);
CREATE INDEX IF NOT EXISTS usage_time ON usage_events(occurred_at);
CREATE INDEX IF NOT EXISTS usage_seq ON usage_events(change_seq);

CREATE TABLE IF NOT EXISTS wi_claims(
  node TEXT NOT NULL, runtime TEXT NOT NULL, claim_id TEXT NOT NULL,
  scope TEXT NOT NULL CHECK(scope IN('subagent','turn')),
  session_ref TEXT NOT NULL, turn_id TEXT NOT NULL DEFAULT '', agent_id TEXT NOT NULL DEFAULT '',
  wi TEXT NOT NULL,                                   -- wi_YYYYMMDD_NNN 或 'none'
  status TEXT NOT NULL CHECK(status IN('pending','accepted','rejected')),
  occurred_at TEXT NOT NULL, change_seq INTEGER NOT NULL,
  PRIMARY KEY(node, runtime, claim_id));
CREATE INDEX IF NOT EXISTS claim_turn ON wi_claims(session_ref, scope, turn_id, agent_id);
CREATE INDEX IF NOT EXISTS claim_seq ON wi_claims(change_seq);

CREATE TABLE IF NOT EXISTS launch_receipts(
  node TEXT NOT NULL, receipt_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN('execution','scheduled_session','session_binding')),
  occurred_at TEXT NOT NULL, runtime TEXT, agent TEXT, wi TEXT,
  attempt_ref TEXT, session_ref TEXT, job_id TEXT, job_name TEXT,
  entrypoint TEXT,                                    -- automation | feishu | telegram | NULL
  state TEXT, change_seq INTEGER NOT NULL,
  PRIMARY KEY(node, receipt_id));
CREATE INDEX IF NOT EXISTS launch_session ON launch_receipts(session_ref, kind, occurred_at);
CREATE INDEX IF NOT EXISTS launch_attempt ON launch_receipts(attempt_ref, kind);
CREATE INDEX IF NOT EXISTS launch_seq ON launch_receipts(change_seq);

CREATE TABLE IF NOT EXISTS session_entrypoints(      -- 原生会话记录自带的入口信号：集合，每个信号一行，随节点上送
  node TEXT NOT NULL, runtime TEXT NOT NULL, session_ref TEXT NOT NULL,
  entrypoint TEXT NOT NULL,                           -- cli | sdk | codex_app | codex_exec | pi_cli | relay | feishu | telegram
  source TEXT NOT NULL,                               -- 信号出处：cc_entrypoint | codex_originator | pi_path
  change_seq INTEGER NOT NULL,                        -- 只插入不覆盖；消失的信号由重读撤销（墓碑）；视图按会话取 (entrypoint, source) 最小者
  PRIMARY KEY(node, runtime, session_ref, entrypoint, source));
CREATE INDEX IF NOT EXISTS session_entrypoint_seq ON session_entrypoints(change_seq);

CREATE TABLE IF NOT EXISTS workspaces(               -- 工作区到 Agent 的登记，随节点上送
  node TEXT NOT NULL, workspace_root TEXT NOT NULL, agent TEXT NOT NULL,
  change_seq INTEGER NOT NULL, PRIMARY KEY(node, workspace_root));

-- 仅节点库使用（不上送）
CREATE TABLE IF NOT EXISTS file_cursors(
  path TEXT PRIMARY KEY, runtime TEXT NOT NULL,       -- runtime 另含 'host'
  inode INTEGER NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL,
  head_sha256 TEXT NOT NULL,                          -- 文件前 min(4096,size) 字节的哈希
  parser_version INTEGER NOT NULL, state TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
-- 撤销（墓碑）：节点重读来源文件后不再产生的行从事实表删除，在此记一条，随批上送，汇总库据此删行
CREATE TABLE IF NOT EXISTS retractions(
  node TEXT NOT NULL, table_name TEXT NOT NULL, row_key TEXT NOT NULL,   -- row_key：主键各列的 JSON 数组
  change_seq INTEGER NOT NULL, PRIMARY KEY(node, table_name, row_key));
CREATE INDEX IF NOT EXISTS retraction_seq ON retractions(change_seq);

-- 仅节点库使用（不上送）续：哪个来源文件产生了哪一行；整文件重读后求差集撤销
CREATE TABLE IF NOT EXISTS file_rows(
  path TEXT NOT NULL, table_name TEXT NOT NULL, row_key TEXT NOT NULL,
  PRIMARY KEY(path, table_name, row_key));
CREATE INDEX IF NOT EXISTS file_rows_key ON file_rows(table_name, row_key);
CREATE TABLE IF NOT EXISTS ship_cursors(
  dest TEXT NOT NULL, table_name TEXT NOT NULL, acked_seq INTEGER NOT NULL,
  PRIMARY KEY(dest, table_name));

-- 仅汇总库使用
CREATE TABLE IF NOT EXISTS transfer_cursors(          -- 传输游标：每个节点、每张表收到哪
  peer TEXT NOT NULL, db_epoch TEXT NOT NULL, table_name TEXT NOT NULL,
  last_seq INTEGER NOT NULL, last_received_at TEXT NOT NULL,
  backlog_bytes INTEGER NOT NULL DEFAULT 0, backlog_files INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(peer, db_epoch, table_name));
CREATE TABLE IF NOT EXISTS prices(                    -- 线②从价表快照装入
  model_key TEXT NOT NULL, effective_from TEXT NOT NULL,   -- Asia/Shanghai 日期
  prompt INTEGER NOT NULL, cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, completion INTEGER NOT NULL,
  -- 单位：每 Token 的 1e-12 美元（整数），金额全程整数运算
  catalog_sha256 TEXT NOT NULL, PRIMARY KEY(model_key, effective_from));

-- 回合起点来源（判定规则 turns.classify），节点事实，随节点上送
CREATE TABLE IF NOT EXISTS turn_origins(
  node TEXT NOT NULL, runtime TEXT NOT NULL, session_ref TEXT NOT NULL, turn_id TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN('human','delivery','continuation')),
  occurred_at TEXT NOT NULL,                          -- 回合首条用户消息时间，UTC ISO8601
  change_seq INTEGER NOT NULL,
  PRIMARY KEY(node, runtime, session_ref, turn_id));
CREATE INDEX IF NOT EXISTS turn_origin_session ON turn_origins(session_ref, occurred_at);
CREATE INDEX IF NOT EXISTS turn_origin_turn ON turn_origins(session_ref, turn_id);
CREATE INDEX IF NOT EXISTS turn_origin_seq ON turn_origins(change_seq);
