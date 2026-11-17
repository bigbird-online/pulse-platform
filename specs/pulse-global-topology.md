# SPEC：PULSE 全局 — 生产拓扑（配置真源 · HTTP 面 · systemd · 重启纪律 · 双机边界）

> version: `0.3.0`
> status: `reverse_engineered_from_code`
> authority: 平台部署与运行拓扑的**现行契约**（代码 + 仓内 unit 文件反推；live 值只作 manual 步骤不作断言）
> owner: Pulse
> modified: `2026-11-10`
> 版本史：只看 git log

**live 边界声明**：cron 定义、relay 配置及凭据由主机运行态维护，当前仓库不携带
`data/cron-jobs.json`。CI 使用独立合成数据验证命令和约束；现场配置与进程是否加载
须通过实际主机读回验证，合成测试通过不能充作生产配置验收。

## 1. 配置真源（默认路径已归一至 config-paths.ts）

- [x] relay.json / cron-jobs.json / metering 目录的解析序：显式参数 → env
  （`RELAY_CONFIG_PATH` / `CRON_JOBS_PATH` / `METERING_DIR`）→ 代码默认，
  **三段优先级由 `config-paths.ts` 单一真源实现**，三入口（`startRelay` /
  provision CLI / relay-jobs CLI）全部走它。代码默认值不再是绝对路径常量，
  而是从本模块位置回推仓库布局（`resolvePulseRoot()` → `<PULSE>/data/`）
  `@anchor symbol RELAY_CONFIG_PATH`
  `@anchor symbol resolveConfigPath`
  `@anchor symbol resolveCronJobsPath`
  `@anchor symbol resolvePulseRoot`
  `@anchor test Relay/tests/config-paths.test.ts :: 裸跑（无参无 env）落在仓内 data/ 且不含 /opt/PULSE`
  （债已还，2026-08-10 / wi_20260810_949。原状：主进程 `/opt/PULSE/data/relay.json`、
  provision CLI `$HOME/.relay/relay.json`、relay-jobs 用 dist 相对路径三处互不一致，
  live 靠 systemd Environment 覆盖才收敛，裸跑即发散。选值理由——`/opt/PULSE`
  两机均不存在；`$HOME/.relay/` 是运行态目录不是配置真源，拿它当默认会静默读到
  陈旧副本。防复发的闸是 `config-paths.test.ts` 的源码级断言：三入口源码里不得再
  出现自带的默认路径字面量或直读这三个 env 键）
- [x] relay.json 顶层恰两字段（users 必填 + bindings 可选）；AgentConfig 全集见
  类型定义，启动闸强制每 agent 至少一个通道凭据且 channel 与凭据匹配，
  feishu openId 缺失只走配对不阻塞启动
  `@anchor symbol RelayConfig`
  `@anchor symbol UserConfig`
  `@anchor symbol AgentConfig`
  `@anchor symbol validateConfig`
- [x] provision 写配置走备份 + 临时文件原子重命名
  `@anchor symbol provision`
  `@anchor manual 备份惯例 :: provision.ts:128-141 备份到 dirname(configPath)/backups/、临时文件 ${configPath}.provision.tmp；与人工惯例 relay.json.bak-<ts>-<why> 并存`
- [x] cron-jobs.json：加载失败**静默降级空数组不抛**；v1→v2 迁移给无 owner 的 job
  补 `owner='system'` 并回写；运行态 `_state` 分离到独立 state 文件
  `@anchor symbol loadJobs`
  `@anchor symbol CRON_JOBS_STATE_PATH`
- [x] 显式平台 epoch cutover 输入由 manifest 冻结**完整 job 集、owner、
  enablement、execution mode 与目标文件 SHA**；所有 `harness_dispatch` 定义必须显式
  `platformEpochPolicyRequired=true`，被 quarantine 的分类不得启用。验证器对缺 job、
  多 job、分类误开与 epoch gate 缺失均 fail-closed
  `@anchor symbol validate_cutover`
  `@anchor symbol QUARANTINED_CLASSES`
  `@anchor symbol platformEpochPolicyRequired`
  `@anchor test Scripts/tests/test_platform_epoch_cutover.py :: test_synthetic_cli_contract_passes`
  `@anchor test Scripts/tests/test_platform_epoch_cutover.py :: test_quarantined_job_cannot_be_enabled`
