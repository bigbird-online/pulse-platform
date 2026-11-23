-- 归属与计价视图（合同第 4 节）。只建 TEMP 对象，不改库结构；每个连接由
-- pulse_meter.publish.attach_views() 执行一次，执行前须注册两个函数：
--   pm_model_key(model)  模型名 → 价表 model_key（wi_pricing._or_model_id 的拼写映射）
--   pm_cwd_agent(cwd)    …/agent-harness/workspaces/<组织>/<仓库>/<WI>/<agent> 的 <agent>，不符则 NULL
--   pm_cwd_wi(cwd)       同一路径里的 <WI>，不符则 NULL
--   pm_amount(rates, 四类 Token)  价表一行 × 四类 Token 的整数金额（1e-12 美元），缺所需单价则 NULL
-- 时间列都是 store.utc_text 的 UTC 毫秒串（…T…:…:….mmmZ），字典序即时间序。

DROP VIEW IF EXISTS temp.v_event;
DROP VIEW IF EXISTS temp.v_event_base;
DROP VIEW IF EXISTS temp.v_event_raw;
DROP VIEW IF EXISTS temp.v_usage;
DROP TABLE IF EXISTS temp.t_launch;
DROP TABLE IF EXISTS temp.t_holder;

-- 会话写入补充：publish.connect 用 outcomes.holder_claims 填（内核导出的会话写入 × 回合窗口）。
CREATE TEMP TABLE t_holder(session_ref TEXT NOT NULL, turn_id TEXT NOT NULL, wi TEXT NOT NULL);

-- 会话的启动记录：execution 经 session_binding 接到会话，或自带 session_ref；scheduled_session 自带。
CREATE TEMP TABLE t_launch AS
  SELECT b.session_ref, e.receipt_id, e.kind, e.occurred_at, e.agent, NULLIF(e.wi, '') AS wi,
         e.entrypoint, NULLIF(e.job_id, '') AS job_id, e.job_name
    FROM launch_receipts b JOIN launch_receipts e ON e.kind = 'execution' AND e.attempt_ref = b.attempt_ref
   WHERE b.kind = 'session_binding' AND b.session_ref IS NOT NULL AND b.attempt_ref IS NOT NULL
  UNION
  SELECT session_ref, receipt_id, kind, occurred_at, agent, NULLIF(wi, ''), entrypoint, NULLIF(job_id, ''), job_name
    FROM launch_receipts
   WHERE kind IN ('execution', 'scheduled_session') AND session_ref IS NOT NULL;
CREATE INDEX temp.t_launch_session ON t_launch(session_ref, occurred_at);
CREATE INDEX temp.t_holder_session ON t_holder(session_ref, turn_id);

-- 生效用量。一个会话、一个模型只取一种来源，不重复计：
-- 1. 原生记录全取。
-- 2. 会话没有原生记录时取 host 记录：host request 只取 final=1 的行（消息开始时写的 final=0 行
--    output 偏低）；CC 会话 host turn（CC SDK 累计值取差）压过 host request，其他 Runtime
--    host request 压过 host turn。
-- 3. 会话有原生记录、但某模型只出现在 host turn（如 CC 的 claude-haiku 辅助调用）：该模型的
--    host turn 补计；回合、子代理号、cwd、会话号沿用同会话时间上最近的原生记录，归属随之沿用。
--    同模型两边都有时以原生为准，不补差。
CREATE TEMP VIEW v_usage AS
SELECT u.* FROM usage_events u WHERE
  u.source = 'native'
  OR (u.source = 'host'
      AND NOT EXISTS(SELECT 1 FROM usage_events n WHERE n.session_ref = u.session_ref AND n.source = 'native')
      AND ((u.runtime = 'cc' AND u.granularity = 'turn')
           OR (u.runtime = 'cc' AND u.granularity = 'request' AND u.final = 1
               AND NOT EXISTS(SELECT 1 FROM usage_events t WHERE t.session_ref = u.session_ref
                              AND t.source = 'host' AND t.granularity = 'turn'))
           OR (u.runtime <> 'cc' AND u.granularity = 'request' AND u.final = 1)
           OR (u.runtime <> 'cc' AND u.granularity = 'turn'
               AND NOT EXISTS(SELECT 1 FROM usage_events r WHERE r.session_ref = u.session_ref
                              AND r.source = 'host' AND r.granularity = 'request' AND r.final = 1))))
