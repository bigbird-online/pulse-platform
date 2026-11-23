# SPEC：Relay 运行时与进程池 — 四路运行时 / provider 接入 / 生命周期 / state 真值

> version: `0.3.0`
> status: `reverse_engineered_from_code`
> authority: 会话运行时选择、进程池生命周期与 state 落盘的**现行实现契约**
> owner: Pulse（wi_20260804_070 · KR2a）
> modified: `2026-10-21`
> 版本史：只看 git log

**取证基础**：2026-08-09 代码普查 + spec-anchor 机器验证全绿。

## 1. 四路运行时与降级

- [x] 注册与选择：四路（cc / codex / cc-sdk / pi）注册进同一 manager，重复注册抛错；
  缺省 runtime 取 `data/runtime-priority.json` 首项（缺失/坏文件退 codex）；偏好列表
  白名单过滤去重。CC/cc-sdk 仅在 agent 或历史
  session 显式声明时进入路径。Pi 仅允许 structured route 显式 opt-in，不能进入自动
  fallback 链；完整 Pi 契约见 `pi-conversation-runtime.md`
  `@anchor symbol createConversationRuntimeManager`
  `@anchor symbol resolveConversationRuntimeId`
  `@anchor symbol resolveConversationRuntimePreference`
  `@anchor test Relay/tests/conversation-runtime.test.ts :: "defaults missing AgentConfig.runtime to codex"`
  `@anchor test Relay/tests/conversation-runtime.test.ts :: "preserves cc-sdk in the resolved preference instead of collapsing to cc"`
  `@anchor test Relay/tests/conversation-runtime.test.ts :: "routes runtime: cc-sdk to the cc-sdk adapter, not the legacy cc pool adapter"`
  `@anchor test Relay/tests/conversation-runtime.test.ts :: "keeps structured Pi routing opt-in and outside automatic fallback chains"`
  `@anchor test Relay/tests/gateway.test.ts :: "carries runtime: pi through the gateway route instead of collapsing to cc"`
  `@anchor test Relay/tests/telegram.test.ts :: "cold-starts a persisted native Pi WorkItem session through the gateway route"`
- [x] 两级降级：spawn 级按偏好逐个试（失败注入降级上下文）；turn 级仅在**尚无
  assistant 输出且没有已观察副作用**时降级（有输出就不换；观察到副作用则进入
  runtime-recovery-required，禁止重放）
  `@anchor symbol fallbackActiveTurn`
  `@anchor symbol nextRuntimeForTenant`
  `@anchor test Relay/tests/conversation-runtime.test.ts :: "falls back from Codex spawn failure to CC and records the final runtime"`
  `@anchor test Relay/tests/conversation-runtime.test.ts :: "falls back during a turn when the active runtime is rate-limited before any assistant response"`
  `@anchor test Relay/tests/conversation-runtime.test.ts :: "does not fallback after an assistant response has already been emitted"`
  `@anchor test Relay/tests/conversation-runtime.test.ts :: "blocks recovery instead of replaying after an observed side effect"`
- [x] Codex conversation 命令显式钉平台默认模型与推理档：`-m gpt-6-astra`
  与 `-c model_reasoning_effort="xhigh"`；catalog 中明确为 openai protocol 的
  agent 模型可覆盖模型名，anthropic protocol 模型必须拒绝进入 Codex runtime
  `@anchor symbol buildCodexCommand`
  `@anchor const DEFAULT_PULSE_CODEX_MODEL = 'gpt-6-astra'`
  `@anchor const DEFAULT_PULSE_CODEX_REASONING_EFFORT = 'xhigh'`
  `@anchor test Relay/tests/codex-conversation-runtime.test.ts :: "builds first-turn codex exec command with stdin prompt placeholder"`
- [x] Codex 权限默认由 agent 的 `codexYoloDefault` 物化；true 时命令下发
  `--dangerously-bypass-approvals-and-sandbox`，`/yolo off` 可给当前 tenant 暂时恢复
  workspace-write + approval never，重启/新会话恢复 agent 默认
  `@anchor symbol effectiveYoloMode`
  `@anchor test Relay/tests/codex-conversation-runtime.test.ts :: "uses spawn codexYoloDefault for the turn command"`
- [x] WI 声明保障由 agent 的 `sessionWiGuard` 开启（缺省关闭，未开启的 agent 行为不变）。
  CC：Relay 发出的每条消息都开启新的原生请求，发送时在消息末尾附一行提醒（`/` 开头的
  命令除外）；另以 `--settings` 注册 PreToolUse hook `runtime/cc-session-wi-guard.mjs`，
  在本请求未声明时拒绝 `git commit`、`git push`、`gh pr merge`（含 `pulse-gh.py`）、
  `launchctl bootstrap/bootout` 与 `pulse_work_item` 的 `execute`，其余调用放行。
  判定读取主 transcript：最近一条主链人类输入及其 promptId 下、成功配对的
  `declare_session_wi` 确认（与 collector 的请求边界一致）；transcript 不可读或尚无请求时放行。
  声明为 `none`（闲聊、只读查询）的请求仍按未声明 WI 拒绝上述写操作，直到同一请求再声明一个 WI；
  Pi 扩展同此。提醒行只提示本请求尚未声明，不复述规则；规则只在声明工具自身一处：CC 为
  `pulse-request-purpose` 的 MCP 服务说明（CC 每个会话都展示），Pi 与 Codex 为工具说明。Relay 的
  `--append-system-prompt` 不再附这段规则。
  CC 会话另经 `cc-session-settings.ts` 裁去 Relay 对话用不上的内容：`--exclude-dynamic-system-prompt-sections`
  把记忆说明与环境段移到首条消息、去掉快速模式一行与 CC 的会话指引；`skillOverrides` 关掉终端或 CC 自身设置才用的
  内置 skill；关掉账户级 Telegram 插件。`ensureClaudeProjectRegistered` 在工作区所在 git 仓库的项目项上批准 CLAUDE.md
  的外部 `@` 引用（CC 按仓库根读这项批准，首次见到仓库时写成 false），工作区之外的 `@USER.md` 与身份文件才会载入。
  Agent 配置 `ccPrompt`（只用于 legacy `cc` 运行时的 print 与 interactive；`cc-sdk` 不读）再开两项有代价的裁剪，缺省都维持 CC 原样：`systemPrompt: "replace"` 用
  `--system-prompt` 以 Relay 的 `CC_BASE_PROMPT` 取代 CC 默认提示（去掉游戏规则、型号清单、WebSearch 模式、代词规则、
  记忆写法说明与 git 快照，基础提示自行复述运行环境、审慎、隐私、上下文与记忆写法）；`gitStatus: false` 设
  `includeGitInstructions: false`，去掉 git 状态快照与 Bash 工具的 Git 段（含署名提醒），Relay 补回 `CC_GIT_RULES`。
  replace 只在 print 模式去掉 git 快照，interactive 模式仍带，需同开 `gitStatus: false`。改 `ccPrompt` 只对该 Agent
  下一场新会话生效（`/new`、rollover 或尚无记录的会话）：CC 恢复会话时沿用开场时记下的系统提示与首条消息里的
  git 快照，`--settings` 却每个进程即时生效。所以启用裁剪的新会话开场时把 `ccPrompt` 记到
  `<Relay dataDir>/cc-prompt-sessions/<会话 ID>.json`，恢复时用记录值而非当前配置，Bash 的 Git 段与 `CC_GIT_RULES`
  因此在同一会话边界一起变化。关掉 replace 同样要新开会话才回到默认提示。任何故障都按缺省启动、不阻断会话，
  缺省总带 CC 原有的 Git 段：未配置或全为缺省的会话不写记录，参数与没有本开关时逐字相同；记录写不成（无数据目录、
  目录被占、无写权限）就不应用裁剪并告警；恢复时没有记录视为以缺省开始的旧会话，静默按缺省；记录读不了、
  解析失败或字段非法则告警并按缺省。
  `@anchor symbol ccPromptForSession`
  `@anchor symbol CC_SESSION_SETTINGS`
  `@anchor symbol ccSystemPromptArgs`
  `@anchor symbol gitRootOf`
  Codex：按 session 声明，线程 rollout 中尚无成功完成的 `declare_session_wi` 时每轮附提醒；
  无等价拦截点，只提醒
  `@anchor symbol withSessionWiReminder`
  `@anchor symbol addSessionWiGuardSettings`
  `@anchor symbol codexSessionDeclared`
  `@anchor test Relay/tests/session-wi-guard.test.ts :: "reminds every new request and registers the write guard"`
  `@anchor test Relay/tests/cc-session-wi-guard.test.ts :: "guards only the work-item execute action and never the declaration itself"`
  `@anchor test Relay/tests/cc-session-wi-guard.test.ts :: "runs as a CC hook command"`
  `@anchor test Relay/tests/session-wi-guard.test.ts :: "reminds until the thread rollout holds a completed declaration"`