- [x] 生产 Relay deploy **串行且可补偿**：经容量准入发布精确源码的不可变制品，
  保留运行配置；不重放历史 cron / epoch 切换。部署驱动在激活或健康检查失败时
  调用发布器回滚，并核对先前进程；本次 SCL 采用仅按本次回执补偿。
  并发 deploy 排队而非 cancel-in-progress。
  `@anchor test Scripts/tests/test_deploy_live_root_contract.py :: test_tcloud_deploy_uses_the_same_release_writer_contract`
  `@anchor test Scripts/tests/test_deploy_live_root_contract.py :: test_tcloud_source_only_failure_never_restarts_the_resident_service`
  `@anchor manual deploy transaction :: .github/workflows/deploy-tcloud-relay.yml 核 concurrency.cancel-in-progress=false 及失败回滚步骤`
- [x] system job 特例分支（对所有 owner 可见 + `[system]` 标签）以
  `owner === 'system'` 字符串比较实现
  `@anchor manual live 恒不触发 :: jq '[.jobs[].owner] | unique' data/cron-jobs.json——2026-08-10 实测零条 system，该分支 live 下是死路径；分支本体见 relay-jobs.ts:77 与 job-commands.ts:17`

## 2. HTTP 面（内部接口 + Web Terminal）

- [x] 端口不是常量：env `RELAY_HTTP_PORT` 必须是 1..65535 整数，否则 HTTP 面
  **整个不启动**；绑定 host 默认 `127.0.0.1`
  `@anchor symbol RELAY_HTTP_PORT`
  `@anchor symbol RELAY_HTTP_HOST`
- [x] 路由表七组：/internal/health、/internal/cc-status、/internal/pairing/approve、
  /internal/agents/reload、/internal/push、/internal/heartbeat（GET|POST）、
  /terminal/:token（HTML+WS），其余 404
  `@anchor test Relay/tests/relay-http.test.ts :: "returns ok with uptime + bot count"`
  `@anchor symbol checkInternalToken`
- [x] 鉴权域**不均匀**：push、heartbeat（含 GET）、health 与 cc-status 过
  `checkInternalToken`；pairing approve 与 agents reload 仍不过鉴权。live 虽绑
  `0.0.0.0`，但**公网到不了**——
  ufw INPUT 默认 DROP，放行清单只有 22/tcp 与 tailscale0 接口，无 8787 规则。
  故网络层当前暴露面 = 本机 + tailscale 网内，不是公网；即使误放行 8787，前述
  四类读写面也会在应用层拒绝无 token 请求。pairing/reload 的残余债仍不可据此消失
  `@anchor test Relay/tests/relay-http.test.ts :: "rejects a request without the internal token"`
  `@anchor manual 鉴权域核对 :: http-server.ts route() 分发处核 checkInternalToken 调用点——health、cc-status、push、heartbeat 有，pairing approve、agents reload 无`
  `@anchor manual 公网不可达实测 :: 2026-08-10 从 ownermac 绑物理网卡（--interface <en 网卡 IP>，绕开默认走 VPN 的路由）实测：22 端口有响应、8787 连接超时且 nc 失败。复算必须绑物理网卡——对端默认路由走 utun 隧道，不绑就是 VPN 内网路径不算公网实测（同族教训：本机打自己公网 IP 的 200 是回环假象）`

## 3. systemd unit（仓内 unit 文件为断言对象）

- [x] 重启策略：Restart=always + RestartSec=5；10 分钟窗口 3 次失败进 failed 态并
  触发 OnFailure 告警服务（规避 Relay 自指死结）
  `@anchor const RestartSec = 5`
  `@anchor const StartLimitIntervalSec = 600`
  `@anchor const StartLimitBurst = 3`
  `@anchor const OnFailure :: "relay-down-notify.service"`
- [x] **无内存上限**：unit 无 MemoryMax/MemoryHigh/MemoryLimit（2026-07-16 真 OOM
  的已知债，本锚点在债还清时应转正为 const）
  `@anchor absent MemoryMax :: 已知债——加上限前本断言应 holds；加了请改成 const 锚点`
