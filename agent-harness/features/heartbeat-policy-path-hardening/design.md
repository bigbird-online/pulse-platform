---
feature_name: "heartbeat-policy-path-hardening"
version: "0.0.1"
created: "2026-04-21T09:40:00+0800"
modified: "2026-04-21T09:40:00+0800"
upstream_dependency:
  - "features/heartbeat-policy-path-hardening/requirements.md"
  - "features/heartbeat-runtime/design.md"
tags: [design, l2-spec, hook, policy, path-constraint]
---

# heartbeat-policy-path-hardening — 技术设计

> **概述**：新增 `harness/hooks/path_guard.py` 独立脚本 + 扩展 `dispatcher.spawn_autonomous_session` 注入 `--settings <tmp>` 含 PreToolUse hook 配置 + 扩展 `_TOOL_DENIED_PATTERNS` 捕获 `HARNESS_PATH_DENIED:` 前缀。零 schema 变更，纯 Python stdlib。

---

## 1. 技术栈

| 层 | 选型 | 备注 |
|----|------|------|
| hook 脚本 | Python 3.11+ stdlib（`json` / `sys` / `os` / `yaml` / `pathlib` / `fnmatch`）| 与 harness 包依赖一致，无新 pip |
| Settings 文件 | JSON（CC 要求）| `json.dumps` 生成 |
| 子进程注入 | `subprocess.Popen` + `--settings` + `env` | 复用 dispatcher 已有 subprocess 模式 |
| 临时文件 | `tempfile.NamedTemporaryFile(delete=False)` + finally `os.unlink` | 显式清理更稳（防异常泄漏） |
| 包资源定位 | `importlib.resources.files("harness").joinpath("hooks/path_guard.py")` | Python 3.9+ 标准 |

---

## 2. 架构

```
harness/
├── hooks/
│   ├── __init__.py              # 新：空 package
│   └── path_guard.py            # 新：独立可执行脚本（CC hook entry）
├── dispatcher.py                # 改：spawn_autonomous_session 注入 --settings
└── policy.py                    # 不变（零 schema 变更）

tests/
├── test_path_guard.py           # 新：hook 脚本单元测试
└── test_dispatcher_path_hardening.py  # 新：dispatcher 注入 + 集成

templates/policies/              # 新：样例 policy yaml
├── amanda-hardened.yaml
├── reeve-hardened.yaml
└── aki-hardened.yaml
```

---

## 3. 数据流

```
dispatcher.spawn_autonomous_session(agent, policy, hits)
    │
    ├── hook_script_path = importlib.resources / "hooks/path_guard.py"
    ├── policy_abs_path = resolve(policy source yaml)
    ├── tmp_settings = build_tmp_settings(hook_script_path, policy_abs_path)
    │       │
    │       └── JSON: {
    │             "env": {"HARNESS_POLICY_PATH": policy_abs_path,
    │                     "HARNESS_WORKSPACE": str(policy.workspace)},
    │             "hooks": {
    │               "PreToolUse": [{
    │                 "matcher": "Write|Edit|NotebookEdit",
    │                 "hooks": [{"type": "command",
    │                            "command": str(hook_script_path)}]
    │               }]
    │             }
    │           }
    ├── cmd = ["claude", "-p", prompt,
    │          "--allowedTools", ...,
    │          "--disallowedTools", ...,
    │          "--permission-mode", "bypassPermissions",
    │          "--output-format", "text",
    │          "--settings", tmp_settings]   ← 新加
    ├── subprocess.run(cmd, ...)
    │       │
    │       └── CC 内调 Write → PreToolUse → path_guard.py 脚本执行
    │               │
    │               ├── allow → exit 0 空 stdout → Write 执行
    │               └── deny  → exit 0 + JSON deny + stderr "HARNESS_PATH_DENIED:..." → Write 被拒
    └── stderr 汇总 → _detect_tool_denied 捕获 HARNESS_PATH_DENIED → tool_denied event
```

---

## 4. 模块接口

### CMD-001 — `path_guard.py` 脚本

**执行**：`python3 /path/to/path_guard.py`（CC spawn 时自动调起，stdin/stdout/stderr 连通 CC）

**stdin**：CC 传入 JSON（structure 见 requirements §FR-P0-001）

**stdout**：
- allow：空
- deny：
  ```json
  {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"path not in write whitelist: <file_path>"}}
  ```