- [x] 会话文件边界：CC 系统提示的「文件访问沙箱」列出工作区、本对话投递目录、平台中转目录、
  共享仓库（只读）、规则正本（共享 SSOT，修改走自己分支的 PR）与内核 `grant_access` 的会话授权（所在 WI 结束即失效）。
  共享 SSOT 是 Coevolution 根下的 `ssot/`，由 Agent 工作区决定：从工作区上溯到第一个同时含
  `ssot/` 与 `Bigbird/PULSE/` 的祖先；工作区不在 Coevolution 树内时不列。服务家庭成员的租户 Agent
  （工作区在 `Bigbird/Oikos/tenants/<成员>/`）同样列出（大鸟 2026-11-15 定），其他租户目录由下述排除项挡住。
  共享仓库是该祖先（Coevolution）及其下 `Bigbird/PULSE`、`Bigbird/Oikos` 三个本机检出，可见范围与 SSOT 相同，
  本节点上不存在的不列；各节点走同一推导，不另设配置。提示写明工作区与可读写授权以外只能读、不得修改、
  不得在其中建 worktree，要改代码用 `pulse_work_item` 的 `allocate_workspaces` 申请稀疏工作区或在自己分支的工作区内改。
  紧随其后一行列出共享仓库内不得读取的内容：凭据与令牌文件名模式、其他 Agent 工作区的 `.relay/`、`.local/`，
  以及从仓库根推出、本节点上存在的 `.harness/`、PULSE `data/agent-harness/scl-host/`、Oikos 私有数据存档与
  `tenants/` 下非本租户目录（本租户只按工作区判断）；本会话工作区与授权路径不在此限。一条排除项只在被工作区
  或某条授权整个覆盖时才不列，局部授权不撤销祖先排除项，只作精确例外。这是提示层约束，CC 的读取没有技术拦截。
  共享仓库与 SSOT 只进提示，不进 `--add-dir`，不进 CC 写入硬边界的写根，也不改 Codex 写根。
  Pi 系统提示、Codex 新线程的首条提示与定时任务的提示开头列出同样三行。
  执行面：CC 以 `--dangerously-skip-permissions` 运行，未开 `ccWriteBoundary` 的 Agent 无 Relay 文件沙箱，边界只由提示约束；
  Codex `workspace-write` 可读全盘，写域为工作区与 `resolveCoevolutionRoot()`，YOLO 时不设沙箱；
  Pi host-native 的 `workspaceRoots` 只做工作区准入，不拦读
  `@anchor symbol resolveSharedSsotRoot`
  `@anchor symbol resolveSharedRepoRoots`
  `@anchor symbol sharedAccessPromptLines`
  `@anchor symbol sharedReadExclusions`
  `@anchor test Relay/tests/config-paths.test.ts :: "release 目录运行时从 Agent 工作区上溯到 Coevolution 根，Codex 写根不变"`
  `@anchor test Relay/tests/config-paths.test.ts :: "服务其他家庭成员的租户工作区不列 SSOT，大鸟自己的租户照常"`
  `@anchor test Relay/tests/config-paths.test.ts :: "共享仓库：Coevolution、PULSE、Oikos 中本节点存在的检出，只进读域"`
  `@anchor test Relay/tests/config-paths.test.ts :: "读取排除：凭据、.harness、scl-host、私有数据与非本租户目录，本工作区与授权路径不受排除"`
  `@anchor test Relay/tests/config-paths.test.ts :: "局部 grant 不撤销祖先排除项：跨租户局部授权时同级私有目录仍被排除，整项覆盖才不列"`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "lists the shared repositories and SSOT read-only for every session without opening them for writing"`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "lists only the shared repositories present on the node"`
  `@anchor test Relay/tests/pi-conversation-runtime.test.ts :: "names the shared repositories and SSOT read-only when the workspace sits in a Coevolution root"`
  `@anchor test Relay/tests/codex-conversation-runtime.test.ts :: "names the shared repositories read-only in a new thread without widening its write roots"`
  `@anchor test Relay/tests/scheduler.test.ts :: "names the shared repositories read-only in the scheduled prompt"`
