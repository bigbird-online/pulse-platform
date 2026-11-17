---
feature_name: "heartbeat-policy-path-hardening"
version: "0.0.1"
created: "2026-04-21T09:50:00+0800"
modified: "2026-04-21T09:50:00+0800"
upstream_dependency:
  - "features/heartbeat-policy-path-hardening/requirements.md"
  - "features/heartbeat-policy-path-hardening/design.md"
tags: [tasks, l2-spec, tdd]
---

# heartbeat-policy-path-hardening — 任务拆分

## 1. 任务总览

| # | 任务 | 优先级 | 工时 | 依赖 | 负责 | 状态 |
|---|------|-------|------|------|------|------|
| T1 | `harness/hooks/path_guard.py` + `tests/test_path_guard.py` | P0 | 5h | — | sub-agent | todo |
| T2 | `harness/dispatcher.py` 扩展：注入 --settings + _TOOL_DENIED_PATTERNS 新 pattern + 回归测试 | P0 | 3h | T1 | sub-agent | todo |
| T3 | `harness/templates/policies/{amanda,reeve,aki}-hardened.yaml` 三份样例 | P0 | 1h | — | sub-agent | todo |
| T4 | 集成测试 `tests/test_path_hardening_integration.py` | P0 | 3h | T1, T2 | sub-agent | todo |
| T5 | pyproject.toml / MANIFEST.in 声明 hooks + templates 为 package data（若需）| P0 | 0.5h | T1, T3 | sub-agent | todo |
| T6 | Ken 亲验 smoke（tmp workspace Write IDENTITY.md → denied） | P0 | 1h | T1-T5 | Ken | todo |

**总计**：~13.5h 工作量；sub-agent 真实运行 ~30-45min。

**并行**：T1 ∥ T3（无依赖）；T2/T4 依赖 T1；T5 依赖 T1+T3；T6 最后 Ken 亲验。

---

## 2. 任务详情

### T1 — path_guard.py hook 脚本

**描述**：独立可执行 Python 脚本，按 design.md §4 CMD-001/002 实现。stdlib only（json / sys / os / yaml / pathlib / fnmatch / re）。

**产出**：
- `harness/hooks/__init__.py`（空）
- `harness/hooks/path_guard.py`（含 shebang `#!/usr/bin/env python3`，chmod +x）
- `tests/test_path_guard.py`

**TDD 要求**（≥ 15 UT）：
- 见 design.md §7 单元测试清单
- 所有测试用 `subprocess.run(["python3", str(hook_path)], input=..., env=..., capture_output=True, text=True)` 黑盒测
- 测试环境 env 构造用 `{**os.environ, "HARNESS_POLICY_PATH": ..., "HARNESS_WORKSPACE": ...}`
- 覆盖率 ≥ 95%（纯 script 用 coverage 测有点特殊——可通过 `coverage run` 或重构为 library function + thin `if __name__ == "__main__"` 入口，library 部分做 UT）

**实现建议**：
- 把主要逻辑（`main()` / `match_whitelist()` / `allow()` / `deny()` / `allow_with_warn()`）做成函数，`if __name__ == "__main__": sys.exit(main())`
- 这样单元测试可以直接 `from harness.hooks.path_guard import match_whitelist` 测内部逻辑（白盒）+ 外层 subprocess 测（黑盒）双管齐下
- 纯 stdlib yaml：用 `yaml.safe_load`（yaml 是 harness 已有依赖，不算新增）

**验收**：
- ≥ 15 UT 全过
- hook 脚本可独立 `python3 path_guard.py < input.json` 跑通
- 覆盖率保持

---

### T2 — dispatcher 扩展

**描述**：按 design.md §4 CMD-003/004 改 `spawn_autonomous_session` + `_TOOL_DENIED_PATTERNS`。

**产出**：
- `harness/dispatcher.py` 改动（diff ≤ 100 行）
- `tests/test_dispatcher.py` 新 UT（≥ 5 条）

**改动要点**：
- import `tempfile`, `json`, `importlib.resources`
- `spawn_autonomous_session` 里增加 tmp settings 生成 + cmd 加 `--settings` + finally unlink
- `_TOOL_DENIED_PATTERNS` 追加 `HARNESS_PATH_DENIED:` pattern
- 降格路径（`importlib.resources` 失败 / tempfile 失败）：stderr warning + 不加 `--settings`

