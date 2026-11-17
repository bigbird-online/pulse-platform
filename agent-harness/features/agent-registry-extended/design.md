---
feature_name: "agent-registry-extended"
version: "0.0.1"
created: "2026-04-21T13:55:00+0800"
modified: "2026-04-21T13:55:00+0800"
upstream_dependency:
  - "features/agent-registry-extended/requirements.md"
tags: [design, l2-spec, ssot, registry]
---

# agent-registry-extended — 技术设计

> **概述**：`harness/ssot.py::AgentDecl` 扩 5 字段（Pydantic 默认值向后兼容）；`harness/ssot_engine.py` 新增 agent.id 一致性检测规则；`harness/cli.py` 新增 `registry show` 子命令组；零 schema 变更到 policy/relay config。

---

## 1. 技术栈

| 层 | 选型 | 备注 |
|----|------|------|
| schema | Pydantic v2 `AgentDecl` 扩字段 | 复用 |
| 校验 | Pydantic validators（`model_validator` / `field_validator`）| 复用现有风格 |
| YAML | yaml.safe_load | 复用 |
| CLI | Click（扩现有 `harness registry` 命令组；若无则新建）| 复用 |
| 测试 | pytest + pytest-cov | 复用 |

无新 pip 依赖。

---

## 2. 架构

```
harness/
├── ssot.py                     # 改：AgentDecl 扩 5 字段 + field_validator
├── ssot_engine.py              # 改：加 _check_agent_identity_consistency 规则
└── cli.py                      # 改：加 registry 子命令组（show + 未来 audit）

tests/
├── test_ssot.py                # 改：AgentDecl 新字段 UT
├── test_ssot_engine.py         # 改：agent.id 一致性 drift UT
└── test_cli_registry.py        # 新：registry show CLI UT

ssot/
└── registry.yaml               # 改：amanda/reeve/aki 迁移（填新字段）
```

---

## 3. 数据模型

### Model-001 — `AgentDecl` 扩展（harness/ssot.py）

```python
from typing import Literal

class AgentDecl(BaseModel):
    """Agent workspace declaration in the registry."""

    # 现有
    workspace: str
    identity: str
    depends_on: list[str] = []
    # 新增（wi_20260421_634 根因修复 — agent 身份 SSOT）
    tg_bot: str | None = None          # "@example_amanda_bot"
    tg_chat_id: str | None = None      # "1000000001"
    policy_path: str | None = None     # ".harness/policies/amanda.yaml"
    runtime_status: Literal["active", "bootstrap", "retired"] = "active"
    cron_enabled: bool = False

    @field_validator("tg_bot")
    @classmethod
    def _tg_bot_leading_at(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.startswith("@"):
            raise ValueError("tg_bot must start with '@'")
        return v

    @field_validator("tg_chat_id")
    @classmethod
    def _tg_chat_id_numeric(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.isdigit():
            raise ValueError("tg_chat_id must be all digits")
        return v

    @field_validator("policy_path")
    @classmethod
    def _policy_path_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.startswith(".harness/policies/"):
            raise ValueError("policy_path must start with '.harness/policies/'")
        if not v.endswith(".yaml"):
            raise ValueError("policy_path must end with '.yaml'")
        return v
```

### Model-002 — `RegistryDrift` 新增 `agent_identity_mismatch` 类

`harness/ssot_engine.py::DriftKind`（或现有 drift label 枚举）加一个值：`agent_identity_mismatch`。

现有 drift 事件 schema 不动；新增一个 severity=critical 的 label 值。

---

## 4. 接口

### CMD-001 — `ssot_engine._check_agent_identity_consistency(registry, harness_root)`

新函数（在 `ssot_engine.py`）：

```python
def _check_agent_identity_consistency(
    registry: SSOTRegistry,
    harness_root: Path,
) -> list[DriftEvent]:
    """Per-agent identity coherence: registry.key == policy.agent_id.

    For each ``registry.agents[<key>]`` with ``policy_path`` set:
      - Load the referenced policy yaml (best-effort)
      - Compare ``policy.agent_id`` with the registry key
      - Emit ``agent_identity_mismatch`` (critical) if they differ
    Retired agents with a still-present policy → warning-level drift.
    """
```

**失败容忍**：policy 文件缺失/损坏 → 单独 drift（severity=major，label=`policy_missing`），不抛异常阻塞整个 check。

### CMD-002 — `harness registry show <agent_id>` CLI

```bash
harness registry show amanda            # text
harness registry show amanda --json     # JSON 机读
```

text 格式（设计）：

```
Agent: amanda
  workspace:     Doc/amanda-space/
  identity:      Doc/amanda-space/IDENTITY.md
  tg_bot:        @example_amanda_bot
  tg_chat_id:    1000000001
  policy_path:   .harness/policies/amanda.yaml
  runtime_status: active
  cron_enabled:  true
  depends_on:    agent-constitution
```

json 格式：`AgentDecl.model_dump(mode="json")` + `{"id": "amanda", ...}` wrapper。

未知 agent_id → exit 1 + `Error: agent 'X' not in registry`。

---

## 5. 流程

### Flow-001 — registry load（zero change）

```
harness/any-command
    ↓
SSOTRegistry(**yaml.safe_load(registry.yaml))
    ↓
AgentDecl validators run (tg_bot / tg_chat_id / policy_path shape)
    ↓
Usage: ssot check / registry show / etc
```

老 registry（无新字段）→ 新字段默认值 → 无错误。

### Flow-002 — `harness ssot check` 含新规则