- [x] 常驻只读授权：内核 `grant_access` 带 `standing: true` 登记的授权属于 Agent（`harnessAgentId`，缺省为
  Agent id），不属于某个 WI 或会话；家族成员（本体 `<base>` 与分身 `<base>-<member>`）互读对方工作区时内核按家族
  规则直接登记。Relay 启动某 Agent 的任何会话时都套用它，与共享 SSOT 一样：CC 会话进「文件访问沙箱」提示
  （「常驻授权」一行）并加 `--add-dir`，Pi 会话进系统提示，Codex 新线程的首条提示列出它，定时任务的提示
  开头列出它（键为任务 `owner`）。来源是内核 `relay-standing-access`：一次读出全部 Agent 的常驻授权，要回放
  全部 WI 日志，所以 Relay 启动时在后台先读一次，之后保留读数，超过 10 分钟的读数在后台刷新；只有启动后第一个
  会话等待首次读数。读不到时不套用常驻授权，也不阻止会话或任务启动；过期读数在过期后的一次刷新失败后即停用，
  超过 20 分钟无论如何停用，撤销不会因内核读不到而一直有效。CC 会话每次人类回合前的 `relay-access` 读回同时
  带回本 Agent 的常驻授权，变化时照旧以同一会话续开；保留的读数只用这份独立的常驻授权读回更新，不从合并后的
  会话视图推断。
  `@anchor symbol standingAccessFor`
  `@anchor symbol readStandingAccess`
  `@anchor test Relay/tests/standing-access.test.ts :: "waits for the first read, then serves it and refreshes in the background once stale"`
  `@anchor test Relay/tests/standing-access.test.ts :: "stops applying an expired read once a refresh after its expiry fails"`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "keeps the standing grant when a session grant opens the same path"`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "opens the Agent standing read grants at spawn and keeps them when the readback agrees"`
  `@anchor test Relay/tests/scheduler.test.ts :: "names the owner Agent standing read grants in the scheduled prompt"`