**stderr**：
- allow：无（或 warning）
- deny：`HARNESS_PATH_DENIED:<tool_name>:<file_path>`（便于 `_detect_tool_denied` 捕获 tool 名）
- 失败容忍 warning：`HARNESS_HOOK_WARN:<reason>`（不影响 exit）

**exit code**：始终 0（用 JSON 决策，不用 exit 2）

**主要逻辑**（伪代码）：

```python
def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        return allow("stdin parse failed")

    tool_name = data.get("tool_name", "")
    if tool_name not in ("Write", "Edit", "NotebookEdit"):
        return allow()  # 本 hook 只管写类工具

    file_path = data.get("tool_input", {}).get("file_path")
    if not file_path:
        return allow()

    policy_path = os.environ.get("HARNESS_POLICY_PATH")
    if not policy_path:
        return allow_with_warn("HARNESS_POLICY_PATH not set")

    try:
        with open(policy_path) as f:
            policy = yaml.safe_load(f)
    except Exception as e:
        return allow_with_warn(f"policy load failed: {e}")

    whitelist = (
        policy.get("autonomous_llm_config", {})
              .get("path_constraints", {})
              .get("write", [])
    )
    if not whitelist:
        return allow()  # 未配置 = 全开（平滑升级）

    workspace = os.environ.get("HARNESS_WORKSPACE") or policy.get("workspace", "")
    if match_whitelist(file_path, whitelist, workspace):
        return allow()

    return deny(tool_name, file_path)
```

### CMD-002 — Whitelist 匹配逻辑（`match_whitelist`）

```python
def match_whitelist(file_path: str, whitelist: list[str], workspace: str) -> bool:
    """
    file_path: CC 传来（通常绝对路径）
    whitelist: policy 里的 glob 列表（通常相对 workspace 或 repo root）
    workspace: policy.workspace 绝对路径
    
    算法：
      1. resolve file_path 到绝对路径（Path(file_path).resolve()）
      2. 对每个 whitelist pattern：
         a. 若相对路径 → join 到 workspace 或 repo_root，再 resolve
         b. fnmatch.translate(pattern) → regex（支持 ** 展开为 .*/）
         c. 检查绝对 file_path 是否 match
      3. 任一 match 即 allow
    """
```

**关键细节**：
- `**` 在 fnmatch 不原生支持。用自定义 translate：把 `**` 替换成 `(?:.*/)?` 再用 re.fullmatch
- workspace 锚点：policy.workspace 是绝对路径（amanda.yaml 里是 `/Users/owner/Coevolution/Doc/amanda-space`）
- whitelist pattern 里如果是 `Doc/amanda-space/memo/**`（相对 Coevolution 根）——这是老格式；与 `memo/**`（相对 workspace）并存可能。解析时两路试：
  1. 相对 workspace resolve
  2. 相对 repo root（Coevolution 根）resolve
  任一 match 即 allow。

### CMD-003 — `dispatcher.spawn_autonomous_session` 扩展

原函数签名不变，内部增加：

```python
# 新：找到 hook 脚本路径
import importlib.resources
hook_script = importlib.resources.files("harness").joinpath("hooks/path_guard.py")

# 新：找到 policy 文件路径
from harness.policy import policy_path
policy_file = policy_path(agent_id, harness_root=harness_root)

# 新：生成 tmp settings
tmp_settings = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", delete=False, prefix="harness-settings-"
)
try:
    settings_content = {
        "env": {
            "HARNESS_POLICY_PATH": str(policy_file),
            "HARNESS_WORKSPACE": str(policy.workspace),
        },
        "hooks": {
            "PreToolUse": [{
                "matcher": "Write|Edit|NotebookEdit",
                "hooks": [{"type": "command", "command": str(hook_script)}]
            }]
        }
    }
    json.dump(settings_content, tmp_settings)
    tmp_settings.close()
    settings_path = tmp_settings.name

    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", ",".join(cfg.allowed_tools),
        "--disallowedTools", ",".join(cfg.disallowed_tools),
        "--permission-mode", "bypassPermissions",
        "--output-format", "text",
        "--settings", settings_path,  # ← 新
    ]
    # ... subprocess.run 等 ...
finally:
    try:
        os.unlink(settings_path)
    except OSError:
        pass
```

**降格路径**：`importlib.resources` / tempfile 任一失败 → 走原 cmd（无 --settings），记 stderr warning，不阻塞 spawn（NFR-002）。