UNION ALL
SELECT s.node, s.runtime, s.event_id, s.request_id, s.source, s.granularity, n.session_id, s.session_ref,
       n.agent_id, n.turn_id, s.occurred_at, s.model, n.cwd, s.input_uncached, s.cache_read, s.cache_write,
       s.output, s.final, s.change_seq
  FROM (SELECT u.*, COALESCE(
                 (SELECT m.rowid FROM usage_events m WHERE m.session_ref = u.session_ref AND m.source = 'native'
                     AND m.occurred_at <= u.occurred_at ORDER BY m.occurred_at DESC, m.rowid DESC LIMIT 1),
                 (SELECT m.rowid FROM usage_events m WHERE m.session_ref = u.session_ref AND m.source = 'native'
                   ORDER BY m.occurred_at, m.rowid LIMIT 1)) AS nearest
          FROM usage_events u
         WHERE u.source = 'host' AND u.granularity = 'turn' AND u.model IS NOT NULL AND u.model <> ''
           AND EXISTS(SELECT 1 FROM usage_events x WHERE x.session_ref = u.session_ref AND x.source = 'native')
           AND NOT EXISTS(SELECT 1 FROM usage_events x WHERE x.session_ref = u.session_ref AND x.source = 'native'
                          AND x.model = u.model)) s
  JOIN usage_events n ON n.rowid = s.nearest;