- [x] CC 写入硬边界：Agent 配置 `ccWriteBoundary: true`（缺省关闭）时，其 CC 会话与以它为 `owner` 的 CC 定时任务只能
  写入写根：工作区（含 `.relay/` 中转）、本对话投递目录、`mode: write` 的会话授权与内核分配的工作区（见下一项）；
  定时任务的写根只有工作区。
  两层都并入同一个 `--settings`：PreToolUse 钩子 `runtime/cc-write-boundary.mjs` 按写入实际到达的真实路径判断
  Edit、Write、MultiEdit、NotebookEdit（跟随符号链接，悬空链接也跟到其目标；解析不了的路径拒绝），越界或读不懂的
  请求拒绝；钩子以 exec 形式（`command` 加 `args`）运行并设 `onFailure: "block"`，起不来、崩溃或超时（10 秒）都按拒绝处理，
  生成的设置同时固定 `disableAllHooks: false`。CC 沙箱 `sandbox.enabled`、`allowUnsandboxedCommands: false`、
  `failIfUnavailable: true`、`filesystem.allowWrite` 为同一组写根，约束 Bash 及其子进程；`--settings` 里原有的
  `sandbox` 整体替换，不合并。生成的设置还在 `env` 里给沙箱内的 AI 命令行补两项（CC 把设置的 `env` 加进自身进程环境，
  Bash 及其子进程随之继承；与 `--settings` 原有的 `env` 逐项合并，同名以边界的值为准）：`NODE_USE_ENV_PROXY=1`，
  Node（如 Pi）只在它为 1 时走沙箱设的 `HTTPS_PROXY`，CC 自身不设；仅在 macOS 设 `CODEX_CA_CERTIFICATE=/etc/ssl/cert.pem`，
  沙箱内 Codex 读不到系统钥匙串里的根证书，否则每回合报 workspace routing discovery failed，Linux 上 Codex 自读发行版的
  `/etc/ssl/certs` 证书包、该路径也不存在，故不设。未开边界的会话与定时任务不带这两项。
  沙箱内运行的 Pi 不用账户共享的 Pi 状态目录（会话 `HOME` 下 `.pi/agent/`），改用 Relay 为这一次会话准备的独立目录（`ccPiState`）。
  Pi 0.87.1 读凭据、设置、模型目录与信任记录前，都经 proper-lockfile 在 Pi 状态目录里建锁目录（`auth.json.lock` 等），
  用完再删。若会话能写共享锁目录，就能在里面放一个文件，Pi 删锁时报 ENOTEMPTY，本机所有 Pi 从此拿不到这把锁。
  所以 CC 设置的 `env` 给 Bash 设 `PI_CODING_AGENT_DIR`、`PI_CODING_AGENT_SESSION_DIR`（`ccPiEnv`），沙箱只给 Bash
  （不给文件工具，钩子的写根不变）放行两处（`ccPiWrites`）：
  一是独立目录 `<Relay dataDir>/cc-pi-agent/<目录名>/<会话 id>`（`ccPiStateRoot`，会话 id 每次启动随机生成），二是共享目录里
  本工作区的会话目录 `sessions/<目录名>`。目录名按 Pi 的规则从工作区真实路径得出（`piSessionDirName`：去掉开头的 `/`，
  `/`、`\`、`:` 换成 `-`，前后加 `--`）。每次 CC 启动前（渠道会话与定时任务各自一次），Relay 在沙箱外租用一个独立目录
  （`leasePiAgentDir`）：先在旁边写属主记录 `<会话 id>.owner`（内容是进程号，先记 Relay 自己，CC 启动后改记 CC 进程），
  再建目录，为共享目录里除 `sessions`、`tmp` 和 `*.lock` 以外的每一项建符号链接，Pi 因此照常读账户的凭据、设置、模型目录、
  扩展与包。Pi 加锁时不解析链接（`realpath: false`），锁目录落在这次会话的独立目录里。同一工作区的渠道会话与定时任务并发时，
  各用各的目录和锁，后启动的一方不碰前一方的目录。经链接写入会落到共享文件，沙箱仍然拒绝，所以沙箱内的 Pi 不能刷新过期的
  OAuth 凭据、记下信任决定或更新模型目录。属主记录在沙箱可写范围以外，会话改不了。CC 进程退出（或被 Relay 结束）时，Relay
  删掉这次会话的目录和属主记录（`PiAgentLease.release`）；进程没能启动时立即删。每次租用前，Relay 清掉同一工作区里已无主的
  旧目录（`sweepPiAgentDirs`）：没有属主记录，或记录的进程已不在（向该进程号发信号 0 得到 ESRCH；属于别的账户、得到 EPERM
  的仍算在）。删除时不跟随链接，会话留下的任何东西，无论是锁目录里的文件、多出的文件，还是被换成链接的目录本身，都随目录删掉；
  在此之前只影响这次会话沙箱内的 Pi。进程号被复用时，旧目录要等那个进程也退出才清，只多占一点磁盘，不会误删在用的目录。
  会话可在自己的目录里建 `rm -rf` 删不掉的东西（如 `chmod 000` 的子目录里放文件），所以删除逐项进行、从不抛错，结束会话与
  清扫旧目录同一做法：先删；删不掉就把其下各级目录恢复为属主可读写可穿过（0700，不跟随链接）再删；仍删不掉就把目录改名移入
  隔离区 `<Relay dataDir>/cc-pi-agent/.quarantine/`（0700，任何会话都不可写），记一条 warn 日志写明去向；连移动也失败时原样
  留下目录和属主记录、记 warn 日志，下次清扫再试。每次清扫也重试删除隔离区里的内容。清扫本身的任何失败只记日志，不阻止这次
  租用，一个会话留下的残留因此不会让同一工作区后续的 CC 起不来。
  `linuxUser` 会话经 `sudo -u` 以另一账户运行，Relay 用 `id -u`、`id -g` 查出该账户，把这次会话的目录 chown 给它，并把
  Relay 自己拥有的上两级目录（`cc-pi-agent` 与工作区那一级）改为 0711（只许穿过，不许列出）；查不到账户、Relay 无权 chown
  （Relay 不是 root 时就是这样），或目录以上某一级该账户无法穿过时，Relay 删掉刚建的目录，Bash 不得到任何 Pi 设置，并记一条
  warn 日志写明原因，会话照常启动。会话日志仍写入共享目录里本工作区的会话目录，与沙箱外从工作区启动 Pi 时的位置相同，因此
  ComputeCost 按账户 `.pi/agent/sessions` 登记的 Pi 来源不需改动；从工作区子目录启动的 Pi 也写入这个目录。没有 `HOME`，
  或任一路径含 `*?[]`（CC 沙箱会把它读成通配）时，Bash 不得到任何 Pi 设置。
  2026-11-11 以 `@anthropic-ai/sandbox-runtime` 0.0.82 按生成的设置实测（当时独立目录每个工作区一个）：沙箱内 `pi -p` 建出会话
  并答复；在共享目录建锁目录报 EPERM；在独立目录的锁目录里放文件后，共享目录里没有锁目录。改为每次会话一个目录后，同日以同一
  版本跑沙箱回归测试：上述两项不变，沙箱内改写属主记录被拒，会话结束后独立目录被删；`pi -p` 未重测。
  未在 CC 本体内复测。对应的沙箱回归测试需设 `SRT_CLI` 指向 sandbox-runtime 的 `dist/cli.js`；Relay 不安装该包，未设时跳过。
  写根内改变执行策略的路径对两层都禁写：任意层级的 `.claude/`（设置、钩子、技能、子 Agent）
  与 `.mcp.json`、`.git/`（钩子与配置会执行命令；沙箱禁 `.git/hooks`、`.git/config`），以及承载钩子的 Relay 安装目录；
  钩子同时按请求的字面路径与实际到达的真实路径判断，并把每个写根及路径上各级目录中的这些入口连同其链接目标一起保护
  （如 `.claude -> config` 时经两个名字写入都拒绝）；沙箱以 `filesystem.denyWrite` 列出每个写根下的这些入口、它们的真实目标
  与 Relay 安装目录。会话因此不能给自己注入钩子或关掉边界。
  CC 跨来源合并数组设置，所以准入时读取用户、项目（工作区及其仓库根）、本地与托管设置：任一含
  `sandbox.filesystem.allowWrite`、`sandbox.filesystem.disabled`、`sandbox.excludedCommands`、
  `allowUnsandboxedCommands: true`、`sandbox.enabled: false`、带路径的文件工具 allow 规则、
  `permissions.additionalDirectories`、`disableAllHooks: true` 或 `allowManagedHooksOnly: true`，或托管设置存在但读不懂时，
  拒绝启动该会话或定时任务，使钩子一定加载、且与 Bash 用完全相同的写根。交互式定时任务以 `--bare` 运行、会跳过钩子，
  开启边界时直接拒绝；被拒的定时任务不进冷却、不改用其他运行时。读不受限：SSOT 与只读授权只进提示，开启时只读授权
  （含常驻授权）不进 `--add-dir`（工作目录会成为沙箱写根）。
  剩余缺口：沙箱自带的临时目录可写；CC 沙箱的网络走代理，CC 2.1.311 以 `--dangerously-skip-permissions` 运行时，
  未列入 `sandbox.network.allowedDomains` 的主机也能到达（2026-11-09 实测 example.com），出网不受限；
  MCP 工具（含内核 `pulse_work_item`）不在沙箱内；CC 自身的会话记录不经工具，不受限，模型用 Write 写自动记忆目录会被拒；
  Agent 也不能再用文件工具维护工作区里自己的 `.claude/` 内容；沙箱 `denyWrite` 只列写根顶层的入口及其目标，子目录里的
  `.claude/` 等（CC 不从子目录加载设置）对 Bash 仍可写；钩子判断与实际写入之间有时间差；沙箱 `denyWrite` 与
  钩子拒绝已在登录的 CC 2.1.311 上实测（见下一项的冒烟），`onFailure` 的效果依据其设置说明，尚未实测。
  `@anchor symbol ccWriteBoundarySettings`
  `@anchor symbol ccSandboxEnv`
  `@anchor symbol ccPiState`
  `@anchor symbol leasePiAgentDir`
  `@anchor symbol sweepPiAgentDirs`
  `@anchor symbol ccPiWrites`
  `@anchor symbol ccPiEnv`
  `@anchor symbol piSessionDirName`
  `@anchor symbol ccWriteRoots`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "enforces the write boundary for an Agent with ccWriteBoundary and keeps read grants read-only"`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "keeps standing read grants out of the write roots under the boundary"`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "leaves Agents without ccWriteBoundary unchanged"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "rejects a file-tool write outside the write roots and allows one inside"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "keeps reads and other tools untouched"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "follows a dangling link to its missing target outside the root and denies it"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "denies writes that would change the session execution policy, even inside a root"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "denies Bash the same policy paths inside every root"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "protects a policy entry point that is a link to an ordinary in-root target, through both names"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "runs the hook fail-closed: exec form, onFailure block, hooks pinned on"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "refuses settings files that would widen writes beyond the session roots"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "gives sandboxed Bash the AI CLI environment, with the CA bundle only on macOS"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "merges into the single --settings value beside the WI guard and subscription routing"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "names the session directory the way Pi does"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "keeps Pi's agent directory in Relay's state and opens only it and the workspace's session directory"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "links the account's Pi configuration and sweeps only the directories whose session no longer runs"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "keeps a running session's Pi lock when another session of the workspace starts"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "deletes or quarantines a directory its session made undeletable, and never refuses the next session for it"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "hands the directory to the account a linuxUser session runs as, or gives Pi nothing"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "gives Pi its state through Bash only, not through the file-tool hook"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "leaves the account's Pi locks to every other Pi when a sandboxed session poisons its own"`
  `@anchor test Relay/tests/runtime.test.ts :: "applies the declared CC subscription credential to the CC batch process"`
  `@anchor test Relay/tests/runtime.test.ts :: "refuses a bounded run it cannot bound, without cooldown or fallback"`