### CMD-004 — `_TOOL_DENIED_PATTERNS` 扩展

```python
_TOOL_DENIED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"tool\s+['\"]?([A-Za-z_][A-Za-z0-9_\-]*)['\"]?\s+is\s+not\s+allowed", re.IGNORECASE),
    re.compile(r"tool\s+['\"]?([A-Za-z_][A-Za-z0-9_\-]*)['\"]?\s+not\s+allowed", re.IGNORECASE),
    re.compile(r"permission\s+denied.*?tool[:\s]+([A-Za-z_][A-Za-z0-9_\-]*)", re.IGNORECASE),
    re.compile(r"HARNESS_PATH_DENIED:([A-Za-z_][A-Za-z0-9_\-]*):", re.IGNORECASE),  # ← 新
)
```

hook 脚本 stderr 格式 `HARNESS_PATH_DENIED:Write:/abs/path` → regex 捕获 `Write`。`tool_denied_calls` 里体现为 Write。

---

## 5. 关键决策

### Decision-001 — 零 schema 变更

不新增 policy 字段。`autonomous_llm_config.path_constraints.write` 原本就是文字数据，本 feature 让它真执行。Amanda 平滑升级（改窄值即生效）。避免 Pydantic migration + 446 存量文件兼容成本。

### Decision-002 — hook 脚本独立可执行（不 import harness）

hook 脚本不通过 `python -m harness.hooks.path_guard`，而是 `python /abs/path/path_guard.py` 直调。

**理由**：
- CC hook 命令字符串希望简洁（绝对路径单命令）
- 避免 PYTHONPATH 依赖 harness 包安装状态
- 脚本应能在"没 harness venv 激活"的 shell 里独立跑（CC subprocess 环境干净）
- shebang `#!/usr/bin/env python3` + 用 `sys.path` 注入（或根本不 import harness）

**代价**：hook 脚本不能 import harness 里的复杂模型（policy.HeartbeatPolicy Pydantic）——只能用 yaml.safe_load 直接读 dict。

### Decision-003 — 失败永远 allow（NFR-002）

hook 任何异常 / policy 缺失 / env 未设 → allow + stderr warning。

**理由**：hook 故障不应让 cron 分身工作中断。宁可放宽一次（workspace 层还有 Amanda `accept=接手即锁` + dispatcher-hygiene 双重兜底），不能让 Agent 因 hook bug 彻底做不了事。

观察期（Amanda smoke）如果发现 warn 泛滥，再加 hard-fail 选项。

### Decision-004 — `--settings <tmp>` 而非改 Agent 原 settings

永不碰 Agent workspace 的 `.claude/settings.json`。tmp settings 住 `/tmp/harness-settings-*.json`，subprocess 结束后清理。

CC merge 机制保证：tmp settings 的 hook 追加到 Agent 原有 hook 后。不冲突、不覆盖。

### Decision-005 — workspace 锚点两路 resolve

path whitelist 里的 glob 可能：
- 相对 workspace（Amanda 新格式 `memo/**`）
- 相对 repo root（Amanda 当前格式 `Doc/amanda-space/memo/**`）

hook 对两路 resolve 后都 match 一次。任一 match 即 allow。

**理由**：Amanda 当前 amanda.yaml 用 repo-root 相对路径。要求她升级前把所有路径格式改齐会拖慢采纳。两路兼容让"升级路径"简单。

### Decision-006 — **/ 自定义 translate

`fnmatch.translate` 把 `**` 当作两个 `*` 处理（都只匹配单层）。path whitelist 的 `**` 应匹配多层目录。

实现：在 fnmatch 调用前先把 `**/` → sentinel → fnmatch.translate 后 sentinel → `(?:.*/)?`。或直接手写 translate。选后者（简单）。

### Decision-007 — 不动现有 hook 触发的其他事件

CC hook 机制（PostToolUse / SessionStart / Stop 等）本 feature 只用 PreToolUse。其他事件不注入（减少表面、降低 settings 合并风险）。

---

## 6. 错误处理

