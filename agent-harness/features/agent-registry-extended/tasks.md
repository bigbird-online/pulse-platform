---
feature_name: "agent-registry-extended"
version: "0.0.1"
created: "2026-04-21T14:05:00+0800"
modified: "2026-04-21T14:05:00+0800"
upstream_dependency:
  - "features/agent-registry-extended/requirements.md"
  - "features/agent-registry-extended/design.md"
tags: [tasks, l2-spec, tdd]
---

# agent-registry-extended — 任务拆分

## 1. 任务总览

| # | 任务 | 优先级 | 工时 | 依赖 | 负责 | 状态 |
|---|------|-------|------|------|------|------|
| T1 | `AgentDecl` 扩 5 字段 + field_validators + UT | P0 | 2h | — | sub-agent | todo |
| T2 | `ssot_engine._check_agent_identity_consistency` + UT | P0 | 3h | T1 | sub-agent | todo |
| T3 | `harness registry show` CLI + text/json 输出 + UT | P0 | 2h | T1 | sub-agent | todo |
| T4 | ssot check 集成（把 T2 规则接入现有流水线）+ 回归 | P0 | 1h | T2 | sub-agent | todo |
| T5 | 迁移 registry.yaml：amanda/reeve/aki 填新字段 + 通过 ssot check | P0 | 0.5h | T1-T4 | Ken | todo |
| T6 | Ken 亲验 smoke：registry show + 故意构造 drift 看拦截 | P0 | 0.5h | T5 | Ken | todo |

**总计**：~9h 编码 + Ken 1h 验收。

---

## 2. 任务详情

### T1 — AgentDecl 扩 5 字段

**描述**：按 design.md §3 Model-001 扩 `harness/ssot.py::AgentDecl`。

**产出**：
- `harness/ssot.py` 改（+5 字段 + 3 field_validators）
- `tests/test_ssot.py` 新增 UT（≥8 条）

**TDD**：
- 旧 registry（3 字段）load 成功 + 新字段默认值
- 5 新字段合法值 load 成功
- tg_bot 无 `@` → ValidationError
- tg_chat_id 非数字 → ValidationError
- policy_path 格式错 → ValidationError
- runtime_status 非枚举 → ValidationError
- None 值保持 None（validators 不崩）

**验收**：
- UT 全过
- 现有 registry.yaml load 不破（回归）

---

### T2 — ssot_engine 加 agent.id 一致性规则

**描述**：按 design.md §4 CMD-001 实现 `_check_agent_identity_consistency`。

**产出**：
- `harness/ssot_engine.py`（新函数 + DriftKind 扩一个值 `agent_identity_mismatch`）
- `tests/test_ssot_engine.py` 新增 UT（≥6 条）

**TDD**：
- registry.amanda.policy_path 指向 match 的 yaml → 无 drift
- agent_id 不匹配 → critical drift
- retired agent + policy 存在 → warning drift
- policy_path None → skip
- policy 文件缺失 → policy_missing drift（major）
- policy yaml 损坏 → policy_missing drift

**验收**：
- UT 全过
- drift event schema 未变（仅新 label）

---

### T3 — `harness registry show` CLI

**描述**：按 design.md §4 CMD-002 实现新子命令组。

**产出**：
- `harness/cli.py` 新 `@registry.group()` + `@registry.command()` show
- `tests/test_cli_registry.py` 新文件（≥5 UT）

**TDD**：
- `registry show amanda` text 输出含 "Agent: amanda" + 全字段
- `--json` 输出可 json.loads 且结构含 id + AgentDecl 字段
- unknown agent → exit 1 + "Error:" stderr
- --help 可用
- 迁移前老 registry（新字段 None）→ text 输出 "None" 或 "-"

**验收**：
- UT 全过
- CLI runner（Click `CliRunner`）assertion 对

---

### T4 — ssot check 集成

**描述**：把 T2 的新规则接入现有 `harness ssot check` 流水线。

**产出**：
- `harness/ssot_engine.py`（`check_drift` 或主入口调 `_check_agent_identity_consistency`）
- 集成测试 `tests/test_ssot_integration.py`（如有）新增场景

**TDD**：
- 完整 `ssot check` 调用对迁移后 registry → pass
- 故意改 policy.agent_id 制造不匹配 → check 报 critical drift + exit 非零

**验收**：
- harness 包 100% 覆盖率保持
- 964 基线 + 本 feature 新 UT 全过

---

### T5 — 迁移 registry.yaml