-- 每条生效用量一行，只用标量子查询。wi_pick 编码"哪一层、什么结果"：
--   'N:<wi>' 第 N 层唯一主张；'N!' 第 N 层多于一个不同 WI（冲突，不再下探）；NULL 五层都没有。
--   层：1 子代理标记，2 回合声明，C 延续回合沿用声明，3 启动记录带的 WI，4 会话写入补充，5 定时任务。
CREATE TEMP VIEW v_event_raw AS SELECT u.*,
  date(u.occurred_at, '+8 hours') AS day,
  COALESCE(
    (SELECT w.agent FROM workspaces w
      WHERE w.node = u.node AND u.cwd IS NOT NULL
        AND (u.cwd = rtrim(w.workspace_root, '/')
             OR substr(u.cwd, 1, length(rtrim(w.workspace_root, '/')) + 1) = rtrim(w.workspace_root, '/') || '/')
      ORDER BY length(rtrim(w.workspace_root, '/')) DESC LIMIT 1),
    pm_cwd_agent(u.cwd),
    (SELECT l.agent FROM t_launch l
      WHERE l.session_ref = u.session_ref AND l.kind IN ('execution', 'scheduled_session')
        AND l.agent IS NOT NULL AND l.agent <> ''
        AND l.occurred_at <= u.occurred_at
      ORDER BY l.occurred_at DESC, l.receipt_id DESC LIMIT 1),
    -- 受管工作区 …/workspaces/<组织>/<仓库>/<WI>/workspace.op_…：该 WI 的 owner。
    (SELECT o.owner FROM wi_owners o WHERE o.work_item_id = pm_cwd_wi(u.cwd)),
    -- 兜底：前面各级都判不出、且本节点没有任何工作区登记（如只剩历史记录的 TCloud）时，借别的节点的
    -- 登记：每个节点各取对该 cwd 的最长匹配，各节点结果一致才取，任一不一致就不取。绝对路径不能证明
    -- 机器身份，所以排在执行与定时启动记录之后，本节点有登记时不用。
    (SELECT CASE WHEN count(DISTINCT m.agent) = 1 THEN min(m.agent) END
       FROM (SELECT (SELECT w.agent FROM workspaces w
                      WHERE w.node = n.node
                        AND (u.cwd = rtrim(w.workspace_root, '/')
                             OR substr(u.cwd, 1, length(rtrim(w.workspace_root, '/')) + 1)
                                = rtrim(w.workspace_root, '/') || '/')
                      ORDER BY length(rtrim(w.workspace_root, '/')) DESC LIMIT 1) AS agent
               FROM (SELECT DISTINCT node FROM workspaces) n) m
      WHERE m.agent IS NOT NULL AND u.cwd IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM workspaces own WHERE own.node = u.node)),
    'unassigned') AS agent,
  COALESCE(
    (SELECT CASE WHEN count(DISTINCT c.wi) = 1 THEN '1:' || min(c.wi) WHEN count(*) > 0 THEN '1!' END
       FROM wi_claims c
      WHERE u.agent_id <> '' AND c.scope = 'subagent' AND c.status = 'accepted'
        AND c.session_ref = u.session_ref AND c.agent_id = u.agent_id),
    -- 回合声明：同回合里先后声明了不同 WI 时，每条用量取它之前最近的一次声明；
    -- 早于该回合全部声明的用量看全回合，多于一个不同 WI 即冲突。
    (SELECT CASE WHEN count(DISTINCT c.wi) = 1 THEN '2:' || min(c.wi) WHEN count(*) > 0 THEN '2!' END
       FROM wi_claims c
      WHERE u.turn_id <> '' AND c.scope = 'turn' AND c.status = 'accepted'
        AND c.session_ref = u.session_ref AND c.turn_id = u.turn_id
        AND (c.occurred_at = (SELECT max(d.occurred_at) FROM wi_claims d
                               WHERE d.scope = 'turn' AND d.status = 'accepted' AND d.session_ref = u.session_ref
                                 AND d.turn_id = u.turn_id AND d.occurred_at <= u.occurred_at)
             OR NOT EXISTS(SELECT 1 FROM wi_claims d
                            WHERE d.scope = 'turn' AND d.status = 'accepted' AND d.session_ref = u.session_ref
                              AND d.turn_id = u.turn_id AND d.occurred_at <= u.occurred_at))),
    -- 延续回合（turn_origins.origin='continuation'，见 turns.py）自己没有声明时，沿用同会话自上一个
    -- 非延续回合（人类请求或系统投递）起点以来、本事件之前最近的一次声明。
    (SELECT 'C:' || c.wi FROM wi_claims c
      WHERE u.turn_id <> '' AND c.scope = 'turn' AND c.status = 'accepted' AND c.session_ref = u.session_ref
        AND c.occurred_at <= u.occurred_at
        AND EXISTS(SELECT 1 FROM turn_origins o WHERE o.session_ref = u.session_ref AND o.turn_id = u.turn_id
                    AND o.origin = 'continuation')
        AND c.occurred_at >= (SELECT max(h.occurred_at) FROM turn_origins h
                               WHERE h.session_ref = u.session_ref AND h.origin <> 'continuation'
                                 AND h.occurred_at <= u.occurred_at)
      ORDER BY c.occurred_at DESC, c.claim_id DESC LIMIT 1),
    (SELECT CASE WHEN count(DISTINCT l.wi) = 1 THEN '3:' || min(l.wi) WHEN count(*) > 0 THEN '3!' END
       FROM t_launch l
      WHERE l.session_ref = u.session_ref AND l.wi IS NOT NULL
        AND l.occurred_at = (SELECT max(m.occurred_at) FROM t_launch m
                              WHERE m.session_ref = u.session_ref AND m.wi IS NOT NULL
                                AND m.occurred_at <= u.occurred_at)),
    (SELECT CASE WHEN count(DISTINCT h.wi) = 1 THEN '4:' || min(h.wi) WHEN count(*) > 0 THEN '4!' END
       FROM t_holder h
      WHERE h.session_ref = u.session_ref AND (h.turn_id = '*' OR (u.turn_id <> '' AND h.turn_id = u.turn_id))),
    (SELECT CASE WHEN count(DISTINCT l.job_id) = 1 THEN '5:cron:' || min(l.job_id) WHEN count(*) > 0 THEN '5!' END
       FROM t_launch l
      WHERE l.session_ref = u.session_ref AND l.kind = 'scheduled_session' AND l.job_id IS NOT NULL)
  ) AS wi_pick,
  -- 交互入口，按序取第一个有的：① 本事件之前最近一条 Relay 启动记录的渠道（feishu、telegram 等）；
  -- ② 本会话是定时任务（scheduled_session，或启动记录标 automation）→ 'automation'（App 显示"无"）；
  -- ③ 原生会话记录自带的入口（session_entrypoints，线①抽取：cli、sdk、codex_app、codex_exec、codex:<x>、
  --    pi_cli、relay、feishu、telegram 等）；④ 'unknown'。
  COALESCE(
    (SELECT l.entrypoint FROM t_launch l
      WHERE l.session_ref = u.session_ref AND l.entrypoint IS NOT NULL AND l.entrypoint <> 'automation'
        AND l.occurred_at <= u.occurred_at
      ORDER BY l.occurred_at DESC, l.receipt_id DESC LIMIT 1),
    (SELECT 'automation' FROM t_launch l
      WHERE l.session_ref = u.session_ref AND (l.kind = 'scheduled_session' OR l.entrypoint = 'automation') LIMIT 1),
    (SELECT e.entrypoint FROM session_entrypoints e
      WHERE e.session_ref = u.session_ref AND e.node = u.node AND e.runtime = u.runtime
      ORDER BY e.entrypoint, e.source LIMIT 1),       -- 同一会话多个信号（集合）：取 (entrypoint, source) 最小者
    'unknown') AS entrypoint,
  pm_model_key(u.model) AS model_key,
  (SELECT p.prompt || ',' || p.cache_read || ',' || p.cache_write || ',' || p.completion FROM prices p
    WHERE p.model_key = pm_model_key(u.model) AND p.effective_from <= date(u.occurred_at, '+8 hours')
    ORDER BY p.effective_from DESC LIMIT 1) AS rates