| 场景 | 行为 |
|------|------|
| stdin 非 JSON / 解析失败 | allow + stderr `HARNESS_HOOK_WARN:stdin parse` |
| `HARNESS_POLICY_PATH` 未设 | allow + stderr warning |
| policy 文件缺失 / 损坏 | allow + stderr warning |
| write 白名单为空/缺失 | allow（语义 = 全开，兼容老 policy） |
| tool_name 非写类 | allow（无 warn） |
| file_path 字段缺失 | allow（无 warn） |
| glob match 异常（regex 构造失败）| allow + stderr warning |
| `importlib.resources` 找不到 hook 脚本 | dispatcher 降格 spawn 不注入 settings + stderr warning |
| tempfile 创建失败 | dispatcher 降格同上 |

**原则**：所有失败路径都 allow，保证分身可工作；严格边界 靠 observability 发现（stderr warning 积累触发 escalation）。

---

## 7. 测试策略

### 单元测试 `tests/test_path_guard.py`

用 `subprocess.run(["python", hook_script], input=json_str, env=...)` 黑盒测：
- Write + in whitelist → exit 0 / stdout "" / stderr ""
- Write + not in whitelist → exit 0 / stdout JSON deny / stderr `HARNESS_PATH_DENIED:Write:...`
- Edit / NotebookEdit 同上
- Read 等非写类 → allow
- file_path 缺失 → allow
- stdin 空 / 非 JSON → allow + stderr warning
- HARNESS_POLICY_PATH 不设 → allow + stderr warning
- policy 文件缺失 / 损坏 → allow + stderr warning
- `**` glob 多层匹配 → allow
- 白名单 `memo/**`（相对 workspace）+ file_path 绝对路径 → allow（workspace resolve）
- 白名单 `Doc/amanda-space/memo/**`（相对 repo 根）+ file_path 绝对 → allow（双路 resolve）
- 白名单外 → deny

≥ 15 条。

### 单元测试 `tests/test_dispatcher_path_hardening.py`

mock subprocess.run，断言：
- cmd 含 `--settings <path>`，且 path 存在
- tmp settings 文件内容含 PreToolUse + path_guard 绝对路径 + HARNESS_POLICY_PATH env
- subprocess 结束后 tmp 文件被 unlink
- importlib.resources 失败时降格（cmd 不含 --settings + stderr warning）
- `_detect_tool_denied("...HARNESS_PATH_DENIED:Write:/x...")` 返回 ["Write"]

≥ 5 条。

### 集成测试 `tests/test_path_hardening_integration.py`

不 mock 整体：真跑 hook 脚本（subprocess），但 **mock CC**（dispatcher spawn 测试时 CC 本身不会跑起来，只测 cmd 构造 + stderr 处理）。

场景：
- 全链路：dispatcher.dispatch 触发 spawn_autonomous_session → mock subprocess 返回 stderr 含 `HARNESS_PATH_DENIED:Write:/x` → dispatch 产生 tool_denied event + escalation
- 平滑升级 before/after：policy write 收窄前后对比

### Ken 亲验 smoke

tmp workspace：
- mock CC（用一个 fake python 脚本假装 claude，接收 stdin/args/env，调 hook 脚本，打印 stderr）
- 或跳过 CC 全链路，只手跑 hook 脚本验 JSON 格式
- 验证 disposition + event + escalation 完整

产出 `verification-2026-04-21-path-hardening-selftest.md`。

---

## 8. 部署

- **文件改动**：
  - 新增 `harness/hooks/__init__.py` + `hooks/path_guard.py`
  - 新增 `harness/templates/policies/*.yaml`（3 份）
  - 改 `harness/dispatcher.py`（spawn_autonomous_session + _TOOL_DENIED_PATTERNS）
  - 新增 3 个测试文件
- **pyproject.toml**：可能需要声明 `harness/hooks/` 和 `harness/templates/` 作为 package_data（含 .py / .yaml non-code）→ 需核对现有 pyproject 配置
- **配置**：Amanda **后续自己**改 amanda.yaml 的 `path_constraints.write`（不是本 session 做；Amanda 后续清单已列）
- **上线顺序**：L3 全过 → Ken smoke → commit feature/doc-para → 回 Amanda complete wi_072

---

## 9. 演进

- 若 smoke 发现 warn 泛滥（hook 失败频繁）→ 加 hard-fail 选项 or 改默认行为
- 若 Amanda/Reeve/Aki 采纳后发现某些合法写操作被误拦 → 扩 whitelist 或者加 per-Agent 例外机制
- P1：read 硬约束（同构扩展）
- P1：Bash 子命令路径约束（需研究 tool_input.command 解析）