**描述**：update `ssot/registry.yaml::agents` 三 entry。

**产出**：
- `ssot/registry.yaml` 改动

**迁移内容**（Ken 写入前需确认实际值）：

```yaml
agents:
  amanda:
    workspace: Doc/amanda-space/
    identity: Doc/amanda-space/IDENTITY.md
    depends_on: [agent-constitution]
    tg_bot: "@example_amanda_bot"       # 待确认 bot username
    tg_chat_id: "1000000001"
    policy_path: .harness/policies/amanda.yaml
    runtime_status: active
    cron_enabled: true
  reeve:
    workspace: .harness/reeve-space/
    identity: .harness/reeve-space/IDENTITY.md
    depends_on: [agent-constitution]
    tg_bot: "<待查 relay.config>"
    tg_chat_id: "1000000001"
    policy_path: .harness/policies/reeve.yaml   # 若存在；不存在则 None
    runtime_status: bootstrap
    cron_enabled: false
  aki:
    workspace: PARA/aki-space/
    identity: PARA/aki-space/IDENTITY.md
    depends_on: [agent-constitution]
    tg_bot: "<待查 relay.config>"
    tg_chat_id: "1000000001"
    policy_path: .harness/policies/aki.yaml     # 若存在；不存在则 None
    runtime_status: active                       # 有早报 cron
    cron_enabled: true
```

**Ken 本 step 需要**：
- 读 `~/.relay/relay.json` 拿 amanda/reeve/aki 的 bot username（`getMe` 或者配置文件字段）
- 确认 reeve/aki policy 文件是否已存在
- 写入 registry.yaml

**验收**：
- `harness ssot check` pass
- `harness registry show amanda/reeve/aki` 输出含非 None 的字段（能填尽填）

---

### T6 — Ken 亲验

**描述**：按 Ken 自约束第 1 条，亲验 smoke。

**步骤**：

```bash
# 1. 正常场景
harness registry show amanda
harness registry show amanda --json | jq .
harness ssot check

# 2. 故意构造 drift（tmp 环境）
TMP=$(mktemp -d)
cp -r .harness $TMP/.harness
cp -r ssot $TMP/ssot
# 篡改 $TMP/.harness/policies/amanda.yaml 的 agent_id 为 reeve
# 跑 ssot check 看是否抓 critical drift
HARNESS_ROOT=$TMP/.harness harness --root $TMP/.harness ssot check
```

**产出**：
- `ken-space/memo/verification-2026-04-XX-agent-registry.md`

**验收**：
- 正常场景 pass
- 故意 drift 被抓 + exit 非零 + message 清晰

---

## 3. 验收清单（交付时逐项勾）

- [ ] T1 AgentDecl 扩 5 字段 + ≥8 UT 全过
- [ ] T2 `_check_agent_identity_consistency` + ≥6 UT 全过
- [ ] T3 `registry show` CLI + ≥5 UT 全过
- [ ] T4 ssot check 集成 + 回归全过
- [ ] T5 registry.yaml 迁移 + `ssot check` pass
- [ ] T6 Ken 亲验 smoke + verification memo
- [ ] harness 包 100% 覆盖率保持
- [ ] 964 基线全过（加本 feature 新增）

---

## 4. 执行顺序

```
T1 ──┬──→ T3 ──┐
     │         ├──→ T5 ──→ T6
     └──→ T2 ──┴──→ T4 ──┘
```

sub-agent 单 session T1-T4 可完成；Ken T5-T6 独立。

---

## 5. 风险与对策

| 风险 | 对策 |
|------|------|
| reeve/aki bot username 字段在 relay.config 叫不同 key | T5 前先确认；registry.tg_bot 字段是 symbol（可先写 placeholder，P1 从 Relay 反推校正） |
| reeve/aki 无 live policy 文件 | registry.policy_path 填 None；runtime_status=bootstrap；不影响 ssot check |
| registry.yaml 迁移破坏其他消费方 | 新字段 optional 默认值；老消费方只读 3 旧字段，不受影响 |
| CLI `registry` 命名与未来扩展冲突 | design §6 预留 `list/audit/set-status`；本期只做 show |

---

## 6. 非本期（backlog）

- **Relay startup 校验 tg_bot** — PULSE 仓独立 feature
- **cron_enabled 自动同步** — 新 drift 规则（对比 registry vs cron-jobs.json）
- **agent-workspace-builder skill 上线 checklist 更新** — Amanda 域
- **`registry audit` 全量扫描** — P2