- [x] 可提交的仓库与出网域名：开启 `ccWriteBoundary` 的 Agent 可在 relay.json 的 agent 项配置 `commitRepos`
  （仓库根的绝对真实路径）与 `allowedDomains`（主机名，可带 `*.` 前缀）；缺省或空数组不开放任何东西，未开边界时两项不起作用。
  它们是 Agent 的常设配置，不随 WI 失效，也不经 `grant_access`。启动与每次 `/internal/agents/reload` 都校验格式：
  非数组、非字符串、相对路径、含 `..`、未规范化（`//`、结尾 `/`、`/` 本身）的仓库，以及带协议、端口、路径、大写、
  单独 `*`、IP 或无点的域名都拒绝，重载失败时保留运行中的配置。会话或定时任务启动时在主机上核验每个仓库：路径须是
  自身的真实路径（不经链接），且其下 `.git` 是 Git 目录（主检出，有 `HEAD`、`objects/`，没有 `commondir`），或是指向
  `<公共目录>/worktrees/<名>` 的链接文件、该管理目录的 `commondir` 指回公共目录且 `gitdir` 指回这个 `.git`（链接工作树）；
  不是仓库根（如仓库内的子目录）、不存在、经链接或核验不过时拒绝启动，不降级开放。
  只有 Bash 沙箱得到这些路径，且只按白名单开放具名的 Git 数据，从不开放整个 Git 目录、管理目录或 `worktrees/`，所以
  会话启动后别的会话新建的工作树天然拒写（整目录开放时，新工作树的 `HEAD`、`index`、`gitdir`、`config.worktree` 可被改写）。
  两种情况都开放公共目录中提交、建分支、拉取与推送写入的共享数据（`COMMON_GIT_WRITES`）：`objects/`（含 pack）、`refs/`、
  `logs/`、`packed-refs`、`packed-refs.lock` 与 Git 改名覆盖它的临时文件 `packed-refs.new`；以及本工作树自己的文件
  （`WORKTREE_GIT_WRITES`，主检出在其 Git 目录、链接工作树在其管理目录）：`index`、`HEAD`、`ORIG_HEAD`、`FETCH_HEAD`、
  `MERGE_HEAD`、`AUTO_MERGE`、`CHERRY_PICK_HEAD`、`REVERT_HEAD`、`REBASE_HEAD` 各连同其 `.lock`（Git 经锁文件更新或删除
  它们，提交时也会锁 `AUTO_MERGE`），`COMMIT_EDITMSG`、`MERGE_MSG` 及其 `.lock`（单提交 cherry-pick 经锁写它）、
  `MERGE_MODE`。逐个核实过：这些引用文件只存对象名，
  `COMMIT_EDITMSG` 与 `MERGE_MSG` 只作提交说明，`MERGE_MODE` 只存 `no-ff` 选项，`index` 只存暂存内容，`objects/`、`refs/`、
  `logs/`、`packed-refs` 只存对象、引用与引用日志，Git 都不会把它们当作要执行的指令。链接工作树另开放自己管理目录下的
  `logs/`（其 HEAD 的引用日志）。名单外的写入（`gc --auto` 的状态、`info/`、其他工作树）被拒，自动 gc 被拒不影响提交。
  即使会话写根包含它们，以下也禁写：`hooks/`、`config`、`config.worktree`（会执行命令或改 `core.hooksPath`）；本工作树的
  `rebase-merge/`、`rebase-apply/`、`sequencer/`（`EXECUTABLE_GIT_STATE`：交互变基与多提交 cherry-pick、revert 的待办
  可含 `exec <命令>`，选项可指定合并策略程序，人类或沙箱外进程之后执行 `--continue` 就会以其权限运行）；`commondir`、
  管理目录的 `gitdir`、工作树的 `.git` 链接与 `objects/info/alternates`（把 Git 重定向到别处的数据）；公共目录与本工作树的
  `info/`（属性、排除与稀疏规则）；`modules/`（子模块的 Git 目录各带钩子与配置）。代价：沙箱内不能 `rebase`，也不能
  cherry-pick 或 revert 多提交序列（单个提交的 cherry-pick 实测可用）；删除分支时 Git 顺带删其在 `config` 中的节会被拒
  并告警，分支本身照常删除；`git sparse-checkout` 要写 `info/`，同样被拒。这些禁写项按字面路径与真实路径各列一次，与
  `ccPolicyPaths` 一起进 `filesystem.denyWrite`。
  准入 fail-closed（`verifyGitData`）：Seatbelt 按写入实际到达的真实路径判断，禁写项若是指向开放区的符号链接
  （如 `config -> objects/policy`）或与开放区文件共享 inode（硬链接），经开放区就能改写它；开放项若是链接，放行会跟到
  链接目标。所以每次会话启动、续开与定时任务启动都先扫描公共 Git 目录（含链接工作树的管理目录）：任何位置出现符号链接、
  `objects/` 以外出现链接数大于 1 的文件、或任一开放项与禁写项不在自身真实路径上（含 `alternates` 的链接数），都拒绝
  启动并给出路径。Git 自己不在其目录中建符号链接；`objects/` 内 Git 收尾时会先把临时对象或 pack 硬链接到正式名再删临时名，
  故只在那里不数链接数；扫描期间被别的 Git 进程删掉的条目跳过。生产中 Coevolution 的 Git 目录约 1.8 万项，扫描约 0.65 秒。
  会话内：Seatbelt 拒绝把禁写文件硬链接或移动进开放区，也拒绝经符号链接写到禁写区或写根之外（实测）。
  剩余缺口：会话可在开放的 `logs/`、`refs/`、`objects/` 与 `COMMIT_EDITMSG`、`FETCH_HEAD`、`MERGE_MODE` 等位置新建指向
  别处的符号链接；沙箱内写不穿它，但沙箱外的 Git（人类或无边界的 Agent）随后在同一仓库提交、切换或拉取时会跟随它，
  覆写目标（`COMMIT_EDITMSG`）或向目标追加含分支名的引用日志行（`logs/HEAD`）。CC 沙箱不能禁止建符号链接，此缺口在本项内
  无法关闭；下一次会话启动会因扫描到链接而拒绝。内核分配的工作区开放管理目录与 reflog，同样存在此缺口。
  钩子的写根仍是会话写根，
  Write、Edit 等文件工具照旧不能写 `.git`。`allowedDomains` 写入 `sandbox.network.allowedDomains`。
  准入只检查设置文件：Relay 自己经 `--settings` 生成的这些放行合法，用户、项目、本地或托管设置里的
  `sandbox.filesystem.allowWrite` 仍拒绝启动。会话提示「文件访问沙箱」列出可提交的仓库与放行域名。
  CC 会话从 Agent 配置取值，定时任务按 `owner` 取同一 Harness 主体下开启边界的 Agent 的配置，别名之间不一致时不开放。
  2026-11-09 用登录的 CC 2.1.311 冒烟：主检出与链接工作树中的 `git commit`、建分支、切换与 `reset` 成功，写
  `.git/hooks`、`git config`、写管理目录 `commondir`、写两种布局的 `rebase-merge/git-rebase-todo` 与 `sequencer/todo`、
  写会话启动后新建工作树的 `HEAD` 都被沙箱拒绝，Write 写 `.git` 被钩子拒绝，经 `Scripts/github-git.sh` 的 `ls-remote` 出网成功。
  `@anchor symbol ccCommitRepoPaths`
  `@anchor symbol ccRepoAccessProblems`
  `@anchor symbol ccBashAccess`
  `@anchor symbol validateAgentRepoAccess`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "opens only named Git data of a main checkout to Bash, with hooks, config, rebase state and redirects closed"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "opens only named files of a linked worktree administrative directory and the shared object, ref and log stores"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "lets everyday Git run in a main checkout under that sandbox, and not hooks, config or rebase state"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "lets everyday Git run in a linked worktree under that sandbox, and not hooks, config, rebase state or another worktree"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "keeps a worktree added after the session started closed, with the main checkout and a linked worktree configured"`
  `@anchor symbol COMMON_GIT_WRITES`
  `@anchor symbol WORKTREE_GIT_WRITES`
  `@anchor symbol EXECUTABLE_GIT_STATE`
  `@anchor symbol verifyGitData`
  `@anchor symbol CLOSED_COMMON_GIT`
  `@anchor symbol CLOSED_WORKTREE_GIT`
  `@anchor symbol CLOSED_ADMINISTRATIVE_GIT`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "refuses a repository whose config is a link into opened Git data, so Bash cannot rewrite it through objects"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "refuses links and shared inodes anywhere in the Git data, in both layouts, before opening anything"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "denies every closed entry by name and real path, and lets no session turn one into a link or a hard link"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "refuses entries that are relative, carry .., are not normalized, or are not a repository root"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "refuses malformed domains"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "leaves an Agent without commit repositories or domains unchanged"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "accepts its own --settings grant and still refuses a user-level one"`
  `@anchor test Relay/tests/cc-commit-repos.test.ts :: "validates the fields at start and on reload, keeping the running configuration on a bad reload"`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "opens the Agent commit repository Git data and domains to Bash and names them in the prompt"`
  `@anchor test Relay/tests/conversation-launch.test.ts :: "carries the Agent commit repositories and domains, and nothing when it configures none"`
  `@anchor test Relay/tests/scheduled-cc-host-mcp.test.ts :: "keeps the write boundary in the single --settings value beside the servers"`
- [x] 内核分配的工作区：会话持有的 WI（含其子 WI）经 `allocate_workspaces` 分配、已就绪且未退役的工作区，由内核在
  `relay-access` 读回的 `workspaces` 给出（`harness_rewrite/workspaces.py` 的 `session_write`）。CC 会话每次人类回合前读回，
  与会话授权一起比较，变化时以同一会话续开：工作区路径及提交所需的 Git 路径（`write`：内核在主机上核验归属的工作树管理目录、`objects`，
  以及本工作区分支自己的 ref、ref 锁与 reflog 三个精确路径，不开放 ref 目录）并入写根。尚不存在的锁文件靠 macOS
  Seatbelt 按路径放行；Linux 上的 CC 沙箱未验证。内核列出的 `deny` 与
  `ccPolicyPaths` 一起进沙箱 `filesystem.denyWrite`，并作为钩子的第二个参数拒绝文件工具写入；工作区加 `--add-dir`；
  「文件访问沙箱」提示每个工作区列一行「内核分配的工作区（可读写）」。分配在当前回合内不生效，从下一个人类回合起生效；
  WI 结束、请求退役或被替换后，下一个人类回合续开时移除。新会话读回失败时不开放任何分配的工作区；运行中的会话读回
  失败时保持当前边界，不新增。内核读回格式不对时整次读回按失败处理。定时任务不读回会话授权，不开放；Codex 与 Pi
  没有按会话的读回，不开放（Codex 写域仍为工作区与 `resolveCoevolutionRoot()`）。
  `@anchor symbol allocatedWorkspaces`
  `@anchor symbol ccWriteDenials`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "opens the workspaces the kernel allocated to the held WIs and closes them when the readback drops them"`
  `@anchor test Relay/tests/pool-session-access.test.ts :: "opens no allocated workspace when a new session cannot read its access back"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "denies writes under the paths the kernel keeps closed in an allocated workspace, as a command too"`
  `@anchor test Relay/tests/cc-write-boundary.test.ts :: "opens an allocated workspace and its commit paths, and denies the kernel denials with the policy paths"`
  `@anchor test Relay/tests/native-work-item-command.test.ts :: "fails the whole readback on a malformed row instead of opening part of it"`