- [x] 环境注入两层：Environment= 显式键 + EnvironmentFile=-relay.env（`-` 前缀
  缺失不报错）；ExecStartPre 有 dist 新鲜度闸（陈旧自动 build，
  仅编译失败拒启动）；ExecStartPre/ExecStopPost 写 restart-audit.log 的
  START/STOP 行与进程级审计互补
  `@anchor const ExecStartPre :: "ensure-dist-fresh.sh"`
  `@anchor symbol RESTART_AUDIT_LOG`
  `@anchor symbol writeAuditLine`
- [x] 仓内 unit 与 live 副本必须同步（部署即拷贝）。`vultr-pulse-space/systemd/user/relay.service`
  是已退役 Vultr 的副本，只作源码合同；TCloud 的 Relay 由原生 CD 安装
  `@anchor manual 同步核对 :: TCloud cmp vultr-pulse-space/systemd/user/dual-heartbeat-watcher.service ~/.config/systemd/user/dual-heartbeat-watcher.service && echo in-sync`

## 4. 重启与治毒纪律载体

- [x] 安全重启脚本退出码契约：0 成功或 check 无 busy / 2 busy 拒绝 /
  3 HTTP 不可达且无 --force / 4 autonomous 分身越权（`HARNESS_TRIGGER_MODE=cron`
  且非 --check 即拒）/ 1 其他（含端口真源缺失）。
  （原「脚本头注释未列 4」的两义已消：2026-08-10 补齐头注释 / wi_20260810_949）
  `@anchor test Relay/tests/restart-safe-script.test.ts :: HARNESS_TRIGGER_MODE=cron 且非 --check 时退 4`
  `@anchor const STATUS_URL :: "/internal/cc-status"`
  `@anchor const HARNESS_TRIGGER_MODE = "cron"`
- [x] busy 探测打 `/internal/cc-status` 取 counts.busy。端口**单一真源**：
  调用方已导出的 `RELAY_HTTP_PORT` → `relay.env`（与 systemd EnvironmentFile 同一份）
  → 两处都没有则**报错退出 1，不猜默认值**。脚本内不再写 `8787` 兜底
  `@anchor symbol RELAY_ENV_FILE`
  `@anchor test Relay/tests/restart-safe-script.test.ts :: 两处都没有端口时报错退出，不猜一个默认值`
  （债已还，2026-08-10 / wi_20260810_949。不留兜底的理由：Relay 自身
  `resolveRelayHttpPort()` 把「未设」定义为「HTTP 面整个不启动」，没有 8787 这个
  隐含默认；脚本猜端口只会把「配置缺失」伪装成「服务不可达」，再 `--force` 就杀掉
  在跑的会话）
- [x] busy 探测的鉴权 token 同样取调用方已导出的 `RELAY_INTERNAL_TOKEN`，否则从
  `relay.env` 读取；只作为 `x-relay-token` 请求头传递，不写审计日志或命令输出。
  未配置 token 时兼容 Relay 的开发态空配置放行；生产服务端若有 token 而脚本取不到，
  HTTP 401 仍沿既有不可达分支 fail-closed
  `@anchor test Relay/tests/restart-safe-script.test.ts :: "从 relay.env 读取 token 并只作为请求头传给 curl"`
- [x] busy 探测**fail-closed**：探不到状态（HTTP 不可达/超时）时 `--check` 退 3，
  绝不报「无 busy」放行
  `@anchor test Relay/tests/restart-safe-script.test.ts :: HTTP 不可达时 --check 退 3，且绝不打印「no busy」`
  （2026-08-10 实测捉到的 fail-open，同批次修复：原 curl 调用带 `--show-error` 且
  `2>&1`，错误文本混进取值使哨兵串比对失配 → exit 3 闸整条跳过 → 死掉的 Relay
  也打印 `OK: no busy` 并退 0。配对反转已验：撤掉修复同一输入即回到 exit 0）

## 5. 运行时优先级与 Codex 平台默认权限