**TDD 要求**：
- UT 1：mock subprocess.run，断言 cmd 含 `--settings <path>`
- UT 2：tmp settings 文件内容结构（json 解析后含 env.HARNESS_POLICY_PATH + hooks.PreToolUse）
- UT 3：subprocess 结束后 tmp 文件被 unlink（`Path(tmp).exists() is False`）
- UT 4：降格场景 —— mock `importlib.resources.files` 抛异常 → cmd 不含 `--settings`
- UT 5：_detect_tool_denied 新 pattern → `_detect_tool_denied("foo\nHARNESS_PATH_DENIED:Write:/x\nbar")` 返回 `["Write"]`

**回归**：
- 现有 `test_dispatcher.py` + `test_dispatcher_integration_hygiene.py` 全过（863+ tests）
- dispatcher-context-hygiene 行为不变（两 feature 正交）

---

### T3 — 三 Agent 样例 policy

**描述**：创建 `harness/templates/policies/` 下三份样例，展示硬约束收紧后的 amanda.yaml / reeve.yaml / aki.yaml 写法。

**产出**：
- `harness/templates/policies/amanda-hardened.yaml`（基于现 amanda.yaml，`path_constraints.write: ["Doc/amanda-space/memo/**"]`）
- `harness/templates/policies/reeve-hardened.yaml`（reeve workspace 根推测 `PARA/reeve-space/`；`write: ["PARA/reeve-space/memo/**"]`；其他字段按 Amanda 模板裁）
- `harness/templates/policies/aki-hardened.yaml`（同上 Aki）

**注意**：
- **样例，不改 live policy**——Reeve/Aki 的 live policy 不一定存在或未 Amanda 格式对齐
- 每份 yaml 头部加 comment 说明这是"hardened 样例"，使用者需按 workspace 实际路径调整
- `harness policy validate <path>` 应能通过（即 yaml 结构有效）

**验收**：
- 3 份文件存在
- 直接用 `harness policy validate --path harness/templates/policies/amanda-hardened.yaml` 过（如果 CLI 有 --path 或等效参数；否则跑 Python 测试 `load_policy_from_path` 不抛错即可）

---

### T4 — 集成测试

**描述**：端到端验证 design.md §7 集成场景。

**产出**：`tests/test_path_hardening_integration.py`

**场景**（≥ 3 条）：
1. **平滑升级前**：tmp policy `write: ["<ws>/**"]`（宽）→ mock CC 调 hook 脚本（subprocess 启 hook） → Write `<ws>/IDENTITY.md` → hook allow → exit 0 空 stdout
2. **平滑升级后**：同 tmp policy 但 `write: ["<ws>/memo/**"]`（窄）→ hook 对同一 Write → deny + stderr `HARNESS_PATH_DENIED:Write:...`
3. **dispatcher 全链路 mock**：mock subprocess.run 返回 stderr 含 HARNESS_PATH_DENIED → dispatch 产生 AutonomousExecutionEvent.status=tool_denied + 触发 escalation with match_label=autonomous_tool_denied

**技术要点**：
- 用真实 `subprocess.run` 跑 hook 脚本（不 mock 内部）
- 用 `pytest.fixture` 建 tmp workspace + tmp policy + tmp settings
- 集成测试不真跑 claude（CC 二进制不一定存在）—— mock subprocess.run 的返回（stderr 构造）

**验收**：3+ 场景全过；覆盖率不降。

---

### T5 — pyproject.toml / MANIFEST 声明

**描述**：确保 `harness/hooks/*.py` 和 `harness/templates/**/*.yaml` 作为包资源被打包（不然 `importlib.resources.files("harness").joinpath("hooks/path_guard.py")` 找不到）。

**产出**：`pyproject.toml` 改（或 `MANIFEST.in`）

**要点**：
- 查 pyproject 当前 `[tool.setuptools]` / `[tool.setuptools.package-data]` 或等效
- 若 setuptools 且 `packages = find:` + `include_package_data = True` + MANIFEST.in 声明 `*.py` / `*.yaml` —— 最简
- 若 poetry 则 `[tool.poetry]` 下 `include = [ "harness/hooks/*", "harness/templates/**/*" ]`
- 重新 `pip install -e .` 后 `importlib.resources.files("harness").joinpath("hooks/path_guard.py").is_file()` 应返回 True