FROM v_usage u;

-- 最终事件：agent、wi、wi_source、entrypoint、usd_e12、state，以及未关联、未计价的具体原因。
-- 价表中缺缓存单价记 -1：该类 Token 为 0 时不影响，非 0 时整条未计价。
CREATE TEMP VIEW v_event_base AS SELECT
  r.node, r.runtime, r.event_id, r.request_id, r.source, r.granularity, r.session_id, r.session_ref,
  r.agent_id, r.turn_id, r.occurred_at, r.day, r.model, r.cwd,
  r.input_uncached, r.cache_read, r.cache_write, r.output, r.final,
  -- 以上都认不出 Agent、但事件已归到某个 WI 时，取该 WI 的 owner（如 Agent 从 CC 里起的 codex exec 子会话）。
  CASE WHEN r.agent <> 'unassigned' THEN r.agent
       ELSE COALESCE((SELECT o.owner FROM wi_owners o
                       WHERE substr(r.wi_pick, 2, 1) = ':' AND o.work_item_id = substr(r.wi_pick, 3)), 'unassigned')
       END AS agent,
  r.entrypoint,
  CASE WHEN substr(r.wi_pick, 2, 1) = ':' THEN substr(r.wi_pick, 3) ELSE 'unassigned' END AS wi,
  CASE WHEN r.wi_pick IS NULL OR substr(r.wi_pick, 2, 1) <> ':' THEN 'unattributed'
       WHEN substr(r.wi_pick, 1, 1) IN ('1', '2', 'C') THEN 'declared'
       WHEN substr(r.wi_pick, 1, 1) = '3' THEN 'classified'
       WHEN substr(r.wi_pick, 1, 1) = '4' THEN 'holder_supplement'
       ELSE 'scheduled_job' END AS wi_source,
  CASE WHEN substr(r.wi_pick, 2, 1) = ':' THEN NULL
       WHEN r.wi_pick = '1!' THEN 'subagent_marker_conflict'
       WHEN r.wi_pick = '2!' THEN 'turn_declaration_conflict'
       WHEN r.wi_pick = '3!' THEN 'launch_wi_conflict'
       WHEN r.wi_pick = '4!' THEN 'holder_wi_conflict'
       WHEN r.wi_pick = '5!' THEN 'scheduled_job_conflict'
       WHEN r.turn_id = '' THEN 'no_turn'
       WHEN EXISTS(SELECT 1 FROM wi_claims c WHERE c.scope = 'turn' AND c.status = 'pending'
                    AND c.session_ref = r.session_ref AND c.turn_id = r.turn_id) THEN 'declaration_pending'
       WHEN EXISTS(SELECT 1 FROM wi_claims c WHERE c.scope = 'turn' AND c.status = 'rejected'
                    AND c.session_ref = r.session_ref AND c.turn_id = r.turn_id) THEN 'declaration_rejected'
       WHEN (SELECT o.origin FROM turn_origins o WHERE o.session_ref = r.session_ref AND o.turn_id = r.turn_id
              LIMIT 1) = 'human' THEN 'human_request_undeclared'
       WHEN (SELECT o.origin FROM turn_origins o WHERE o.session_ref = r.session_ref AND o.turn_id = r.turn_id
              LIMIT 1) = 'delivery' THEN 'delivery_undeclared'
       WHEN (SELECT o.origin FROM turn_origins o WHERE o.session_ref = r.session_ref AND o.turn_id = r.turn_id
              LIMIT 1) = 'continuation' THEN 'continuation_without_declaration'
       ELSE 'no_declaration' END AS unlinked_reason,
  CASE WHEN substr(r.wi_pick, 2, 1) = ':' THEN NULL
       WHEN NOT EXISTS(SELECT 1 FROM t_launch l WHERE l.session_ref = r.session_ref) THEN 'none'
       WHEN NOT EXISTS(SELECT 1 FROM t_launch l WHERE l.session_ref = r.session_ref AND l.wi IS NOT NULL) THEN 'without_wi'
       ELSE 'after_event' END AS launch_evidence,
  r.model_key,
  CASE WHEN r.rates IS NULL OR r.input_uncached IS NULL OR r.cache_read IS NULL
            OR r.cache_write IS NULL OR r.output IS NULL THEN NULL
       ELSE pm_amount(r.rates, r.input_uncached, r.cache_read, r.cache_write, r.output) END AS usd_e12,
  CASE WHEN r.model IS NULL OR r.model = '' THEN 'model_unknown'
       WHEN r.input_uncached IS NULL OR r.cache_read IS NULL OR r.cache_write IS NULL OR r.output IS NULL
            THEN 'quantity_unknown'
       WHEN r.rates IS NULL THEN 'price_unavailable'
       WHEN pm_amount(r.rates, r.input_uncached, r.cache_read, r.cache_write, r.output) IS NULL
            THEN 'cache_price_unavailable'
       END AS unpriced_reason,
  CASE WHEN r.rates IS NULL OR r.input_uncached IS NULL OR r.cache_read IS NULL OR r.cache_write IS NULL
            OR r.output IS NULL
            OR pm_amount(r.rates, r.input_uncached, r.cache_read, r.cache_write, r.output) IS NULL THEN 'unpriced'
       WHEN r.wi_pick IS NULL OR substr(r.wi_pick, 2, 1) <> ':' THEN 'unlinked'
       ELSE 'known' END AS state