## 2. 运行时诊断与 provider 接入

### 运行时失败通知

TG 和飞书必须透传失败结果与运行时异常的脱敏原文。识别到的错误类别用于补充解释；
未识别的句式仍显示原文，不替换为“请查日志”。结构化错误只提取 message、code、type、
HTTP status 和 cause，不复制整个对象或请求内容。保留失败 subtype、多个不同原因和
原文中的额度重置时间；运行时没有提供错误文本时明确显示该事实。

共享脱敏规则先移除终端颜色与控制字符，避免影响凭据匹配；再在截断之前移除环境中的
凭据值、认证头、凭据赋值、常见 token、URL 的认证信息与私有参数、私有文件路径。
URL 脱敏覆盖 WebSocket 等非 HTTP 协议；错误类别只从脱敏后的诊断识别，避免把 URL
私有参数里的数字误报为 HTTP 状态。结构化错误码与 HTTP 状态置于长消息之前，避免被截断。
单条诊断限 900 字符，合并原因限 2000
字符。结果后重复出现的异常仍只通知一次；日志保存同一份脱敏诊断。

Claude 官方订阅返回 `You've hit your session limit` 或 `You've hit your weekly limit`
时，同时解释为“Claude 订阅用量已达上限，等待额度重置后重试”，并保留原文的重置时间。
`@anchor test Relay/tests/public-runtime-failure.test.ts :: "reports Claude subscription limits without exposing private diagnostics"`
`@anchor test Relay/tests/public-runtime-failure.test.ts :: "passes unfamiliar native failure messages and the original quota reset time"`
`@anchor test Relay/tests/public-runtime-failure.test.ts :: "preserves structured error messages, codes and nested causes"`
`@anchor test Relay/tests/public-runtime-failure.test.ts :: "redacts credentials before bounding unknown diagnostics"`
`@anchor test Relay/tests/public-runtime-failure.test.ts :: "redacts authentication and private data from non-HTTP URLs without inventing HTTP failures"`
`@anchor test Relay/tests/telegram.test.ts :: "delivers a safe native runtime failure to TG once after a failed result"`
`@anchor test Relay/tests/feishu-channel.test.ts :: "delivers one safe native failure notice to Feishu and suppresses the generic exit toast"`