**验收**：
- 本地 venv `pip install -e .`（或已是 editable 情况下跳过）
- Python 里：`import harness, importlib.resources; assert importlib.resources.files("harness").joinpath("hooks/path_guard.py").is_file()`

---

### T6 — Ken 亲验 smoke

**描述**：Ken 在 tmp workspace 做真实实验：

**步骤**：

```bash
# Setup
TMP=$(mktemp -d)
mkdir -p $TMP/ws $TMP/.harness/policies

# 写 tmp policy（收窄 write）
cat > $TMP/.harness/policies/ken-test.yaml <<EOF
schema_version: "1.0"
agent_id: ken-test
frequency_hint: "*/30 * * * *"
workspace: $TMP/ws
scan_items:
  - type: pending_handoffs
    default_autonomy: autonomous_llm
    per_action: []
autonomous_llm_config:
  allowed_tools: [Write, Read]
  disallowed_tools: []
  path_constraints:
    read: ["**"]
    write: ["memo/**"]   # 只允许 memo 下
  max_duration_seconds: 60
  session_wrap_required: false
escalation:
  push_immediately: [red_line_flag]
  aggregate_daily: [critical]
  channel: telegram
  recipient: test
EOF

# 用 hook 脚本手跑（模拟 CC 给 stdin）
HARNESS_POLICY_PATH=$TMP/.harness/policies/ken-test.yaml \
HARNESS_WORKSPACE=$TMP/ws \
echo '{"tool_name":"Write","tool_input":{"file_path":"'$TMP/ws'/IDENTITY.md"}}' | \
python3 /path/to/harness/hooks/path_guard.py

# 期望：stdout 含 permissionDecision:deny；stderr 含 HARNESS_PATH_DENIED
# 再试一个允许的路径
HARNESS_POLICY_PATH=$TMP/.harness/policies/ken-test.yaml \
HARNESS_WORKSPACE=$TMP/ws \
echo '{"tool_name":"Write","tool_input":{"file_path":"'$TMP/ws'/memo/progress.md"}}' | \
python3 /path/to/harness/hooks/path_guard.py
# 期望：exit 0，stdout 空
```

**产出**：`ken-space/memo/verification-2026-04-21-path-hardening-selftest.md`，含：
- 实验命令 + 输出截取
- 断言：deny 场景 stdout JSON 结构 + stderr 前缀都对
- 断言：allow 场景 stdout 空 + exit 0
- 断言：_detect_tool_denied 对产出 stderr 的正则能捕获

---

## 3. 验收清单（交付时逐项勾）

- [ ] T1 hook 脚本 + ≥15 UT 全过
- [ ] T2 dispatcher 扩展 + ≥5 UT 全过 + 回归 863+ tests 全过
- [ ] T3 3 份样例 yaml + validate 通过
- [ ] T4 集成测试 ≥3 场景全过
- [ ] T5 pyproject package data 声明（importlib.resources 能找到）
- [ ] T6 Ken smoke 通过 + verification memo 签
- [ ] harness 包整体测试覆盖率保持 100%（新增 UT 覆盖新代码路径）
- [ ] 未改 Agent live policy / workspace
- [ ] 未启 cron

---

## 4. 风险与对策

| 风险 | 对策 |
|------|------|
| `importlib.resources` 找不到 hook（package data 未声明）| T5 显式检查；必要时用 `__file__` fallback |
| yaml 在 hook 脚本是 harness 已有依赖，但 hook subprocess 环境 PATH 可能找不到 | 用 `sys.executable` 确保同一 Python；pyyaml 是 harness 已依赖 |
| `**` glob 匹配 Python fnmatch 不原生支持 | T1 自定义 translate（design Decision-006）|
| Amanda 当前 amanda.yaml 路径是相对 repo root，hook 拿到 file_path 可能是绝对 | T1 双路 resolve（design Decision-005）|
| T6 真跑分身难（需 claude CLI 可用）| 降级为 hook 脚本单跑验证（不依赖 CC）|
| tempfile 清理异常导致 /tmp 泄漏 | finally + try/except OSError |

---

## 5. 执行顺序

```
T1 ────┐
       ├──→ T2 ──┐
       │         ├──→ T4 ──┐
T3 ────┤         │         │
       └──→ T5 ──┤         ├──→ T6（Ken）
                           │
          （可 ∥ T4）      │
```

sub-agent 单 session 内可完成 T1-T5，Ken T6 亲验在同 session 或下 session。