- [x] PULSE 源码中**新建/托管配置的主路缺省**为 `gpt-6-astra/xhigh`；只有逐端 runtime、
  模型目录、服务 binding 与真实对话 UAT 全部读回后，才能声称对应生产端完成迁移，旧 session
  仍必须另按 runtime 计数。Relay conversation、agent-harness 与 CLI 全局兜底取该档；runtime
  低于 Codex 0.153.4 或目录无 Astra 时 fail-closed。Stats quota probe 单独读取 active
  `runtime-cost-route.json` 与 schema-v2 `compute-profiles.json`：phase 2 canary 已在生产把它切到
  `economy`，并由真实 Luna/low session 与 canonical exporter 验收；active route、catalog 或
  模型缓存缺失/坏契约时仍禁用 probe，不回退主路。phase 23 已激活并由 Relay
  加载；Hestia 周复盘父路由与产物已验收，但嵌套 Duke 裁决出现两条
  Sol/xhigh 重入且账未收口，所以端到端节省仍是未证。direct cron 自 2026-08-16
  起不再把模型字面量物化进 job，而引用中央 `compute-profiles.json`：多数 job 为
  `platform_default`，Hestia 30 分钟 canary 为 `media`，且 0 新增时机械 gate 不启动 LLM。
  该中央目录自 schema v2 起也不保存 Codex 具体型号，只保存 `codex-astra-latest` /
  `codex-terra-latest` 等逻辑 alias，并从 Codex 当前模型目录 fail-closed 解析；具体型号只作为
  live argv 时点证据。其他 consumer 仍保留 v1 具体型号，不能写成全平台均已 alias 化。
  live 运行态仍只能用 manual 观察步骤核，不把时点快照写成断言
  `@anchor const DEFAULT_PULSE_CODEX_MODEL = 'gpt-6-astra'`
  `@anchor const DEFAULT_PULSE_CODEX_REASONING_EFFORT = 'xhigh'`
  `@anchor manual live-codex-defaults :: systemctl --user show relay.service -p Environment; jq 检查 data/relay.json 与 data/cron-jobs.json 的 computeProfile；读取 Relay/config/compute-profiles.json；grep 检查 /Users/owner/Coevolution/.harness/policies/*.yaml 与 /home/pulse/.codex/config.toml；最后按 /home/pulse/.relay/state.json 的 session runtime 分组计数，禁止用配置计数代替存量会话计数`
- [x] 跨模块管理真源包括 `data/runtime-priority.json` 与 tracked
  `ComputeCost/config/runtime-cost-presets.json`；后者由管理器物化为 active
  `data/runtime-cost-route.json`。管理器以 plan/apply/verify/rollback 同时收敛 Relay、
  harness、Stats 与 Codex 用户配置。Codex YOLO on 的运行时实参是
  `--dangerously-bypass-approvals-and-sandbox`，不是当前 CLI 不支持的 `--yolo`；
  `/yolo` 仅是 Relay 会话命令
  `@anchor symbol resolvePulseRuntimePriority`
  `@anchor symbol _materialize_cost_route`
  `@anchor symbol _resolve_codex_probe_route`
  `@anchor test Relay/tests/codex-defaults.test.ts :: reads runtime order, model, and reasoning from the shared profile`
  `@anchor manual 管理器闭环 :: python3 ComputeCost/bin/pulse-runtime-priority.py verify；必须同时报告 agents/direct jobs/policies/harness_dispatch 排除计数并退出 0`

## 6. 双机边界

- [x] 运行节点（2026-10-18 起）：ownermac（macOS，launchd）与 TCloud（Linux，原生 CD）
  两个常驻 Relay，均单轨运行原生核心。Vultr 已退役：租户与机器人迁往 TCloud，Relay
  停用；bigbird-pulse 的 Relay 与 Platform 已停用，同机 CWA CI runner 保留。整机宕机
  告警由 TCloud `dual-heartbeat-watcher` 巡检 ownermac 探针心跳并经飞书推送。
  `@anchor manual 节点读回 :: ownermac launchctl print gui/$(id -u)/com.pulse.relay；TCloud systemctl --user is-active relay.service dual-heartbeat-watcher.timer`