### Provider 接入

- [x] 池内限流：429 信号触发退避重试，达到 `RATE_LIMIT_MAX_RETRY` 后结束会话；
  模型目录内按 `priority` 切换 provider 的规则见 `relay-config-http.md` §2 L4
  `@anchor symbol isRateLimitSignal`
  `@anchor test Relay/tests/pool-rate-limit-loop-fix.test.ts :: "C: rate-limit backoff stops after RATE_LIMIT_MAX_RETRY and emits session-end"`
- [x] 继承环境毒化守卫：cc-sdk 引擎与批任务（CC / Codex）子进程一律删除继承的
  `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY`、
  `ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL`、`ANTHROPIC_CUSTOM_MODEL_OPTION`
  与 `GLM_`/`KIMI_`/`MINIMAX_`/`DEEPSEEK_`/`XFYUN_` 前缀变量（裸 BASE_URL 会毒化
  订阅路）；只有调用方显式给出的路由（批任务 `BatchRunInput.env`、cc-sdk
  `engineRoute`）才生效
  `@anchor symbol stripInheritedProviderRoutingEnv`
  `@anchor symbol buildSdkEngineEnv`
  `@anchor test Relay/tests/runtime.test.ts :: "does not pass inherited provider routing env to the CC batch process"`
- [x] cc-sdk 认证与端点纯配置：`subscription`（缺省，不注入端点与 token）/
  `apikey`（预留，行为同 subscription）
  `@anchor symbol resolveProviderEnv`
- [x] cc 子分支 print/interactive 判据：环境强制 > 调用点 > 模型目录 provider 的
  `force_interactive` > print
  `@anchor symbol resolveCCMode`
  `@anchor symbol needsInteractiveMode`
  `@anchor symbol buildPrintArgs`
  `@anchor symbol buildInteractiveArgs`

### CC 订阅凭证唯一来源

实例设置 `RELAY_CC_OAUTH_ENV_FILE` 为凭证源文件的绝对路径时，官方 CC 订阅路由在每次
spawn 时只读取该文件中的 `CLAUDE_CODE_OAUTH_TOKEN`，通过子进程 env 传递。该配置优先于
Relay 启动时继承的 token 和 PoolOptions token；源文件缺失、空值或非法值必须拒绝启动，
不能退回旧 settings、交互登录或付费 API。未声明该配置的实例保留原有环境变量接入方式。

文件值为单行字面量 setup token，支持裸值、单/双引号、export 和行尾注释，不展开变量。
token 仅留在原始凭证源与匹配的 CC 子进程环境中；不加载文件中的其它密钥，不写入
bot settings、LaunchAgent、临时凭证副本、argv、日志或证据。API provider 路由不读取该
订阅 token。官方路由以不含 token 的 settings override 清除共享网关、API key/helper
和默认模型映射的污染；保留原 HOME、用户其它设置、session ID、历史和渠道绑定。

旧的 managed bot settings token 副本通过 `Scripts/relay-cc-auth-source.py` 显式迁移：
先计划并检查全部文件原件，再删除旧 OAuth 键、将源文件路径写入 LaunchAgent；
不生成含 token 的备份。运行时发现 user/project settings 中仍有 OAuth 副本时拒绝启动，
避免悄悄使用过期值。ownermac 源为 `/Users/owner/Coevolution/.env`。

更新源文件后，已有 CC 进程保持其启动时凭证；在原渠道执行 `/restart` 重建 CC 进程，
或完成受管 Relay 重启，即采用新值并 resume 原 session。无需逐个 bot 修改凭证。
部署先通过隔离 CC canary，再在空闲维护窗口迁移和激活；保留此前运行包供回滚，
回滚不得重新创建 token 副本。服务健康、真实渠道回复与 Stats 有效配额消费分别验收。

`@anchor test Relay/tests/pool.test.ts :: "reloads the declared token on a same-session CC process rebuild and keeps it out of argv"`
`@anchor test Relay/tests/pool.test.ts :: "refuses to launch through a stale settings token copy"`

### 工位的 Claude 认证选择

工位可在受管 `relay.env` 旁的私有 `relay-auth` 文件写一行 `claude_auth=api`。
Console key 另存同目录的 `anthropic-api.key`（权限 0600），Claude 通过 `apiKeyHelper`
读取。切回订阅时写 `claude_auth=subscription`，缺少选择文件时也使用订阅。
下次获取空闲的原生 CC 会话时采用当前选择并恢复同一会话。已经运行的回合继续使用
本回合的凭据。定时任务沿各自的认证配置。

`@anchor test Relay/tests/conversation-acquisition.test.ts :: "switches authentication once after the running turn while retaining the same session"`

## 3. 进程池生命周期

- [x] 每 tenant 至多一个进程（**无全局并发上限**——登记 §6）；重复 spawn 先杀旧
  （孤儿 CC 的最后防线）；auto 模型每次 spawn 重解析让冷却过期者重新入选
  `@anchor symbol spawnProcess`
  `@anchor test Relay/tests/pool.test.ts :: "preempts (kills) the existing process when spawning duplicate tenantId"`
  `@anchor test Relay/tests/pool.test.ts :: "follows starting → ready → busy → idle cycle"`
- [x] 空闲驱逐：idle 超时且非待回答交互才驱逐，先发 idle-reclaimed 再杀；
  jsonl 仍在滚动视为活跃不驱逐
  `@anchor symbol cleanupIdleProcesses`
  `@anchor const DEFAULT_IDLE_TIMEOUT_MS = 30 * 60 * 1000`
  `@anchor test Relay/tests/pool.test.ts :: "emits session-end with idle-reclaimed on idle cleanup"`
  `@anchor test Relay/tests/pool.test.ts :: "does NOT reclaim idle process while JSONL records keep arriving"`
  `@anchor test Relay/tests/pool.test.ts :: "reclaims idle process when JSONL goes silent past idleTimeoutMs (control)"`