```
load registry
    ↓
_check_drift_against_filesystem (现有 projection drift)
    ↓
_check_agent_identity_consistency (新 — FR-P0-003)
    ↓
aggregate drift events
    ↓
report / exit code
```

新规则放最后（现有规则优先，向下兼容）。

### Flow-003 — `harness registry show`

```
parse args
    ↓
load SSOTRegistry
    ↓
lookup agents[agent_id] → AgentDecl
    ↓
render (text | json)
    ↓
exit 0 (or 1 if unknown)
```

---

## 6. 关键决策

### Decision-001 — 5 字段全部 optional

新字段全设 default（None / "active" / False），保证向后兼容。现有 registry.yaml 不改也 load 过。**一次性迁移** amanda/reeve/aki entry 在 L3 T5 单独做。

### Decision-002 — botToken 不入 registry

secret 分级管理：registry.yaml 作 monorepo 可公开元数据级别；botToken 是 secret，仍留 Relay config。

**原则**：registry 可在 git 全仓查，botToken 只在 config 本地（或 secret manager）。

### Decision-003 — tg_bot 只存 username，不存 token

`@example_amanda_bot` 是公开符号，可安全入 git。Relay 通过 botToken 解析出对应 username（grammy Bot.api.getMe），可与 registry.tg_bot 对照做 startup check（P1）。

### Decision-004 — policy_path 是 registry 单向引用

registry 指 policy，policy 不反指 registry（避免循环依赖）。policy 仍以 `.harness/policies/<id>.yaml` 命名约定为唯一锚点；registry 的 `policy_path` 提供显式声明，便于 harness ssot check 做一致性校验。

### Decision-005 — cron_enabled 手动维护（本期）

registry.cron_enabled 不自动从 cron-jobs.json 反推。手动维护（Agent 启 cron 时自己在 registry 更新，或 Ken 代更新）。理由：cron-jobs.json 归 PULSE 仓，本 feature 不跨仓库读。P1 可加 drift 规则（`registry.cron_enabled=true` 但 cron-jobs.json 里该 owner 无 enabled cron → drift）。

### Decision-006 — `registry` CLI 子命令组

现有 `harness ssot {check,register}`。新建 `harness registry {show}` 而非 `harness ssot show`：
- 语义区分：`ssot` 管 entries 段（SSOT 文档依赖图）；`registry` 管 agents 段（Agent 元数据）
- 未来扩展空间：`registry list` / `registry audit` / `registry set-status`

### Decision-007 — 不改现有 Registry validator 语义

现有 `validate_references` 检查 `agents.depends_on` 引用 `entries`。保留。新字段的 validators 独立在 `AgentDecl` 内，不跨字段校验。

---

## 7. 错误处理

| 场景 | 行为 |
|------|------|
| registry.yaml 新字段值类型错误 | Pydantic ValidationError（启动时抛，一致行为） |
| `runtime_status` 非枚举值 | ValidationError with Literal union 信息 |
| `tg_bot` 不以 `@` 开头 | field_validator ValueError |
| `tg_chat_id` 非纯数字 | field_validator ValueError |
| `policy_path` 格式错 | field_validator ValueError |
| `policy_path` 指向不存在的 yaml | `ssot check` 产 `policy_missing` drift（不是 load 报错） |
| policy 文件损坏 | 同上，drift 而非 crash |
| `harness registry show <unknown>` | exit 1 + Error 消息，不抛异常 |

---

## 8. 测试策略

### 单元测试

**test_ssot.py**（新增 UT）：
- AgentDecl 只含 3 旧字段 load 成功 + 新字段默认值
- 5 新字段各合法取值 load 成功
- `tg_bot` 不以 `@` 开头 → ValidationError
- `tg_chat_id` 非数字 → ValidationError
- `policy_path` 格式错（不以 `.harness/policies/` 开头 / 非 yaml）→ ValidationError
- `runtime_status` 非枚举 → ValidationError

**test_ssot_engine.py**（新增 UT）：
- registry.amanda.policy_path 指向 match 的 yaml → 无 drift
- registry.amanda.policy_path 指向 agent_id 不匹配的 yaml → drift critical
- registry.retired.policy_path 存在 + yaml 存在 → drift warning
- registry.amanda.policy_path None → skip（无 drift）
- policy 文件不存在 → drift `policy_missing`

**test_cli_registry.py**（新文件）：
- `registry show amanda` text 输出含全字段
- `registry show amanda --json` JSON 解析正确
- `registry show unknown` exit 1
- `registry show --help`

### 集成测试

- 迁移后的 registry.yaml（填 amanda/reeve/aki）通过 `harness ssot check`
- `registry show` 对迁移后 entry 输出非 None 全字段

### 回归

- 964 基线 tests 全过
- 现有 `registry.yaml` 不改（未迁移场景）能继续 load + `ssot check` pass

---

## 9. 部署

- 改动文件：`harness/ssot.py` / `harness/ssot_engine.py` / `harness/cli.py` / `tests/*` / `ssot/registry.yaml`
- 无 pip 依赖 / 无新 package data
- amanda/reeve/aki registry entry 迁移在 T5 单独 commit（便于 rollback）

---

## 10. 演进

- P1：Relay startup 读 registry 校验 tg_bot（PULSE 仓 feature，本 feature 提 schema 契约）
- P1：`registry.cron_enabled` 自动同步（drift 规则反推）
- P2：`harness registry audit` 全量健康扫描
- P2：runtime_status 状态机（bootstrap → active 审批流）
- P2：多租户扩展（per-tenant chat_id）