FROM v_event_raw r;

-- 人类请求未声明 WI（大鸟 11-21 定：不算异常）：wi='undeclared'、wi_source='undeclared'、
-- state='undeclared'，与真正未关联（wi='unassigned'、state='unlinked'）分开；其余列不变。
-- 四种状态 known / unlinked / undeclared / unpriced 互斥、覆盖全部事件。
CREATE TEMP VIEW v_event AS SELECT
  b.node, b.runtime, b.event_id, b.request_id, b.source, b.granularity, b.session_id, b.session_ref,
  b.agent_id, b.turn_id, b.occurred_at, b.day, b.model, b.cwd,
  b.input_uncached, b.cache_read, b.cache_write, b.output, b.final, b.agent, b.entrypoint,
  CASE WHEN b.unlinked_reason = 'human_request_undeclared' THEN 'undeclared' ELSE b.wi END AS wi,
  CASE WHEN b.unlinked_reason = 'human_request_undeclared' THEN 'undeclared' ELSE b.wi_source END AS wi_source,
  b.unlinked_reason, b.launch_evidence, b.model_key, b.usd_e12, b.unpriced_reason,
  CASE WHEN b.state = 'unlinked' AND b.unlinked_reason = 'human_request_undeclared' THEN 'undeclared'
       ELSE b.state END AS state
FROM v_event_base b;