- [x] cc-sdk 惰性驻留独立驱逐：只杀引擎保 sessionId，唤醒即 resume
  `@anchor symbol armIdleTimer`
  `@anchor symbol evictSession`
  `@anchor const DEFAULT_IDLE_RESIDENCE_MS = 15 * 60 * 1000`
  `@anchor test Relay/tests/cc-sdk-conversation-runtime.test.ts :: "a new message after eviction RESUMES the same session id (wake-on-demand)"`
  `@anchor test Relay/tests/cc-sdk-conversation-runtime.test.ts :: "two concurrent cold sends start ONE engine (no duplicate) and BOTH turns reply"`
- [x] 杀进程：SIGTERM → 宽限 → SIGKILL；重启限频防雪崩
  `@anchor symbol killProcess`
  `@anchor const DEFAULT_KILL_TIMEOUT_MS = 5000`
  `@anchor const DEFAULT_MAX_RESTARTS_PER_HOUR = 3`
  `@anchor const RATE_LIMIT_MAX_RETRY = 8`
  `@anchor test Relay/tests/pool-rate-limit-loop-fix.test.ts :: "C: rate-limit backoff stops after RATE_LIMIT_MAX_RETRY and emits session-end"`
  `@anchor test Relay/tests/pool-rate-limit-orphan-spawn.test.ts :: "B: a superseded backoff does not spawn when its timer eventually fires"`

## 4. SIGTERM 收口（busy 不丢尾的六步契约）

标记重启 → **drain 忙会话（30 秒）** → 通知关机（3 秒竞速）→ state 与出站缓存
flush → 停服（此刻 SIGTERM 才到 CC 子进程）。busy 会话收到关停时写「未完 turn」
哨兵（复用 summary 类型防解析器炸）并拒绝挂起队列防未处理拒绝崩主进程。

`@anchor symbol drainBusySessions`
`@anchor symbol emitShutdown`
`@anchor symbol writeUnfinishedTurnMarker`
`@anchor const SHUTDOWN_DRAIN_TIMEOUT_MS = 30_000`
`@anchor test Relay/tests/pool.test.ts :: "emits session-end relay-shutdown and rejects pending when SIGTERM interrupts busy CC"`
`@anchor test Relay/tests/pool.test.ts :: "does NOT emit relay-shutdown session-end when idle CC exits with SIGTERM (wasState=idle)"`

## 5. state.json 真值链（0725 记忆错乱事故的机器化）

- [x] 持久化形态与节奏：2 秒去抖、tmp+rename 原子写、flush 同步落盘而 dispose
  只清表不写盘、损坏文件静默回空
  `@anchor symbol createStateManager`
  `@anchor const DEBOUNCE_MS = 2000`
- [x] **落盘 session 的取值优先级 = 活进程内存 > state 旧值 > 配置值**，只要
  进程活着任何写入都会被真值覆盖回去——「改 state 必须先 stop」由此成立
  `@anchor symbol resolveSessionIdToPersist`
  `@anchor test Relay/tests/session-crossing-persist-live-sid.test.ts :: "CC 自产的真 session 覆盖配置值——事故的核心一条"`
  `@anchor test Relay/tests/session-crossing-persist-live-sid.test.ts :: "进程已驱逐时退回 state，绝不退回配置值"`
  `@anchor test Relay/tests/session-crossing-persist-live-sid.test.ts :: "三者皆空时返回 undefined，不编造值"`
  `@anchor test Relay/tests/session-crossing-persist-live-sid.test.ts :: "state 已有陈旧值时必须回写——旧 guard 正是在这里失守"`
  `@anchor test Relay/tests/session-crossing-persist-live-sid.test.ts :: "驱逐→重建后拿到的仍是 stats session，不是昨天那条"`
- [x] codex 线程号即时 flush（不等去抖）；关机中断的 prompt 持久化重注入
  `@anchor symbol persistThreadId`
  `@anchor symbol persistInterruptedPrompt`
  `@anchor test Relay/tests/codex-conversation-runtime.test.ts :: "persists the in-flight prompt when Relay shuts down during a Codex turn"`
  `@anchor test Relay/tests/codex-conversation-runtime.test.ts :: "serializes queued turns while one codex child is in-flight"`
- [x] 平台 runtime profile 只物化新会话缺省，不伪造既有 session 迁移：管理器只读
  检查 state runtime；不一致时 `verify` 失败。CC `sessionId` 与 Codex
  `codexThreadId` 不互换；会话迁移必须走 `/runtime codex` 的 kill + clear 契约，历史
  transcript 文件不删除
  `@anchor test ComputeCost/tests/test_pulse_runtime_priority.py :: test_verify_detects_persisted_runtime_without_mutating_state`
  `@anchor test Relay/tests/commands.test.ts :: "persists a valid runtime and clears the active session"`

## 6. 已知两义与债（登记待裁）

`@anchor manual 双启动入口绕 drain :: cli 入口有完整六步收口,直跑 dist/index.js 的自启块不调 drain 直接停服;走后者即复现 busy CC 丢 jsonl 尾的 0624 事故;生产必须只走 cli 入口`
`@anchor manual 限流判据双实现已分叉 :: 池版把 allowed 前缀事件全放行(0625 修复),批任务版仍按旧判据,allowed_warning 在批路会被误判成 429 触发降级`
`@anchor manual 类型注释默认值集体过期 :: 池选项四个默认值注释与实现不符(300000 写成 90000 等),另有一个声明字段全池零引用属死字段;以实现与本 spec 为准`
`@anchor manual bare 参数语义分叉 :: 池内注释称 bare 已移除但路径推导仍按其语义,批任务的交互路仍真传 bare;两条 interactive 路已分叉且注释群自相矛盾`
`@anchor manual 排水四实现 :: 忙会话排水在池/cc-sdk/codex/管理器四处近乎复制,管理器版吞错;归一另案`
`@anchor manual state 模块无直接单测 :: 去抖/原子写/flush-dispose 差异只被通道测试间接覆盖;真值链测试齐备但模块自身裸奔`
`@anchor manual 无全局并发上限 :: 池只保证每租户单进程,租户数即进程数上限;单核生产机上租户增长时需另立闸`
`@anchor manual 守护链跨进程契约 :: busy 状态经 cc-status 端点被重启脚本消费以阻止 SIGKILL,busy 与状态机的不变式是跨仓契约,改状态语义须同步重启脚本`

## 7. 覆盖声明

本 spec 给三路选择与两级降级、provider 接入、池生命周期、SIGTERM 六步收口、state
真值链首次挂显式锚点。至此 Relay 六域 spec 齐（main-chain / channels /
guards-metering / scheduler-jobs / config-http / runtime-pool）。存量
cc-process-pool / conversation-runtime / runtime-fallback / session-resilience /
token-fallback 五目录现行契约由本文接管。