- [x] 代码里**没有 hostname 判断**；机器区分一律 `process.platform === 'darwin'`，
  分支点两处（Web Terminal URL 构造与 SSH resume 命令回退），且
  `RELAY_PUBLIC_HOST` 存在时 URL 分支短路
  `@anchor symbol RELAY_PUBLIC_HOST`
  `@anchor symbol getSshAlias`
  `@anchor manual 无 hostname 分支 :: grep -rn "hostname" Relay/src/ 应零命中（环境判定纪律在人不在码——hostname 判环境是 CLAUDE.md 层约定）`
- [x] macOS 端由 plist + 同步脚本承载（launchd 无 ExecStartPre/StopPost 钩子，
  故进程级审计在代码内补齐、两端写同一审计文件）
  `@anchor manual macOS 载体在盘 :: ls Relay/scripts/ai.pulse.relay.plist Relay/scripts/sync-macos-launch.sh 两件均在；launchd 侧行为只能在 ownermac 真机验（GUI/launchd 经 ssh 不可驱动）`
- [x] Codex 沙箱的额外可写根（`--add-dir`）由 `resolveCodexAdditionalWritableRoots()`
  管理，不再写死 `/Users/owner/Coevolution`。**这是安全边界**：只有 canonical
  `<coevo>/Bigbird/PULSE` 布局才自动上溯到 `<coevo>`；受管实例（设了 `PULSE_RELAY_PATHS`）
  的非 canonical 布局收窄到 PULSE 仓根；独立部署（两者皆无）不加额外写根，Codex 只写
  Agent 工作区。非 canonical 正式部署若确需 monorepo 写域，须显式设置 `PULSE_COEVOLUTION_ROOT`
  `@anchor symbol resolveCoevolutionRoot`
  `@anchor symbol resolveCodexAdditionalWritableRoots`
  `@anchor symbol PULSE_COEVOLUTION_ROOT`
  `@anchor symbol DEFAULT_ADDITIONAL_WRITABLE_ROOTS`
  `@anchor test Relay/tests/config-paths.test.ts :: canonical <coevo>/Bigbird/PULSE 才自动上溯，否则收窄为 PULSE 根`
  `@anchor test Relay/tests/config-paths.test.ts :: 显式 PULSE_COEVOLUTION_ROOT 可为非 canonical 部署授权精确根`
  `@anchor test Relay/tests/config-paths.test.ts :: 额外可写根：显式 env 取该根；canonical 取 monorepo 根；受管实例保持 PULSE 根；独立部署为空`
  （2026-08-10 / wi_20260810_949 去除硬编码；2026-08-20 / wi_20260820_136 的隔离
  worktree 回归进一步发现“无条件上两层”会把 `/private/tmp` 扩成可写根，遂改为
  canonical 等值、非 canonical fail-safe 收窄，生产 canonical 授权面不变）

## 已知两义与债（登记，处置另立 wi）

1. ~~**health/cc-status 无鉴权 + live 0.0.0.0**（§2）~~ **已还，2026-08-17 /
   wi_20260810_912**——两端点复用 `checkInternalToken`，Stats、接管健康面板、双机
   watcher、reply probe 与安全重启消费者同步带 token；pairing approve / agents reload
   仍无鉴权，保留为较窄残余债，不冒充随本单关闭。
2. ~~三处配置默认路径分叉（§1）~~ **已还，2026-08-10 / wi_20260810_949**——抽
   `config-paths.ts` 单一真源，三入口全走它；防复发闸见 `config-paths.test.ts`
   的源码级断言。
3. ~~restart-safe 脚本 8787 双真源 + 头注释缺 exit 4（§4）~~ **已还，2026-08-10 /
   wi_20260810_949**——端口改「env → relay.env → 报错不猜」，头注释补齐 exit 4；
   同批次另修一条实测捉到的 fail-open（探不到状态却报「无 busy」放行重启）。
4. unit 无 MemoryMax（§3）——absent 锚点在债还清时转正。
5. ~~Codex 可写根硬编码 macOS 绝对路径（§6）~~ **已还，2026-08-10 / wi_20260810_949**
   ——改为等值推导，授权面不变。

## 机检口径

锚点由 `Scripts/hooks/global-spec-drift-guard.sh` 把守；unit 文件与 shell 脚本
均在 src-glob 内（锚点可钉 `Key=Value` 与 `VAR=value` 形态，2026-08-10 沙箱实测）。
