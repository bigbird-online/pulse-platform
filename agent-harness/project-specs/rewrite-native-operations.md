# WI018 replacement candidate: native Pi integration

Production installation binds the rewritten core directly to the existing PULSE
production Git ledger and ref. All profiles share that authority and namespace;
the policy has production_ledger instead of database, legacy_catalog or
migration_peers. Historical WI identities and bytes stay in place. SQLite is
transaction memory only; there is no second durable production business store.
Isolated fixtures and explicit cross-store migrations are separate modes, never
the normal deployment or continuation procedure. Installation is not channel UAT.

Production storage v2 keeps immutable decisions in bounded objects and private
originals outside Git. Its legacy event projection preserves the original legacy
bytes and one current journal watermark; full native history is queried through
the core. Raw Pi transcripts are never copied into this projection or Git objects.
An unavailable private original does not invalidate a recorded business decision.
See [production storage](rewrite-production-storage.md) for custody and isolation.

After compatible readers are installed on every participating node, the operator
may use `harness_rewrite.release storage-convert --root ROOT --wi WI` to convert an
existing native journal without adding a business decision. This entry executes
the immutable active bundle, validates exact history and preserves original
request receipts. `--finalize` replaces `--wi` after all native journals have been
converted; it retires the old-format lookup path. Repeating either step is safe.
Run conversion as the production service account so its private evidence custody
remains readable. Conversion does not remove already published Git history.

Use the installed canonical Python with `-I -B <installed-root>/active/harness.pyz`.
The business entry accepts `identity`, `execute --input command.json`,
`inspect --wi WI`, and `resume --wi WI`. `inspect` reads facts and rebuilds its
disposable view; `resume` also advances a bounded batch of pending effects.
With writer contract 3, an absent execution policy defaults to manual: execute
records effects and explicit resume advances them. Only operator-installed
automatic mode drains after producer commands. Audit mode reads without
materializing views or constructing effect adapters. Each advancement obeys the
installed batch budget. An observed native verifier session proves dispatch,
never ACK, verdict or human approval.

After each committed revision, once the journal transaction and ledger connection
have closed, the kernel renders the WI project page `<views_root>/<wi>.html` with
its sidecar `<wi>.state.json` (production: `~/.local/share/pulse/tools/scl-core/views`).
Rendering is deterministic code over the committed response (no model call),
writes atomically under the views publication lock, never replaces a newer revision,
and swallows every failure: the business result is returned unchanged. `standard`
pages show overview, acceptance status, plan nodes, PRs, collaborations and
records; `lightweight` pages omit plan nodes and collaborations. The page embeds
an `artifact-state` JSON block and lists changes since the previously rendered
revision. Execute and state inspect responses carry `project_page {path, revision}`;
`inspect` fills a missing page but does not rewrite a current one. The template is
modified from anthropics/claude-plugins-official project-artifact (Apache-2.0) and
ships in the bundle with `project_page.LICENSE`.

`record_project_narrative {banner?, next_steps?, risks?, decisions?, background?}` lets
the working Agent (the holder, or the same Agent appending as with `checkpoint`) add its
judgement to the page: banner and list items at most 300 characters (lists of at most
5, 5 and 10), background at most 3000; plain printable text only. The kernel keeps the
latest narrative (`NarrativeRecorded`, observation `narrative`); the page escapes every
character, links only http(s) URLs, and marks it possibly stale once newer progress
records exist or it is older than three days.

The Host derives identity from the actual Pi parent process and its original
session header. It does not accept actor, session, generation or approval booleans
from business arguments. Run business commands in Muse Pulse's existing Pi
conversation. Muse Agent installs and reads installation facts only.

Command envelope:

```json
{"request_id":"stable-request-name","intent":"record_work","work_item_id":"returned-WI","input":{"summary":"Actual work performed","resources":["repo"],"artifacts":[{"file":"result"}]},"files":{"result":{"path":"<lab>/work/result.md","logical":"repo/result","version":"v1","access":"team"}}}
```

`files` captures real bytes from admitted roots. `{ "file": "result" }` within
input becomes that captured artifact reference. Do not invent hashes or write
the database, view JSON, installation policy, native transcript or human ingress.
Keep a stable request ID for retries of the same intention; new business attempts
use a new ID. `result.original_request_result` is immutable; `latest_observation`
and `inspect` show current progress. An error after a durable result is not
permission to manufacture a new request or repeat an external send.

For human-confirmed decisions, set envelope `human_input_ref` to the exact real
reference supplied in this conversation by Relay. The Host checks the original
ingress and its presence as a user message in this native session. It does not
infer approval from assistant prose. Use only decisions actually authorized by
the human/controller for the stated candidate, plan or definition.

Each intent's input is readable from the core itself, so a caller that cannot
read this file or the lifecycle skill need not guess: `inspect --view guide
--intent NAME` (`pulse_work_item` action inspect, view guide, intent) returns its
fields with types, required and optional, whether `work_item_id`,
`human_input_ref` and `files` apply, a complete example envelope and common
refusals with their fixes; without `--intent` it lists the covered intents. It
reads no policy, ledger or WI. The top-level field sets in
`harness_rewrite/guide.py` are the sets the kernel validates, and tests execute
every example envelope. An `invalid_input` or `source_invalid` refusal of an
execute carries `error.input_guide` (`intent`, the one-line input shape and where
to read the full guide); the tool brief keeps only one line pointing at it.

Native CC, Codex and Pi readers tolerate complete lines containing only JSON
whitespace (space, tab, CR or LF). Such separators carry no input or authority.
Reads preserve the original bytes, physical offsets and input references; they
never rewrite a transcript. Nonempty malformed complete records remain invalid,
including records that appear to be internal metadata: guessing their meaning
could hide a later human decision. An unfinished trailing row remains pending.

For resumed Codex desktop threads, session_meta.id remains the logical session
identity even when the filename includes a new rollout suffix. Read the native
account's current rollout locator in read-only mode, then validate the original
header, source-root containment, ownership, workspace and delegation boundary.
The locator supplies no business authority. A missing or invalid selected
original cannot fall back to an older transcript or its human decisions; multiple
originals without a current locator are unavailable, never ordered by mtime.

Continuing an admitted historical execution preserves its current frozen
candidate and review stage. New human-authorized work against that candidate
uses revise_work with the original candidate_id, reason and actual input ref;
historical candidates without a graph use nodes: []. This keeps earlier evidence
and clears only the current freeze. Session discovery, inspect and resume do not
approve a candidate or grant permission to bypass its gate.

## Original production WorkItems

Use Relay's pulse_work_item tool (list, inspect, resume, execute); its result is
returned to the current Agent in the same turn. Human-decision-required is not
successful takeover or a queued background activation. Inspect the original
definition/history and existing affiliation first. Only undecided choices need
human input. Record the real decision with record_affiliation (type, ref,
optional work_class, reason) and its original human_input_ref, then resume the
same WI. Never infer consent from the registry's candidates.

The tool's text is a brief of at most 512 characters for every action and
outcome, because the core's responses carry whole WI views (megabytes for a large
WI) and the text enters the model's context: status, request_id, WI and local
revision, error, next_action, the request's own result, a line for a decision only
a human can make, and `more:` naming the largest parts of the response with their
sizes. A read (`list`, `inspect`, `resume`) taking `path` (a dotted path into the
response, `""` for all of it) and `page` returns that part in pages of 4000
characters instead; the core never receives these two fields. An `execute` is never
repeated to read it: `path` on `execute` is refused before any core call, and its
full result is read back with `inspect` view `request`, its `request_id` and `path`.
The tool process keeps its last 32 original execute responses by `request_id` and
answers that read from them without a core call, because the core journals no
rejected request and cannot read a creation that returned no WI ID; other request
ids are read from the core. The full response stays in the tool's details.
A `resume` result carries `result.latest_checkpoint` (`record_id`, `author`,
`summary`, `position`: its 1-based place among the records, which later work
records may follow), the handover a successor after a rollover, continuation or
switch starts from. The brief's priority is the status line, `next_action`, the
error, then this checkpoint: its record and as much of its summary as the room
left after those allows (at most 220 characters, at least an opening, else the
line is left out), then the rest; `more:` names it first.

A refusal names its recovery. Execution refusals (`No current execution grant`,
`Current resource execution scope required` and the workspace scope refusals) add
the first failing condition: the WI is not active, the parent of a child is not
active, a frozen candidate blocks work, the resources lie outside the WI's
resources, a workspace on them is unsettled, a grant delegated them, another
session of the same Agent holds the WI (the human sends `/wi continue WI` in this
session), or another Agent holds it. `retry_effect` on an effect that is not
`observed_absence` says what to do for its status: an unknown or started effect is
read back by resume; a failed verifier dispatch continues through the holder's
resume; a failed merge needs a new round.

record_affiliation never replaces a different recorded decision. To move a WI
to another registered affiliation, the owner uses revise_affiliation (type,
ref, optional work_class, reason) with the human_input_ref approving exactly
that input. The target must be registered. The AffiliationRevised event keeps
the replaced record (`previous`), the observation it replaced and the reason;
the new record carries `revised_from`. The latest revision is the WI's
affiliation: a title/source CR mention, machine OA field or registry override
that names something else no longer conflicts with it and is reported under
`superseded` in the affiliation observation. Malformed machine fields, the
legacy cutoff and later deregistration of the revised target still fail
closed. The affiliation observation reports the work class in force (the
recorded one, else the source field's). A revision writes no event only when
the admitted type, ref and work class all stay the same; `revised_from` keeps
the replaced work class, including one derived from source. Readers older than this event return journal_reader_upgrade_required.

An original draft with no resource scope can supply resources with its first
adopt_plan; that scope and the plan share the same real confirmation. Then
start_work uses the adopted graph revision. `resume` continues the same authenticated
original Pi execution after verifying its state, registration, checkpoint and
production Git audit. `review_existing_execution` accepts `{}`; the Host supplies
original evidence, never caller scope. No reinit, new startup or additional
business approval occurs. Accepted originals are retained in the sole production
journal; subsequent continuation does not depend on the old source. Inspect stays
read-only. Missing original evidence is a compatibility gap, not a reason to ask
for the same business approval again. See [original execution](rewrite-existing-execution.md)
for the historical input and publication checks.

Production commits use the fixed SCL Production Ledger service identity for both
Git author and committer; interactive Git configuration and Relay HOME do not
select the decision maker. Authenticated caller and human-source provenance stay
in the original journal. No account or repository Git configuration is rewritten.
Each transaction rebuilds its related-document index once from the freshly
observed tree; no cross-request cache or second ledger is introduced.

Production Git commands and object streams discard inherited repository-local
Git environment, including a calling hook's repository, object directory and
index. They retain operator transport/credential configuration and use only
core-owned transaction index overrides. A business repository's pre-commit or
pre-push hook cannot redirect ledger reads or writes, or change its own staged
content through a ledger transaction.

An admitted original workspace retains its journal-bound physical location.
Replacing it may allocate under the current Host root for the same admitted
repository, using the same repository-identity check as original continuation.
A changed repository is rejected. Replacement does not rewrite or retire the
original directory, discard uncommitted results, or create another WI.

Production local_committed means the original production ref includes the exact
decision. Publication acknowledgement loss returns ledger_commit_unknown unless
readback proves success. Reuse the original request after readback. Old writers
must consume the installed production ledger guard before native WI mutations
are enabled. The installer validates the exact proposed bundle against the
production records before moving its program pointer and performs no WI write.

A `journal_reader_upgrade_required` result identifies a WI whose event contract
is newer than the installed reader. It does not mean original bytes are damaged.
Use the platform CD to adopt a compatible main core; do not reinitialize the WI,
rewrite its journal, or bypass required global accounting checks. Independent
WI operations remain scoped to their own facts. See [reader compatibility](rewrite-reader-compatibility.md).

## Standard and lightweight work

Create with `create_draft` or `create_lightweight`; input is
`summary`, `why`, `what`, `acceptance` (nonempty text list), `resources` (logical scopes such
as `["repo"]`). `summary` is required: one plain line, at most 120 characters, telling a
person what the WI does and why, without IDs, dates or process notes; Stats shows it as the
WI's introduction. `rename_work` with `reason` and `title` and/or `summary` revises either later. Owner is the actual native actor; ID is returned by the kernel.
Draft does not grant execution. Lightweight then uses `start_work` with `{}`.
Standard first uses `adopt_plan`, then `start_work` with `graph_revision:1`.

`adopt_plan` input is `{plan_text, graph}`: `plan_text` is the plan's own text as a
string and `graph` is the JSON object below, both inline. Do not pass them as
`files` or `{"file": name}`: a placeholder becomes a captured-artifact reference,
not the file's content, and is rejected. Only the WI owner adopts a draft's plan,
with the human's newest message as top-level `human_input_ref`; the graph's
`confirmation_ref` is that same reference. Replace business text and returned WI:

```json
{"intent":"adopt_plan","work_item_id":"returned-WI","human_input_ref":"<64-hex>","input":{"plan_text":"The agreed plan, as text","graph":{"work_item_id":"returned-WI","graph_id":"main","revision":1,"definition_revision":1,"objective":"Produce the agreed report","acceptance_refs":["AC-1"],"confirmation_ref":"<same 64-hex>","nodes":[{"node_id":"write","kind":"work","purpose":"Write and check the report","executor_role":"producer","input_refs":[],"output_contract":{"artifacts":["report"],"outcomes":["success","failure"]},"resource_scope":["repo/report"],"required":true,"completion_rule":{"join":"all","success_outcomes":["success"]}}],"edges":[]}}}
```

Edges are `{from,to,condition}`. Claim a ready work/decision/wait node with
`claim_task` (`graph_revision`, `node_id`); retain returned attempt_id. Record
the actual result using `record_task_result` (`attempt_id`, `outcome`, `outputs`
mapping declared output names to file placeholders). Host supplies observation
provenance. Claim is required before work, and successful process exit alone
does not finish the task. Last required task freezes the candidate and records
an independent verifier assignment and dispatch intention in the same commit.

The installed adapter captures exact candidate inputs and launches the existing
Pi/OpenAI gpt-6.1-sol with a separate native session. It consumes existing auth
in place. Inspect/resume later to observe its ACK and actual four-way verdict.
Do not launch another verifier manually or claim accepted from dispatch success.

Human `record_review` input is `candidate_id`, `decision` (`approve` or
`changes_requested`), `reason`. Approval needs the exact candidate's accepted
independent result or explicit prior waiver. `revise_work` takes candidate_id,
nodes and reason; affected/downstream results are invalidated, earlier evidence
stays in history. A new candidate requires fresh verification and review.

`amend_definition` takes previous_revision, why, what, acceptance and reason,
plus real human source. Then use `revise_plan` with previous_revision, graph,
plan_text, reuse_results and reason, confirmed for the changed definition.
`add_requirement` takes previous_revision, what, acceptance, material_refs and
reason, plus real human source. It appends the requirement to the same WI: the
original why/what/acceptance stay byte-identical, the new criteria follow them,
and `definition.additions` records what, material_refs, reason and
acceptance_from. Like `amend_definition`, it stops the current plan until
`revise_plan`; a later `amend_definition` rewrites the whole definition and drops
the additions from the current definition (history keeps them).
`rename_work` takes title/reason and does not invalidate execution.

`declare_delivery` input is candidate_id (null for lightweight) and deliveries:
`[{artifact:<already-captured-reference>,target:"unique-relative-path",
required:true,node_id:null}]`. Real file delivery uses only the operator-selected
root, exact captured bytes and no-overwrite publication. Conflicting user files
are preserved. Completion waits for required deliveries to be observed.
`complete` takes candidate_id. Logical completion and resource retirement are
separate; managed resources use the explicit inventory and retirement contract below.

Lightweight `record_work` takes summary, resources and artifacts; retain record_id.
After actual acceptance, `complete_lightweight` takes all record_ids and all
acceptance_refs, with real human source. No independent verifier is required.
`record_ids` is exactly the set of `WorkRecorded` ids (checkpoints are not work
records), each carrying artifacts. `acceptance_refs` is exactly `AC-1` … `AC-n`,
numbered by the order of `definition.acceptance` (inspect also lists them as
`acceptance: [{ref, text}]`). The human acceptance message
goes in the top-level `human_input_ref`, not in input:

```json
{"intent":"complete_lightweight","work_item_id":"returned-WI","input":{"record_ids":["op_…"],"acceptance_refs":["AC-1","AC-2"]},"human_input_ref":"<64-hex>"}
```
To upgrade the same active lightweight WI, `promote_workflow` takes graph,
plan_text and reason with real human source; the graph starts at revision 1.
Existing work records and startup_profile remain unchanged; current standard
completion rules then apply.

## 子 WI

设计见 `pulse-space/memo/2026-10/2026-10-29-child-work-items.md`。新事项的目标或验收与当前 WI 不同时，
持有父 WI 的会话在其下建子 WI，交子代理完成，自己验收并结束。只允许一层：子 WI 不能再建子 WI。

`create_child` 不带 `work_item_id`；input 与 `create_lightweight` 相同（`summary`、`why`、`what`、
`acceptance`、`resources`），另带 `parent`，不收 `owner`。父 WI 须为 active、由调用会话在父 WI 当前代次（generation）持有，且本身不是子 WI；CLI 取父 WI 的当前代次。
子 WI 为 lightweight，建立即 active，持有者就是该会话（不需 `start_work`，没有执行权转移）；负责人、主身标签
（`main_only`）与父 WI 当前生效的业务挂靠沿用父 WI（挂靠记录带 `inherited_from: <父 WI>`）。结果返回子 WI 的 ID：

```json
{"intent":"create_child","input":{"parent":"wi_20261029_001","summary":"…","why":"…","what":"…","acceptance":["…"],"resources":["repo/child"]}}
```

派发子代理时，提示词首行写 `[WI: <子 WI>]`。子代理不声明 WI，在子 WI 上 `record_work`。

子 WI 的执行权一律跟随父 WI：对子 WI 的每个写操作（`record_work`、`checkpoint`、`review_child`、
`submit_pull_request` 等）都以父 WI 当前的持有者与代次判定，内核在同一事务里读父 WI 的日志；子 WI 自己
记录的持有者只是历史。父 WI 经 `assume_control`、`rollover`/`accept_transfer` 或 `continue_here` 换了持有者，
其子 WI 随之迁移，原持有者对子 WI 的审核与结束一律被拒（与任何 WI 相同，同一 Agent 的其他会话仍可追加记录）；
父 WI 不在 active（转交中、已停、已结束或读不到）时，子 WI 暂停：没有会话持有它，一切执行写入以
`authority_denied` 拒绝，包括协作授权（`grant_scope` 给出的范围）与同一 Agent 非持有会话的追加记录；只读不受
影响，仍可做的只有人类的 `stop`、`terminate_work`，用量与关注度观测、通知，以及已决定副作用的结算。子 WI 上的
协作授权只在授予它的父 WI 代次内有效：父 WI 恢复 active 时已换代（`reopen_work`、接管、交接都会换代），原授权
不恢复，由新持有者重新 `grant_scope`。子 WI 自身拒绝上述控制类命令。
inspect、列表与 Stats facts 中子 WI 的持有者、代次都按父 WI 显示。

`review_child` 作用于子 WI（`work_item_id` 为子 WI），由父 WI 当前持有者在父 WI 当前代次执行，input 为
`decision`（`approved` 或 `returned`）与 `reason`，记 `ChildReviewed`。须已有 `WorkRecorded`。`returned`
只记理由，子 WI 保持 active；`approved` 在同一决定里结束子 WI：照轻量收口核对成果（每条工作记录带
artifacts，全部验收标准，没有未结协作或交付），不需要人类引用；终态记 `review_id`，`source_digest` 为 null。
子 WI 不用 `complete_lightweight`。人类确认留在父 WI 结束时。

```json
{"intent":"review_child","work_item_id":"wi_20261029_002","input":{"decision":"approved","reason":"两条验收均已满足"}}
```

父 WI 仍有未结束（不是 completed 或 terminated）的子 WI 时，`complete`、`complete_lightweight` 与
`terminate_work` 拒绝，并列出这些子 WI。人类仍可 `terminate_work` 子 WI 本身。inspect 输出 `parent`（父 WI 的 ID
或 null）、`children`（`[{id, status, business_progress}]`，内核在同一次读取里读各子 WI 的日志得到；列表视图不含）
与 `child_reviews`。Stats facts 每个 WI 带 `parent`（字符串或 null），子 WI 结束时有 `closed_at`；子 WI 经
`review_child` 的完成记 `verification: agent_review`，`acceptance` 绑定 `review_id`，不算人类验收。
`create_child` 的原请求重试返回原结果，不重新核对父 WI 前提（与其他请求一样忽略代次变化）。

## PR 产出审核关卡

产出以内核合并的 PR 为准。设计与决定见
`pulse-space/memo/2026-10/2026-10-26-pr-output-review-gate.md`。该关卡独立于图候选
（`CandidateSubmitted`）与 Pi 独立验真：不设置 active candidate，不改变
`complete`、`complete_lightweight` 的条件，也不放宽图候选“生产 Agent 不能验真”的规则。

Host 策略的可选段 `pull_request_gate` 启用该关卡，例如：

```json
{"pulse_gh": "<release>/Scripts/pulse-gh.py", "gh": "/opt/homebrew/bin/gh",
 "relay_paths": "/Users/owner/.local/share/pulse/config/relay/paths.json",
 "repositories": ["bigbird-online/PULSE", "bigbird-online/CollaborateWithAI", "bigbird-online/Oikos"],
 "merge_method": "merge"}
```

仓库按名称精确匹配（大小写也须一致），不在表中的仓库一律拒绝。

- `submit_pull_request {repository, number, head_sha}`：标准与轻量 WI 均可，调用者须是持有者
  Agent 的会话且 generation 为当前值。Host 现场读 GitHub：PR 须开着、未合并、头等于 `head_sha`、
  基线为 main。作者即创建该头提交的会话：Host 先从登记会话自己的原生记录找提交执行窗口——执行类
  工具（CC 的 Bash；Codex 的 shell、shell_command、exec_command、exec、js 与 local_shell_call；Pi 的 bash；agy 的 run_command）的调用，
  其实际命令在任意位置运行 `git [全局选项] commit`（或 merge、pull；含 `git -C <目录>`、`-q`/`--quiet`、
  `-F`、heredoc、`--amend`、复合命令与 `cd … &&` 前缀），记下该调用的模型、工作区与开始、结束时间戳。
  Pi 记录首行须是 `session` 头（原生会话标识与 cwd；Pi 在头部 cwd 打开会话并在该目录运行工具），否则整份
  记录不可用。fork 与 branch（头部带 `parentSession`）先写新头部，再原样复制父会话条目（id、父指针与时间戳都不变），
  因此时间早于头部（或时间不可读）的消息是继承历史，不算本会话的轮次或提交窗口；其中的声明仍计入本会话：
  ComputeCost 按 fork 记录原样读取，继承的声明同样绑定 fork 的请求，整份记录声明过两个 WI 的 fork 不能登记。助手消息的 `model` 为该轮模型，此前最近的 `thinking_level_change` 为强度；窗口从含该
  `toolCall` 的助手消息行时间戳开始（Pi 先写入该消息再执行工具），到 `toolCallId` 与工具名都对应的
  `toolResult` 行结束，`isError` 的结果不认领提交（同 CC 的 `is_error`）。
  agy 记录是 `<brain>/<会话标识>/.system_generated/logs/transcript_full.jsonl`，会话标识即该目录名，不在此位置
  的文件不可用；每行是一步（带整数 `step_index` 与字符串 `type`），完整行缺这两项则整份记录不可用。`PLANNER_RESPONSE`
  步的 `model` 为该轮模型 ID，其档位后缀（`-low`、`-medium`、`-high`、`-xhigh` 等）为强度，无后缀的强度为
  unknown。该步 `tool_calls` 中的 `run_command`（命令取 `args.CommandLine`）可开窗，开始于该步 `created_at`。调用不带
  id；结果（其后到下一个模型步之前的 `GENERIC` 步）不指名调用，成功结果不回显命令，并行结果按完成先后写入，
  所以只在唯一读法下把结果配给命令：该步恰有一个 `run_command`，且其后恰有一条命令结果（去掉开头的
  `Created At:`/`Completed At:` 行后，以 agy 的命令结果行开头：`Command exited with code`、`Command failed to run:`
  或命令权限拒绝）。窗口结束于这条结果自己的 `Completed At`。输出文本没有来源标记（复合命令与 Git hook 能打印同样形状的文字），所以 agy
  只在能静态排除其他输出的一种形状下认领提交：命令行恰为 `cd <字面绝对路径> && git commit <参数>`（没有其他运算符、子
  shell、变量、命令替换或注释），参数含 `--no-verify` 或 `-n`（不跑 pre-commit 与 commit-msg hook），不含 `-q`/`--quiet`；
  结果为 `Command exited with code 0.`，该步不是 ERROR、不带 `error`，且输出第一行就是 git 自己的摘要
  `[<分支> <sha>] <标题>`（或 `[<分支> (root-commit) <sha>] <标题>`，记为 `agy_commit`）。HEAD 日志核对再要求该工作区在窗口内
  恰有一条提交类移动，移到头提交，且由该 sha 指名；窗口内有第二条提交（同工作区的其他调用或会话），作者不明。两个会话
  的窗口都含同一移动时，全 PR 作者搜索报告两者，内核判为作者不明（`ambiguous_commit_authors`）。其他形状一律不认领。
  调用只属于自己的模型步，不延续到下一步。一步有多个命令、结果缺 `Completed At`、被截断或认不出是命令结果、失败、非零
  退出、没有结果，或首行不是指名该提交的摘要行，都不认领；记录里步序号重复或倒退时，整份记录不认领任何提交：作者不明，
  转人类。agy 会话的提交要自动派审，须按上述形状单独一步执行；否则走人工确认。
  agy 记录没有工作目录，也没有 `declare_session_wi`：工作区只认上述 `cd <字面绝对路径>`，声明为空。
  工作区：Codex 先取工具输入的 `workdir`，否则会话 cwd；再按 shell 作用域静态读解码后的实际命令
  （`bash -lc '<脚本>'` 读其脚本）：只有与提交命令处于同一 shell 作用域、在其之前确定生效的 `cd <目录>`
  才改变工作区，最后取该提交命令的 `git -C`（均按 shell 引号规则解析，相对路径按前一级解析）。
  `( … )` 子 shell 的 `cd` 只在子 shell 内有效；后台（`&`）中的 `cd` 不影响后续命令；管道中的 `cd`
  使工作区不明（zsh 与 bash lastpipe 在当前 shell 执行管道最后一段），赋值同理；`$( … )`
  命令替换在子 shell 中运行；heredoc 正文是数据。`$名称` 只展开为同一作用域无条件赋值（单独的
  `名称=/目录` 或 `export`）的字面量。`cd` 只在条件下执行（`||` 之后、if/while/for/case 或 `{ … }` 内、
  `a && cd X; …`，或后接与提交无关的 `&&`/`||` 链；`cd X && git commit` 与 `cd X || exit; git commit`
  确定），或遇到 pushd/popd、eval、source、exec、函数定义、其他变量、目录中的命令替换、`~`、`cd -` 等
  无法静态确定的写法时，工作区不明，该窗口不认领任何提交：宁可漏认转人类，也不误认。窗口不依赖工具输出：`git commit -q` 不打印
  `[分支 <sha>]`，输出里恰有的该行只作附加信息（`printed`）。再由该工作区自己的 HEAD 日志决定窗口创建
  了哪些提交：只读该工作区的 `<gitdir>/logs/HEAD`（linked worktree 为 `worktrees/<名称>/logs/HEAD`），
  不读其他工作树或分支的 reflog；取 HEAD 确有移动（前一 sha 不等于新 sha）、操作确实创建了所移到的提交（`commit`、
  `commit (amend)`、`commit (merge)`、`commit (initial)`；merge、pull 产生的合并提交（`Merge made by`）；
  rebase 过程中生成新对象的 `(pick)`、`(reword)`、`(edit)`、`(squash)`、`(fixup)`、`(continue)`，含
  `rebase -i (pick)` 与 `pull --rebase … (pick)`；移到已有对象的 `(start)`、`(finish)`、checkout、reset、
  `Fast-forward` 等不算，如 `pull --rebase` 的 start 指向的上游提交不归执行者）且时间落在该调用
  开始与结束之间（容差 2 秒）的条目；移动到的提交为头提交、对象存在且可从某个分支到达，该窗口即头
  提交的作者证据。执行成功但 HEAD 未动的命令（如干净工作区上的 `git diff --cached --quiet || git commit`）
  不认领任何提交。同一窗口内的多条提交都归该调用（一条命令可提交多次），`--amend` 产生的新 sha 同样
  归该调用。只有核对通过的窗口才是作者证据，其模型即作者模型，须属已知家族（`claude-*` 为 Claude，
  `gpt-*` 与 `codex-*` 为 GPT，`gemini-*` 为 Gemini）；审核运行时取另一家族：Claude 作者派 Codex，GPT 作者派 CC，
  Gemini 作者派 Codex。输出文本本身不是凭证；没有核对通过的证据（另一会话代为登记、`printf` 伪造而工作区
  HEAD 日志无对应提交类移动的提交行、时间窗外的提交）即 `source_unavailable`。例外是本 WI 的前任持有
  会话：内核按日志把每个卸任的持有者（经交接、在别处继续或接管）记入 `former_holders`；头提交经下述全 PR
  作者搜索核对为同一 Agent 某个前任持有会话所创建时，作者即该会话及其那一轮模型，后继持有会话可登记，
  其归属按其他提交作者的规则判定。别的 WI 的前任、同 Agent 未持有过本 WI 的会话都不算。工作区被删除后其 HEAD
  日志随之消失，提交须在删除工作区前登记。审核者资格核对“提交过本 PR 任一轮的头”时，核对通过的
  窗口与执行类调用输出中打印的提交行都算。登记会话为 CC 时，候选还包括它派出的子代理（Agent/Task 工具，含
  `isolation: worktree`；Agent/Task 调用本身只证明派出关系，其提示或返回摘要里的提交行不是提交证据）：CC 把子代理记录写在 `<会话>/subagents/agent-<agentId>.jsonl`，旁有记下派出
  调用 `toolUseId` 的 meta 文件。子记录只在主记录能证明派出关系时采纳：主记录有该 Agent/Task 调用，
  其结果行 `toolUseResult.agentId` 指名该子代理；meta 文件若存在，其 `toolUseId` 须等于该调用；子记录的
  每个 user/assistant 行都是 `isSidechain`、`agentId` 为该子代理、`sessionId` 为主会话。任一条不满足，或
  子记录有完整坏行，整个子记录不采纳（同 Agent 的其他会话、目录里没有派出证据的“子会话”都不算）。
  只读直接子代理。子代理的提交与主会话用同一套工作区 HEAD 日志时间窗与分支核对，作者模型取子代理那一轮
  的实际模型，作者会话是子代理自己的会话标识（原生标识 `<主会话>/subagents/agent-<agentId>`）。
  Codex 本 Agent 的会话目录中未见子会话机制的记录；Codex 若派生子会话，其记录是独立会话文件，不按本
  规则归入登记会话，提交须由该会话自己登记或经下述全 PR 作者搜索识别为独立作者。
  Pi 的 `declare_session_wi` 是 Relay 扩展工具，按 ComputeCost `pi_request_purpose` 的同一规则计入：同一调用 id
  恰有一条调用和一条结果，参数恰为 `wi`（`wi_YYYYMMDD_NNN` 或 none），结果不是 `isError`、只含一段文字且逐字等于扩展回执
  `{"schema":"pi_request_wi/v1","wi":…,"origin":"agent_declaration"}`，调用与结果沿 `parentId` 链走到同一条用户消息
  （同一 id 有冲突副本时不成立）。
  父 WI 对子 WI 有完全权限：登记目标的父子谱系（目标本身、其父 WI 与其子 WI）内的产出可登记在谱系任一 WI 上，成本仍由 ComputeCost 按请求划分。
  头提交由登记会话自己创建时，它若以 `declare_session_wi` 声明过 WI，声明须都在该谱系内；声明了谱系外的
  WI 时，以创建该提交的那一轮所在请求的声明为准（`request_declared_wis`，见下；多个窗口都创建了该提交时须全部非空且一致），它都在谱系内才接受；
  它是别的 WI 或证明不了都拒绝，并给出所依据的 WI；头提交由子代理创建时，按下述作者归属判定；采纳的子代理若有声明，也须都在
  该谱系内，否则同样拒绝。作者是 PR 相对 main 引入的全部提交（GitHub 列表，至多 250 个）的生产会话：Host 在本 Agent
  的 CC、Codex、Pi 与 agy 会话目录（策略中未写 `runtime` 的 profile 为 Pi；agy 只认上述 transcript 位置）中，对每个提交用同一套工作区 HEAD 日志时间窗与分支核对找生产会话与模型
  （CC 会话连同按上述规则采纳的子代理一起搜索，子代理以自己的会话标识计为作者，所属主会话的声明
  随条目一并给出，与子代理自己的声明分开；主会话条目同样带 `request_declared_wis`，按上述规则代替会话声明，
  该会话有多个窗口创建同一提交时，须每个窗口的证明都非空且一致，否则为 null；Pi 记录只认其原生位置与文件名：距目录根至多两层、路径不含
  `.` 开头的目录、不在 Relay profile 目录内 Pi 进程的 HOME（`agent/`）中、文件名为 `<开始时间>_<头部会话标识>.jsonl`）。任一作者归属于该谱系以外的 WI，登记同样以 `domain_conflict` 拒绝并给出该 WI，
  与登记会话的声明规则一致。
  子 WI 细化作者的归属：Host 读每个子代理首条提示的首行，匹配 `^\[WI: (wi_\d{8}_\d{3})\]` 时记为
  `subagents[].wi_marker`（全 PR 作者搜索的结果同样带 `wi_marker`），否则为 null；同时记 `dispatch_declared_wis`：
  ComputeCost（`cc_request_purpose`）把该子代理每一轮用量计给的 WI，由 `harness_rewrite/child_dispatch.py` 在同一份
  原生记录上证明。只在 ComputeCost 结论确定的窄形态下给出：走到的记录 uuid 不重复；子代理首条记录就是带标记、无
  父记录的派发提示；每条用量记录沿文件内更早的父记录走到文字输入或协调输入，它不带其他原生来源，其 `promptId`
  对应主会话恰好一条更早的文字输入；每个这样的请求至少有一条 ComputeCost 认可的声明（调用与唯一结果互相链接，
  结果带该请求的 `promptId`，调用的父链走到该请求，经同 `promptId` 的压缩摘要也算，回执与服务端确认逐字一致），
  且结果带该 `promptId` 的全部声明都是同一个 WI，或者它们分属多个 WI（如先 `none` 后某 WI）但每一条都被认可，此时
  每一轮按 ComputeCost 的请求内换 WI 规则，计给结果时间不晚于该轮的最后一条声明（早于全部声明的轮次计给第一条），
  同一时刻的声明须是同一个 WI；主会话没有解不开的声明（否则 ComputeCost 作废整场声明）；
  该会话的其他记录文件（其他子代理、分叉副本、嵌套 `main.jsonl`）与子代理及主会话记录不共享任何 uuid 或用量
  消息 id（ComputeCost 按这两种身份跨文件合并记录，副本可把同一轮改挂到别的请求）。取这些 WI 的并集，另记
  `dispatch_since`（这些记录最早的时间）；其余情形（只靠时间戳排序、分叉、任务通知转接等）
  一律为 null。并集含父 WI 时，ComputeCost 只经其登记表（账户主目录
  `.local/state/pulse/compute-cost/sources.json`）指定的子 WI 导出才把用量改记子 WI，Host 按 ComputeCost
  `child_work_items` 的同一规则读该导出中的子 WI，记为 `cost_child`（父 WI、状态、建立与结束时间），读不到为 null。
  会话自己的提交同理：CC 主会话每个核对通过的提交窗口记 `request_declared_wis`，即 ComputeCost 把执行该
  `git commit` 的那一轮（同一用量消息 id 的全部主记录）计给的 WI，由 `child_dispatch.request_charged` 用上述
  请求证明给出：该调用恰在一条主记录上，这些主记录都走到同一个请求，该请求的声明满足上述条件，主会话没有解不开的声明，
  其他记录文件不共享主会话的 uuid 或该消息 id；否则为 null。Host 只记录，不判定。全 PR 作者搜索中子代理的条目把它自己的声明（`declared_wis`）与所在会话的声明（`session_declared_wis`）分开：子代理自己的声明若有，须都在该谱系内；归属按下述规则判定 marker，marker 无效时取子代理自己的声明，否则会话声明。
  内核判定：marker 是未结束子 WI，`dispatch_declared_wis` 非空、只含该子 WI、其父 WI 或 `cost_served`
  （Host 按同一导出读出的其余已声明 WI）显示属于同一父 WI 的兄弟子 WI，且含该子 WI 以外的 WI 时
  `cost_child` 显示该子 WI 属于该父 WI、仍为 active、建立不晚于 `dispatch_since`，该子代理才归属该子 WI，此时
  ComputeCost 把它每一轮用量都计给该子 WI（`child_work_items.lineage_child`）；登记到父 WI 时，
  `dispatch_declared_wis` 只含父 WI 与该子 WI，或会话恰好只声明父 WI 也算；含兄弟子 WI 时须有与登记到子 WI
  相同的导出证明（`cost_child` 与 `cost_served`）。所以一个请求可派多个带标记
  子代理并行推进多个子 WI，各自归属自己的标记。marker
  是其他任意 WI（如登记目标本身）且 `dispatch_declared_wis` 恰为该 WI 时，子代理归属该 WI。否则
  继承子代理自己的声明，再否则继承会话的声明。作者归属只要在该谱系内即接受：父 WI 持有会话自己提交的头、未带标记的子代理提交都可登记到子 WI，
  归属子 WI 的子代理提交也可登记到父 WI。
  任一提交找不到作者（`unattributed_commits`，含提交列表不完整），任一提交被多个会话（含主会话与其
  子代理）的窗口同时认领、归属不明（`ambiguous_commit_authors`，不认定任何一方为唯一作者），或作者模型不属同一个已知家族
  （`mixed_author_families`，作者范围含本 PR 历史各轮的作者，与评分时的核对一致），该轮不派审，直接
  转人类确认，不自动通过。审核者须与所有作者（含子代理作者）的模型家族不同，且不是任一作者会话
  （登记会话与子代理会话都算）；审核会话自己或其采纳的子代理提交过本 PR 任一轮的头，同样不合格。
- `withdraw_pull_request {repository, number, reason}`：持有者 Agent 在原 WI 撤销登错的 PR（未开始
  合并时），取消未开始的审核与合并请求，理由写入 `pull_requests[].withdrawals` 供审计；之后可在正确
  WI 登记。已撤销的登记不再接受评分、回执、人类确认，也不开始合并或审核派发。仓库 + PR 号的唯一归属以各 WI 最近一次登记或撤销为准（生产账本为两类事件都写声明）。同一仓库 + PR 号在整个事实库只能属于一个 WI（生产账本以
  `.harness/core/pull-requests/<digest>/` 声明）。新头提交开启新一轮，未审轮次标为 superseded，
  尚未开始的合并动作被取消；合并尝试未读回或已合并时拒绝。
- `record_pull_request_review {candidate_id, score, reason}`：只审当前未审、带审核要求的轮次；
  审核者是作者 Agent 的另一会话，且不是本 PR 任一轮的作者会话。资格只取 Host 事实：本会话
  原生记录中每一轮的模型与强度、提交证据，以及 Host 以固定环境（仅 HOME=账户目录、
  PATH=/usr/bin:/bin、LANG 与策略中的 `PULSE_RELAY_PATHS`）运行已安装 Relay 的
  `compute-profile-cli.js resolve review --provider <runtime> --json` 得到的档位解析。依次核对：
  记录完整（缺模型或强度、或有完整坏行即 `reviewer_record_missing`；仅容忍未写完的末行）、
  运行时为 cc 或 codex、单一已知家族且不同于本 PR 各轮作者模型的家族、本会话没有提交过本 PR
  任一轮的头、档位为 `review` 且 provider 等于运行时、每轮模型与解析出的完整型号完全一致
  （CC 为 Relay 模型目录中版本最高的 `claude-opus-*`，Codex 为最新 Sol）、每轮强度为 high、
  xhigh 或 max、本会话以 `declare_session_wi` 只声明过本 WI（只认声明服务器自己的成功应答，与成本系统
  的校验一致）（否则 `reviewer_wi_undeclared`，一个
  审核会话只审一个 WI，成本在审核前绑定到该 WI）。任一不满足返回 `reviewer_ineligible`，原因以规则名
  开头，不写事件。评分 0–100，
  85 分及以上通过并在同一提交请求 `merge_pull_request`。
- 审核资格证据、档位解析与 GitHub 读数只写入首次提交的事件，不进入请求语义：同一请求重试返回
  原结果，即使原生记录此后增长。
- 一个 PR 最多三轮审核（通过但合并失败的轮次也计数）。之后登记的新头不再派审，状态为
  human_confirmation_required；第三轮未通过同样如此。此时只能由持有者 Agent 带本会话人类输入执行
  `confirm_pull_request_merge {candidate_id, reason, override_checks?}`（`human_input_ref`），绑定最新
  一轮的头提交。最新一轮的检查阻塞合并时（`checks_failed`，或因检查缺失而重试预算耗尽）也可如此
  确认，未开始的旧合并请求随之取消。`override_checks` 默认 false：不带它的确认仍要求检查全绿；
  为 true 时人类越过失败或缺失的检查，记入确认与合并快照。
- 长期合并授权（standing merge authority，人类一次登记、长期有效的决定）：人类在 Agent 持有某个
  active WI 的会话中批准 `grant_merge_authority {reason}`（`human_input_ref`），内核在该 WI 记
  `MergeAuthorityGranted`。与 `grant_access` 相同，Host 只接受本会话最新一条人类输入作批准，此后的
  人类消息（如撤销）使它失效；一条人类输入在整个事实库只能登记一次授权，不论该授权是否已撤销，
  重登须人类新的批准（生产账本以 `.harness/core/merge-authority/<digest>/` 声明，并发登记以
  `ledger_conflict` 失败）。该 WI 只承载审计记录，授权服务该 Agent 的所有 WI，直到撤销。
  有未撤销的授权时，该 WI 的当前持有者会话（完整 principal 相同且 generation 为当前值；同 Agent 的其他
  会话、旧会话一律 `authority_denied`）可不带 `human_input_ref` 执行 `confirm_pull_request_merge`，但只限
  最新一轮是三轮审核之后登记的新头（`review_block: three_rounds`，即第三轮之后修好再登记的头），
  且尚未确认；第三轮被否的那个头本身、作者归属阻塞（`unattributed_commits`、
  `ambiguous_commit_authors`、`mixed_author_families`）、检查阻塞的再确认与 `override_checks: true`
  仍须本会话人类输入。内核先读全部 WI 日志找调用者 Agent 的未撤销授权所在的 WI（读不到的日志不算），
  再在确认的写事务里重读这些承载 WI 并以此判定；承载 WI 的日志是该确认的发布依赖，确认发布前
  别处已提交的撤销使确认以 `ledger_conflict` 失败，原请求重试时读到撤销即拒绝。登记与撤销只读写
  承载 WI 自身。确认记录写入 `merge_authority {grant_id, work_item_id}`，`source_digest` 为登记授权那条人类输入；
  合并仍等被审头上的全部检查通过。`revoke_merge_authority {grant_id, reason}` 撤销：授权所服务
  Agent 的任一会话都可撤销，不需人类输入，不核对 generation，不论承载 WI 处于什么状态（停止、完成、终止，
  或子 WI 的父 WI 停止或结束）；撤销后确认重新需要人类输入。带人类输入的显式确认仍只要求持有者 Agent。
  inspect 的 `merge_authority` 列出本 WI 承载的授权（`active` 与撤销记录）。
  `@anchor test test_a_standing_merge_authority_merges_a_head_fixed_after_three_rounds_on_green_checks`
  `@anchor test test_a_standing_merge_authority_never_passes_failed_checks`
  `@anchor test test_a_standing_merge_authority_never_covers_the_rejected_third_round_head`
  `@anchor test test_a_revoked_merge_authority_no_longer_stands_in_for_the_human`
  `@anchor test test_a_merge_authority_revoked_after_it_was_found_does_not_confirm`
  `@anchor test test_a_merge_authority_revoked_on_its_carrier_before_publication_conflicts`
  `@anchor test test_a_merge_authority_follows_the_latest_human_word_and_one_approval_grants_once`
  `@anchor test test_one_human_approval_registers_one_merge_authority_even_after_revocation`
  `@anchor test test_one_approval_registers_one_merge_authority_across_the_ledger`
  `@anchor test test_a_standing_merge_authority_confirms_only_from_the_holder_session`
  `@anchor test test_an_explicit_human_confirmation_keeps_its_holder_agent_rule`
  `@anchor test test_a_merge_authority_on_a_child_is_revocable_after_the_parent_completes`
  `@anchor test test_a_merge_authority_on_a_child_is_revocable_while_the_parent_is_stopped`
  `@anchor test test_a_merge_authority_is_revocable_whatever_its_carrier_state`
- `merge_pull_request` 的快照固定仓库、PR 号、被审提交与 `base: main`。适配器只经策略中的
  `pulse_gh` 与 `gh`（须是实际存在、属本账户或 root、不可被组或他人写的文件），在固定环境
  （账户 HOME、PATH 仅含 `gh` 所在目录与 /usr/bin:/bin、`GH_HOST=github.com`，不继承调用者的令牌
  或主机，工作目录固定为 `/`）中调用 GitHub；`pulse-gh` 的内层驱动同样以 `python -I -B` 隔离运行、
  固定工作目录，不从调用者目录或 PYTHONPATH 导入。每个应答须指向所请求的 github.com 仓库与 PR。
  PR 仍开着且头相同，再经同一通道核对被审提交上的检查。发送前的读回与检查核对共用一个
  `pulse-gh session`：同样的已安装程序与固定环境，只做一次预检，之后只接受本仓库的
  `api repos/<仓库>/...` GET（不含 `..`），至多 8 个并发，每个应答带请求编号；互不依赖的首页请求
  成批发出；合并的 PUT 不经 session。同一判断内 PR 只读一次。
  - 应触发的工作流：先读 base 分支当前指向的提交（`git/ref/heads/main`），再在该提交上经 contents API
    一次列出 `.github/workflows/`（达到 1000 项、或应答不是条目列表即读不到）。只有列目录的请求被拒时
    才尝试证明目录不存在：根目录列表中没有 `.github`，或 `.github` 列表中没有 `workflows`；条目存在、
    类型不是目录或列表本身读不准，都是读不到。目录中每个条目须有 `name`、`type`、`path`、`sha`（40 位
    blob 名），`path` 须等于 `.github/workflows/<name>` 且 name 不含 `/`；缺字段或矛盾即读不到，不跳过。
    PR 自己新增、修改或改名的工作流文件（改动列表中 `.github/workflows/` 下直接的 yml/yaml，按其状态与
    `sha`）以 PR 版本替代 base 版本，删除或改走的不再计入；GitHub 用 PR 自己的版本运行
    `pull_request` 工作流。PR 版本同样按 blob 名读取与缓存，读不准即读不到，`checks.basis.workflows_from_pr`
    列出这些文件。每项自带 Git blob 名；文件按 blob 名读取
    （`git/blobs/<sha>`），缓存在账户效果服务的 `<views_root>/workflow-blobs/<sha>`，只在内容的 Git
    blob 哈希等于文件名时使用，名变才重读，缓存不可用只多读一次。不列仓库工作流注册表（工作流运行自带
    其文件路径）。对 PR 的完整改动文件列表（`pulls/<n>/files`，
    改名同时计新旧路径；条数须等于 PR 的 `changed_files`、文件名不重复，最多 300 个）套用
    `pull_request` 触发的 `branches`/`branches-ignore`（对 base 分支）、`types`（须含 opened、
    synchronize 或 reopened）与 `paths`/`paths-ignore` 过滤。模式按 GitHub 规则：`*` 不跨目录，`**`
    跨目录（`**/` 可匹配零层），`?`、`+` 修饰前一字符，`[...]` 字符类，前缀 `!` 排除，最后匹配者决定。
    任一工作流文件或过滤读不准即读不到。
  - GitHub Actions：分页列出该提交的全部检查套件（`commits/<sha>/check-suites`）与工作流运行
    （`actions/runs?head_sha=<sha>`）。每个运行须有其 Actions 套件，否则读不到；已配对的运行与套件都已完成
    而结论不同即读不到。Actions 套件没有对应运行时，只有已证明为空（`latest_check_runs_count` 为 0）
    才可处理：已完成的忽略，排队中的为等待（运行尚未生成）；非空或计数读不准即读不到。运行或其套件未完成为等待；已完成的运行只看当前尝试
    （`run_attempt`）的全部作业（`actions/runs/<id>/attempts/<n>/jobs`），每个作业须属该提交、该运行与该
    尝试。作业须与套件的检查数一一对应：当前尝试的作业在先，部分重跑未重复的作业名取其最近一次较早尝试的
    作业，合计须等于套件的 `latest_check_runs_count`，否则读不到；套件声明有检查而作业为空、或运行无任何
    检查与作业却称成功，都是读不到。“重跑”产生同一运行的新尝试，替代旧尝试的结果（如磁盘预检失败后重跑）；同一尝试中的同名作业
    全部参与判断；同一工作流的另一个运行不替代本运行，二者都计入。没有作业、或作业全绿而运行本身的结论
    不是 success/skipped/neutral（如取消、启动失败）时，运行本身计为一项。应触发的工作流在该提交上没有
    `pull_request` 运行，记为等待（`<文件> (not started)`）。
  - 其他应用：每个非 Actions 套件都完整列出其检查运行（每个名称最新一次），条数须等于套件自带的
    `latest_check_runs_count`（复读时一并核对），否则读不到。两者都为 0 的套件是应用的占位，不是检查，不论状态都忽略（PULSE 提交上 Vercel、Claude、Cursor 各有一个长期 queued 的空套件）；
    有检查运行而套件未完成为等待。被忽略的套件（应用、套件 id、状态）列在 `checks.basis.ignored_suites`。commit statuses
    （`commits/<sha>/status`，每个 context 一项）独立计入。不同应用、不同工作流、check run 与 status
    互不覆盖。
  - 每个列表每页 `per_page=100`、最多 3 页：每页报告的总数须相同，每行 id 须唯一，读到的行数须等于总数；
    分页中出现重复、总数变化或超过上限即读不到。读完全部作业后再读一次套件与工作流运行（不重读作业明细），
    任一套件或运行的标识、状态、结论、尝试号或套件检查运行数变化即读不到。
  - 没有任何工作流应触发，且该提交上没有工作流运行与 status、检查套件全部属于被忽略的套件：判定为 `not_required`
    （无需检查），可合并，依据（base 及其提交 `base_sha`、读过的工作流文件及其 blob 名 `workflow_blobs`、改动文件数、被忽略的
    套件）写入 `checks.basis`。
  分类：作业或检查运行未完成为等待；完成后
  success 为通过，skipped 与 neutral 为跳过（与 GitHub 必需检查的口径一致；工作流中 `if:` 排除 PR
  的作业，如 `deploy`、`natural-acceptance`，在 PR 上为 skipped），其余结论（failure、cancelled、
  timed_out、action_required、startup_failure、stale 等）为失败；status 为 success 通过、pending
  等待、failure 或 error 失败。判定依次为：有失败为 `checks_failed`，终态失败，不发送，PR 状态为
  checks_failed，作者修复后登记新头（或重跑检查转绿后以新的 request_id 重登同一头，原审核沿用；原 request_id 只返回原结果），或请人类带 `override_checks`
  确认；有等待为 `checks_pending`；不属无需检查而没有一项通过（如全部跳过）为 `checks_missing`；读不到为
  `checks_unavailable`。后三者都是 absence：不发送、不消耗审核轮次，按原效果的重试预算（8 次）与
  退避再读。人类越过只放行失败与缺失的检查：等待中的检查仍等待（它仍可能失败），读不到仍不合并。
  只有判定为全部通过（至少一项通过、其余为跳过）、无需检查或已越过时才调用合并接口并带 `sha`（等价
  `--match-head-commit`）；调用前在
  `<publication_root 或 delivery_root>/pull-request-merges/` 持久写入该尝试的发送标记。已合并的 PR 只有
  在被读回的那次尝试有发送标记、头等于被审提交且合并提交在 main 上（compare 为 identical 或
  ahead）时才算内核合并成功；否则（登记后被人工合并、尝试在发送前崩溃）为终态失败
  `merged_outside_kernel`。失败的合并效果不再进入读回或重发。头已变化、PR 已关闭或基线
  不是 main 为失败，作者须登记新一轮；GitHub 拒绝合并为 absence，可按原效果预算重试；读不到
  GitHub 为 unknown，只读回不重发。读回的头、合并提交、合并时间、合并者与发送前读到的检查写入 WI。
- 只有策略含 `pull_request_gate` 时，生产者工作进程与账户效果服务才持有该效果类型。
- 观察视图 `pull_requests` 列出每个 PR 的状态（awaiting_review、changes_requested、
  human_confirmation_required、merge_pending、checks_failed、merged、merge_failed、cancelled）、已审
  轮数、各轮作者模型、评分与理由、审核者 Agent 和观测到的模型与强度、最近一次合并尝试读到的检查
  （`rounds[].checks`：state 与通过、跳过、等待、失败的检查名，每组最多 50 个，超出的个数记在
  `omitted`；无需检查时 state 为 `not_required` 并带 `basis`）；会话标识不公开。`merge_blocked`
  写明合并卡住的原因：`checks_failed` 带失败检查名与未列出的失败个数（`failed_checks_omitted`）；合并请求重试预算耗尽为
  `merge_retry_exhausted`，带最后原因与恢复动作 `retry_effect`（缺失检查时另可请人类越过）。同一
  Agent 的其他会话在 next_action 看到待审轮次和要求的运行时；作者看到改后重登或请人确认；检查失败时
  作者看到失败检查名与修复或请人越过的提示；预算耗尽时 WI 所有者看到 `retry_effect`。检查失败时作者的 next_action 同样带 `failed_checks_omitted`。
- 威胁模型：该关卡防误操作与非蓄意自审（代他人登记、回显或 `printf` 伪造的提交行、旧版
  Opus/Sol 审核、调用者环境注入）。Agent 蓄意伪造 Git 对象或 reflog、或在同一会话中伪造原生记录，
  不在内核防护范围，残余风险由事后审计（Stats 异常：GitHub 已合并而内核无合并记录等）发现。作者
  绑定的是创建头提交的会话；分支上更早的提交可能来自其他会话。
- 审核派发：登记带审核要求的一轮时，同一提交请求 `pr_review_dispatch` 效果，快照固定原 WI、候选、
  仓库、PR 号、头提交、Agent、审核运行时与 `review` 档位。内核不复用 `verification_dispatch`（该效果
  绑定 Pi 独立验真者、`dispatch_root` 与固定 `verification_compute`，并要求验真者不是生产 Agent）。
  该效果只由 Relay 审核 gate 执行，生产者与效果服务的排空跳过它：
  `harness-native pr-review-dispatch --actor <agent> --runtime cc|codex [--cooldown-minutes 90]
  [--receipt-minutes 15]` 以受保护的已安装程序身份运行，只持有这一效果类型；同一 Agent 与运行时在
  本安装上只有一个 gate 运行（文件锁，忙则跳过）。每次运行最多启动一个审核，一个审核会话只审一个
  WI。开始尝试使用本次运行独有的请求标识（租约）：并发或重放的运行拿不到同一次尝试。批次
  （JSON `pulse.pr_review_dispatch/v1`，退出 0）含 `attempt_id` 与 `commands`：`declare`（声明参数）、`receive` 与 `review`（可直接执行的 `harness-native execute --input - <<'JSON'` 命令，信封内含 work_item_id；execute 不收内联 JSON，也不收 `--wi`）；Relay 以批次加任务提示启动审核会话。
  会话先以 `declare_session_wi` 只声明该 WI，再执行 `receive_pull_request_review {candidate_id,
  attempt_id}`（运行时接收回执：须为当前尝试、同一 Agent 的非作者会话、要求的运行时、只声明了本 WI，
  成本在审核开始前即绑定）。每次尝试只绑定第一个接收会话，其他会话的回执被拒；有回执的轮次只接受当前
  尝试接收者的评分，重派前的接收者不能再评分；gate 已开始尝试而尚无回执时也不接受直接评分，手动评分只
  用于尚未派发的轮次。已撤销的登记拒绝回执，gate 把其进行中的尝试记为失败。之后的 gate：该轮已审记为成功；被新头
  取代记为失败；无回执且超过接收时限（gate 或 Relay 在启动会话前中断）、或有回执但超过冷却仍未审，
  记为 absence 并在原效果的重试预算（8 次）与退避内再开始。预算耗尽时 `pull_requests` 的
  `review_dispatch.blocked` 写明原因 `review_dispatch_retry_exhausted` 与恢复动作，WI 所有者的
  next_action 为 `retry_effect {effect_id, reason}`。新头登记或审核在 gate 开始前完成时取消未开始的
  请求。没有可启动的请求时输出 `PR_REVIEW_SKIP` 并退出 3。

  派发节奏：任务每分钟运行一次门禁。`--default-minutes`（小时内的分钟，如 `7,22,37,52`）是缺省节奏：
  不带 `immediately` 登记的请求，要等登记后经过其中一个时刻才开始，错过的时刻照样计入；登记时带
  `immediately: true`（`submit_pull_request` 的可选布尔，缺省 false，记入轮次与派发快照）的请求在下一次
  运行即开始。不给 `--default-minutes` 时每个请求都立即开始；此字段出现前登记的请求视为已到期。
  每次运行仍至多开始一个审核。审核任务定义 `"maxConcurrent": 3`（Relay 同一任务同时运行的上限，缺省 1，
  见 `Relay/specs/relay-scheduler-jobs.md`），所以同一 Agent 同一运行时至多 3 个审核会话并行，其余请求由
  后续每分钟的运行依次开始。已开始的尝试在接收时限或冷却到期前不会再被开始，gate 又持文件锁、开始尝试用
  租约，所以并行的运行各自开始不同的请求，同一候选同一时刻只有一个尝试；一个尝试失败只按上段重派它自己。

  无变化的运行只读生产头：每次完整检查（拉取生产账本并回放）后，gate 在 `views_root` 下、锁文件旁
  按 Agent 与运行时原子写入私有记录 `.pr-review-dispatch.<agent>.<runtime>.state.json`，内容是检查前读到的
  生产头提交、gate 参数与安装的指纹、检查时刻，以及“最早到期时刻”：各未结请求不靠新账本写入即可变为
  可启动的最早时刻，取缺省时刻槽、接收时限、审核冷却与重试退避中最早者；被取代、预算耗尽等只能由账本
  写入改变的请求不计入；没有未结请求时为空。下一次运行先用一次 `git ls-remote` 读生产 ref 的当前头；头、
  指纹不变，最早到期时刻未到，且记录不足 30 分钟时，直接输出 `PR_REVIEW_SKIP` 并退出 3，不拉取也不回放。
  登记、回执、评分、重试等任何账本写入都会改变生产头，下一次运行即完整检查；本次已开始审核、读回被拒、
  头读不到、记录缺失或损坏、时钟早于记录时也完整检查。

  所有 Agent 共用读取：审核会话以任务的 owner 身份运行，内核只接受作者所属 Agent 的会话，所以每个
  Agent 各有一对任务（`<agent>-pr-review-codex|cc`，owner 与 `--actor` 为该 Agent，workspace 为其工作区）；
  同一安装上的所有 gate 在 `views_root` 下共用锁 `.pr-review-dispatch.shared.lock`（至多等 60 秒，拿不到
  则按上段单独读取）和两份记录。`.pr-review-dispatch.head.json` 存最近一次 `git ls-remote` 读到的生产头、
  读取时刻与安装指纹，45 秒内（短于每分钟一次的节奏）其他 gate 直接复用，所以每分钟只读一次远端头，
  不随 Agent 数增加。`.pr-review-dispatch.index.json` 存某个头下所有 Agent 与运行时的未结审核请求
  （`actor/runtime` → WI 列表）：头变化后第一个 gate 拉取并回放一次账本写入索引，其余 gate 在同一头、
  指纹相同且索引不足 30 分钟时直接读索引；本 Agent 与运行时没有未结请求即输出 `PR_REVIEW_SKIP`，
  不拉取也不回放；有则用索引中的 WI 完整检查并派发。缓存的头不超过 45 秒且在任何检查之前读取，记录与
  索引描述的都是该头或更晚的状态：账本写入最多把开始推迟 45 秒，不会被漏掉。身份规则不变：gate 只派发
  `--actor` 自己的请求，回执与评分仍要求作者所属 Agent 的非作者会话与另一模型家族。

  每个 Agent 每个运行时一个 Relay 任务，例如：

  ```json
  {"id": "pulse-pr-review-codex", "owner": "pulse", "schedule": {"expr": "* * * * *", "tz": "Asia/Shanghai"},
   "maxConcurrent": 3,
   "agent": {"computeProfile": "review", "runtimePreference": ["codex"],
     "gate": {"command": "/Users/owner/.local/bin/harness-native",
              "args": ["pr-review-dispatch", "--actor", "pulse", "--runtime", "codex",
                       "--default-minutes", "7,22,37,52"],
              "timeout": 150, "skipExitCode": 3, "skipOutput": "PR_REVIEW_SKIP"},
     "prompt": "你是 Pulse 的异构审核者。上方批次只有一项。先用 declare_session_wi 声明其 work_item_id（只声明这一个），再用 harness-native execute 提交 receive_pull_request_review {candidate_id, attempt_id}；先读 PR（repository、number）在 head_sha 的差异，定下要跑哪些测试、它们读写什么，再准备副本：D=$(realpath \"$(mktemp -d)\")（沙箱规则按真实路径匹配）；本地检出（工作区所在仓即是它时用工作区的仓，否则用 /Users/owner/Coevolution 下该仓）里 git cat-file -e <head_sha>^{commit} 成功就以它为 R，只读导出，不在共享检出 fetch；不成功就 git init -q \"$D/r\"，在其中 git fetch <该仓 GitHub 地址> pull/<number>/head，确认 git rev-parse FETCH_HEAD 等于 head_sha，以 $D/r 为 R；mkdir \"$D/t\" \"$D/.home\" \"$D/tmp\"，硬排除 X 为 ':(exclude).harness' ':(exclude,glob)**/.env*' ':(exclude,glob)**/secrets.yaml*' ':(exclude,glob)**/*.key' ':(exclude,glob)**/*.pem'，再按本会话文件访问规则加上该仓不得读取的目录；git -C R archive <head_sha> -- . X ':(exclude,glob)**/*credential*' ':(exclude,glob)**/*token*' | tar -x -C \"$D/t\"，导出其余完整树（含同 head 的 Scripts/、.github/、config/ 等测试依赖）；再补回名字含 credential、token 的源代码与文档：用 git -C R ls-tree -r -z --name-only <head_sha> 按 NUL 分隔读出路径，取路径含 credential 或 token（不分大小写）且以 .py .ts .tsx .js .mjs .cjs .md .swift .sh 结尾的，每条写成 ':(literal)<路径>'；清单为空就跳过这一步（只有排除项时 Git 会对整棵树导出），不空才 git -C R archive <head_sha> -- <这些字面路径> X | tar -x -C \"$D/t\"，补回同样受 X 约束，其余同名文件仍不导出；不从别的提交或生产目录补文件；被排除的文件是测试依赖时，相关测试记为未验证。把 (version 1)(allow default)(deny file-read* (subpath \"/Users\") (subpath \"/Volumes\") (subpath \"/private/tmp\") (subpath \"/private/var/folders\"))(allow file-read-metadata)(deny file-write*)(allow file-read* (subpath \"$D\") (subpath \"/Users/owner/.local/share/coevolution/python-env\") (subpath \"/Users/owner/.local/share/uv/python\")<RELAY_READ>)(allow file-write* (subpath \"$D\") (literal \"/dev/null\") (literal \"/dev/tty\") (regex #\"^/dev/fd/\"))(deny network*) 写入 \"$D/p.sb\"（$D 换成实际路径，<RELAY_READ> 平时为空；允许读路径元数据，严格路径解析要逐级 stat 上级目录）。要跑 Relay 测试时，在沙箱外为副本单独装依赖：N=$(dirname \"$(realpath \"$(command -v node)\")\")；mkdir \"$D/npm-cache\"，cp -R ~/.npm/_cacache \"$D/npm-cache/_cacache\"；在 \"$D/t/Relay\" 里 env -i PATH=$N:/usr/bin:/bin HOME=\"$D/.home\" npm ci --offline --ignore-scripts --no-audit --no-fund --cache \"$D/npm-cache\"，按候选 head 的 package-lock.json 逐包校验完整性、不联网、不跑安装脚本，不读不写共享安装；失败（缓存缺包、完整性不符）则 Relay 测试记为未验证；依赖安装脚本或原生构建的测试也记为未验证。<RELAY_READ> 换成 (subpath \"$(dirname \"$N\")\")，只放行这一个 Node 版本；不跑 Relay 测试时 <RELAY_READ> 为空、N 为空。在 $D/t 里用 env -i PATH=${N:+$N:}/Users/owner/.local/share/coevolution/python-env/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin HOME=\"$D/.home\" TMPDIR=\"$D/tmp\" sandbox-exec -f \"$D/p.sb\" <测试命令> 跑候选测试：不继承生产环境变量，文件只能写 $D，读不到 $D、Python 运行时（跑 Relay 测试时另加该 Node 版本）以外的用户目录、外置盘和其他临时目录，也不能联网；用完 rm -rf \"$D\"。然后按正确性与风险打 0–100 分，提交 record_pull_request_review {candidate_id, score, reason}；缺依赖、被沙箱挡住或跑到时限没跑完的测试在 reason 里写明未验证，不记为通过。不修改 PR。"},
   "delivery": {"mode": "none"}}
  ```

  其他 Agent 的任务把 `id`、`owner`、`--actor`、`workspace` 与提示里的 Agent 名换成该 Agent；两个运行时的
  `--default-minutes` 错开。

  缺任务的 Agent 不能静默挂起。每次拿到共用锁的 gate 运行（无论是否 skip）都在
  `.pr-review-dispatch.seen.json` 记下本 Agent 与运行时的最近运行时刻，并记该文件建立时刻。
  `harness-native pr-review-gate-audit [--grace-minutes 30]` 读最近的索引（同安装指纹、不足 30 分钟，
  不论哪个头）与该记录：有未结请求、但对应 gate 30 分钟内没运行过的 Agent 与运行时（从未运行过的，须该记录
  已建立满 30 分钟）列为缺失，输出 `{"schema": "pulse.pr_review_gate_audit/v1", "missing": [{actor, runtime,
  job: "<agent>-pr-review-<runtime>", work_items, last_gate_run}]}` 并退出 0；没有缺失输出
  `PR_REVIEW_GATE_AUDIT_SKIP` 并退出 3，失败退出 2。同一缺失 6 小时内只报一次（记在
  `.pr-review-gate-audit.json`）；索引过期、损坏或属他安装时结果为未知，不报也不清已报记录；已报记录只在有修复证据时清除：有效索引里已没有它的未结请求，或运行记录显示该 gate 在宽限期内运行过。运行记录缺失、损坏或重建后的预热期都不算证据。审计只读共用记录，不写账本、不占请求的重试次数；
  补登任务后该 gate 下一次运行即照常派发。平台方（Pulse）以它为 gate 建一个每 15 分钟的任务，批次交给
  Pulse 会话按上例补登缺失任务及其执行许可并热加载。

  审核会话只声明原 WI，ComputeCost 将其成本判为该 WI；声明多个 WI 的会话归属为 ambiguous，内核
  也拒绝其回执与评分。

## Current integration boundary

The isolated package implements local facts, graph/task rules, candidate/review/
rework, captured records, exact file delivery, disposable aggregate views, Linux
Pi admission, and a fixed-revision bundle. Tests of synthetic Host/proc/dispatch
fixtures are explicitly not real UAT evidence. Record actual native results
separately in the experiment report.

The full replacement still requires real adoption of collaboration contracts,
cross-store collaboration and responsibility transfer,
cross-host migration, original CLI/Relay/Hook/support consumers, production publication,
remaining notification and diagnostics/policies/usage consumers, and complete legacy import.
Intermediate control rounds, physical retirement and isolated Git publication now
have native journey evidence. Do not label any whole A-family or overall WI018
passed from those bounded journeys alone.

## Collaboration contracts

`offer_collaboration` takes `kind` (`request`, `delegate`, `responsibility`),
`recipients` (each has a `principal` and logical `resources`),
`purpose`, `required` and `main_only`. `main_only` is a required boolean:
`true` means only a main session (the resident conversation with the human)
handles the contract; choose it when the work uses the recipient's ongoing work
(unmerged changes, authorizations it holds, a WI it is advancing) or needs write
access. `false` means an unattended task may handle it; choose it only when
read-only work on what main already has completes the contract. Omitting it
returns `invalid_input` stating this rule. A contract offered before the
field existed reads as `false`. A main-only Agent recipient carries
`target_work_item_id`, the recipient's own WI it handles the contract on. When
it is omitted, the kernel reads the active WIs that Agent holds (readable, not a
child, not the offering WI) before deciding: exactly one becomes the recorded
`target_work_item_id`; none or several return `invalid_input` listing up to 10
candidates with their titles, or naming an exact session as the other address.
`reassign_collaboration` resolves its main-only Agent recipients the same way. A principal is either an Agent `{"actor"}` or one
exact session `{"actor", "session", "surface"}` returned by that participant's
Host entry. Requests and delegations may name an Agent on any surface. Any
session of that Agent may accept or reject; the first accepting session bears
the contract and receives the delegated scope, and any session of the Agent may
later return or complete it. An exact-session recipient answers only from that
session. A request grants no execution right; delegation grants only its
accepted scope and keeps the owner. Full responsibility has exactly one exact
successor session on the offering surface and changes owner only when that
session accepts. A draft transfer keeps the WI in draft.

`list` returns `awaiting_response` per WI: contracts the caller's Agent still has
to answer (`pending`) or settle (`accepted`), each with `main_only`, main-only
contracts first; WIs owing the caller a main-only contract are listed first.
For such a non-holder caller, `inspect`/`resume` return `next_action`
`respond_collaboration` with the `contract_id`; for a main-only contract it also
carries `main_only: true` and `agent_action` "主身合同：在与人类的会话中处理", and
takes precedence over settling answered contracts. A session entered by a Relay
scheduler run (`entry: scheduler`) cannot answer a main-only contract
(`authority_denied`). Notice routes reach only the local Relay's human channels, so a
recipient Agent discovers contracts by these reads.

`respond_collaboration` takes the returned `contract_id`, `decision`
(`accept`, `reject`, `return`, `complete`) and `reason`. Completion also captures
real `evidence` through file placeholders. Returning or withdrawing a delegated
scope revokes its live attempts; late original results remain history.
A pending offer stays answerable across the offering WI's own succession: while
the offering Agent holds the WI and every generation since the offer came from
its own Host rollover or `continue_here`, `accept` succeeds. A takeover, reopening,
responsibility transfer or any pass through another Agent, even one handed back, ends it
(`authority_changed`).
`withdraw_collaboration` takes contract_id/reason; `reassign_collaboration`
takes contract_id/recipients/reason and an optional `main_only` (default: the
original contract's value) and preserves both original contracts.
Required unresolved contracts block completion. Scope overlap cannot be
accepted while another live delegation or task owns the resource.

Exact request retry returns the original result even after the Host advances
its current generation. The fresh observation still reflects current rights;
retrieving the old result grants no permission to repeat the old work.

## Explicit control and read-only discovery

`list` supports `--status`, `--owner`, `--text`, `--after` and `--limit`.
It reads one transaction of authenticated facts. `inspect --view` can select
`history`, `plan`, `tasks`, `collaborations`, `candidates`, `records` or `effects`.
Omitting WI in a normal command is allowed only when the actual native caller
has exactly one current active/draft binding; otherwise specify the intended WI.
A parent and the children it holds through it are one binding, naming the
parent; two unrelated WIs are not.
Collections do not grant execution rights.

Human `stop` takes reason and revokes execution. `terminate_work` takes kind
(`cancelled`, `failed`, `superseded`) and reason; superseded also requires the
linked successor WI. These outcomes do not claim delivery or retirement.
Migration requires its separate source/destination protocol and is not accepted
as an unproven termination label.

Human `assume_control` takes reason and makes the authenticated current caller
the new responsible holder without relying on a former holder or disposable
registration. It preserves definition, adopted plan and candidate facts, revokes
old live grants/attempts, and advances generation. Terminal/stopped work needs
explicit `reopen_work` with reason and affected execution nodes (`[]` for work
without a graph). Reopen retains historical candidates and decisions; affected
nodes and their downstream dependents need new execution. A new candidate
cannot reuse an old approval. An unplanned standard draft remains a draft.

`release_scope` takes grant_id/reason and only allows its original recipient
to return the grant. Already-released scope stays released on retry.


## Intermediate graph control

Verification/review/delivery nodes may appear between work nodes and may have
multiple rounds. Each ready gate freezes its own candidate, scope, upstream
results and prior control facts. Intermediate approval releases only its
downstream dependencies. Unrelated ready work may proceed while an intermediate
candidate waits. Final candidates freeze all work and require all ACs.

A verification node may declare `acceptance_refs` as a nonempty subset of the
WI criteria. This limits only its intermediate check; it cannot narrow final
verification. Its `output_contract.artifacts` names required evidence outputs.
`record_verification` and `record_review` accept `outputs` mapping each declared
name to a real captured file placeholder. The fixed receipt and outputs feed
downstream input binding.

A delivery node with named artifacts uses `output` on each declared delivery
row to associate its captured artifact with that contract. Only actual successful
file readback satisfies the node. Intermediate delivery stays attached to its
original candidate, and its successful result remains available after later
candidates form. Rework clears only affected/downstream control results; old
receipts and publications remain immutable history.


## Managed Git workspaces

The operator policy may admit `repositories`: each row has `repo_id`, real
`source`, `common_dir`, `managed_root`, and `remote_url` (null for isolated local
fixtures). The isolated installer refuses any binding outside its installation
root. Business commands supply repository IDs and exact commits, never physical
paths or repository authority. Existing shared-source dirt remains untouched.

`allocate_workspaces` takes `workspaces`, a nonempty list of:
`{repo_id,base_commit,resources,sparse,purpose}`. `resources` is the current
logical execution scope; `sparse` is a list of literal relative paths, or `[]`
for the full checkout. One durable intention can bind multiple repositories.
The returned resource IDs and effect IDs remain stable on exact request retry.
`workspace_settlement.resources` reports the actual managed paths and each
original effect's observed state; a requested allocation is not a ready checkout.

`revise_workspace` takes `resource_id,resources,sparse,purpose`. It preserves the
original repository/path/branch identity and requires the old and new scope to
be owned and unoccupied. The previous operation must have settled. Dirty,
untracked and ignored files are preserved; a scope shrink cannot hide them.
A fresh command does not redirect an original physical binding. When the Host
moves a repository's managed root, earlier resources of the same repository keep
their original path and can still be revised, replaced and retired.

After business completion or explicit termination, first read the exact managed
inventory. A real human/controller may approve `retire_resources` with
`resource_ids` and `reason`. On an active WI its current holder may retire its
own workspaces with the same human decision for the exact inventory: each must
be ready (or already retiring), inside the holder's own scope, not occupied by a
running task, and without an unsettled effect acting on it or publishing a source
captured from it. Such a retirement is not withdrawn by a later handover, stop
or completion: it is the closing of a workspace, not new work, and it runs while
the WI is active, transferring, stopped, completed or terminated. It cannot be
cancelled; the scope stays owned until it succeeds, after which the same scope may
be allocated again. A failed retirement (for example `unique_commits_preserved`)
is final and settled: no drain reads it back or starts it again (a late
observation is kept only as evidence), and it is not a pending retirement: the
workspace it kept is in ordinary use again (tasks on its scope are claimed, its
write bindings open, its source is captured, it is revised), it still owns its
scope, `reopen_work` is not held by it, and only a new approved request retires
it.

Every workspace records the surface of the session that allocated it, and its
allocation, revision and retirement run only on that host: the inspected effect
carries its `host`, and an effect service or a session's own drain on another
host skips it. For a workspace recorded before surfaces were kept, only its
allocation proves the host: the surface of the journal record that committed the
allocation effect's successful `EffectObserved` event, provided every attempt of
that allocation was started (`EffectAttemptStarted`) from that same surface.
Committed events count, never command inputs: a revision request, a duplicate
success receipt (which commits nothing) or a success the kernel did not record
proves nothing. The kernel reads this from the WI's journal records in each
transaction, beside the replayed state (as it reads family facts), and a write's
response uses the same revision's records as a later read; it is not an
event-sourced fact. When that proof is missing (for example a retained historical
allocation, which has no allocation effect) or two surfaces disagree, the host is unknown: its effects run nowhere and a requested retirement
stays pending (never settled as already absent by another host); its scope stays
owned. So an active WI's holder cannot record a retirement or a revision of a
workspace whose host is not proven (`domain_conflict`): nothing could ever run it,
and the workspace would turn from usable to blocked. It stays in use as it is;
`replace_workspace` continues the work in a new workspace on this host. A
terminal WI's reclaim keeps its rule. Only an installation whose policy has no surface falls back to where the
bound source repository and Git directory exist. So a holder that moved to
another host releases an old workspace there; the old host's effect service
removes it under the protections below. Otherwise the effect service submits
`retire_terminal_resources` for a terminal WI with no unsettled effect or
delivery: the human decision that ended the WI covers its cleanup, and the
request covers every ready, unretired resource. An effect is unsettled while it
may still act: requested, started, unknown, or proven absent (absence may begin a
new round). Success, cancellation and a read-back failure are settled; a failure
acts again only on its owner's `retry_effect`, so it does not hold a terminal WI's
workspaces. The reclaim request ID names the pending resources and the effects
that still wait, so a deferral is asked again once those settle, while an
unchanged deferral or a refused retirement is not requested again. After the last workspace is gone the empty WI directory is removed. That command records obligations before touching Git.
The adapter removes only the exact original owned, unlocked, clean worktree and
its branch; it preserves unique commits, ignored files, original allocation
proof and journal evidence. This first slice requires HEAD equal to the original
base, or an exact captured commit with successful publication and every declared
consumer adoption. A changed-head retirement also rechecks the preserved source
bundle; newer commits, dirty files and missing original bytes remain protected.
A workspace already removed outside the core settles as retired without its
ownership marker only when its bound repository (same Git and remote identity)
has no registration and no branch for it and nothing is at its path, even if the
managed root is gone; anything left still requires the original proof. With that
proof, a worktree removed outside the core (no path, no registration) needs no
clean-checkout check: retirement removes the leftover branch when it holds only
the retire commit and otherwise fails with `unique_commits_preserved`.

Interrupted allocation/retirement is resumed under its original durable physical
marker and journal attempt, after current authorization is checked. Results are
accepted only after actual path, branch and registration readback. A pending
retirement prevents reopening execution. `resources_retired` describes only
resources recorded in this new journal; an empty inventory is not proof that
legacy or unregistered resources are zero. External unknown effects block
retirement and remain evidence. No reset, clean, force removal or force push is
used. Original source directories and retained evidence are not cleanup targets.


## Installed program admission

Candidate12 and later declare writer_contract=2. Installation waits for an already
admitted SQLite write transaction to settle, holds BEGIN IMMEDIATE, and asks the
proposed sealed program to replay the stable existing facts before switching.
The database then admits writes only from the exact installed program and policy
pair. Earlier candidate7–11 program-only admission is preserved as history.
A stale program can still inspect facts but reports unavailable write
capabilities and use_active_program; a legacy connection without the admission
function cannot append to originals, journal or the integrity footer.

The stable active entry resolves to its immutable release and re-execs before
importing lifecycle modules. This prevents an old program from claiming a new
bundle digest after the pointer moves. Independent-verifier instructions use the
stable installed entry, while their original candidate and assignment stay fixed.

The program pointer and SQLite admission are separate durable resources. If a
crash occurs after pointer replacement but before SQLite commit, the new program
cannot write yet; retry the exact original installation to converge them. A
crash after commit leaves the new program admitted, and retry reads the same
release. Do not invent a business recovery, alter WI authority, or restore a
SQLite backup to recover a program switch.

The first installation of this contract adds runtime metadata and write guards
to SQLite; whole-file database bytes therefore change, while journal rows,
original inputs, integrity footers and business revisions remain unchanged.
Compare those authoritative facts separately from installation metadata.
Compatible rollback releases must support the writer contract and replay all
current facts. Older packages remain preserved as evidence but cannot be
activated as writers by this installer. This is program-and-policy admission,
not a new WI generation, user authorization, or proof of deployment/UAT.

## Fixed Git source publication

After committing the intended result in a ready owned workspace, use
`capture_source` with `resource_id` and the actual exact `commit`. The native
Host checks the real Git identity, original allocation, clean status, HEAD and
ancestry, and captures the full Git bundle plus commit/tree metadata. Capturing
a bundle does not submit, verify, approve or publish a candidate.

The returned snapshot includes `snapshot_id` and its immutable artifact reference.
To put those original bytes into a task output, add envelope
`sources:{"code":"returned-source-id"}` and use `{file:"code"}` as the declared
output. The CLI retrieves the original captured bytes from this WI's journal;
it does not ask callers to manufacture checksums. A candidate freezes source
metadata only when that captured artifact is among its task results.

After independent verification and exact human review, `declare_source_publication`
takes `candidate_id,snapshot_id,target_id,expected_head,consumers`, where consumers
is a nonempty list of `{consumer_id,expected_head}`. Repository paths, remote URLs,
main ref and consumer bindings come from operator policy, never command input.
The isolated installer admits local lab remotes/consumers only. Policy entries
are `publication_targets:[{target_id,repo_id,remote_url,ref:"refs/heads/main"}]`,
`source_consumers:[{consumer_id,repo_id,path,common_dir,remote_url,branch:"main"}]`,
and a private `publication_root` under the lab.

The command records required publication and adoption effects atomically. The
adapter pushes the fixed captured commit without force, checks the exact remote
ref, and then fast-forwards each clean admitted consumer using the captured
bundle. It checks actual HEAD/tree/clean status; push success alone is not consumer
adoption. Source drift, consumer dirt, ignored files and conflicting commits are
preserved. Required effects block completion until observed successful.

An interrupted send keeps its original marker and attempt. Exact observed target
state may settle it; an old base after an ambiguous send remains unknown because
it may reflect an intentional rollback. Do not blindly resend. Read-only historic
observations may arrive after stop without starting a new send or restoring work.
Changed-head retirement requires every declared adoption plus the preserved
captured bundle; any newer unique commit or file remains protected. These local
Git tests and APIs do not claim remote production source convergence or runtime
adoption by an application process.

## Delivery after business completion

The completed WI's responsible owner may declare another exact fixed-artifact
delivery with `node_id:null`. It uses the original standard candidate approval
or lightweight accepted records. No reopen, new producer grant, new candidate
or repeated human review is required. Other actors, new artifact bytes and
re-execution of graph nodes are refused. Required and unsettled file deliveries
remain visible in `delivery_obligations` even when business_progress is completed;
resume advances them through the original external-effect protocol. Conflicting
destination files remain untouched, and outstanding effects block physical
retirement without rewriting the original business completion.


## Native dispatch crash recovery

Each new independent Pi child writes and fsyncs its own kernel PID/start-time/boot
proof before exec into the admitted Node/Pi process. The same process identity
survives exec even if the dispatching parent has already exited. A missing or
partial proof prevents model launch; an existing proof is never replaced. An
ambiguous original launch stays unknown and is read back without a second launch.
Native session evidence still does not substitute for ACK, verdict or human review.

An exact `capture_source` retry reads an already captured resource/commit and its
original bundle from the journal, including after physical retirement. This is
historical capture evidence, not a fresh live HEAD observation. A new request still
requires current workspace authority, and cannot restore retired execution rights.


## Replace a physical workspace without erasing its predecessor

`replace_workspace` takes `resource_id,snapshot_id,resources,sparse,purpose`.
Choose an original captured source snapshot from this resource, or explicit null
to start from its registered base. The latter does not copy later changes. Both
old and new logical scopes must be currently owned and unoccupied by running tasks.
The original resource operation must be settled and its repository still admitted.

One request records the successor allocation and predecessor link atomically.
Work waits until the new physical checkout is actually observed. Exact retries
reuse that request and resource; a missing old checkout is not repaired or needed
for the new allocation. Superseded resources cannot be revised or newly captured,
but original requests and evidence remain readable. All old bytes, branches and
registrations remain in the retirement inventory, including dirty/ignored files.
Replacement does not retire anything or claim source publication. This slice is
within the admitted repository; moving authority between fact stores remains a
separate source-sealing/target-activation obligation.


## Current native capability observations

The CLI reads the journal generation before producing inspect/resume or
post-command capability observations. It preserves the actual native principal
and checks the current holder and grants; viewing a generation grants nothing.
A new or revoked session remains unable to work; a session of the holder's
Agent may only append `checkpoint` and `record_work` within the holder's own
scope (AH-087). Read-only queries do not write
facts, advance generations, or repeat startup. This corrects the candidate7
shortcut-flow observation that reported generation-zero capabilities despite
a successful generation-one startup.

## Fact-store location admission

Location contract 2 registers persistent physical storage independently of OS
machine/boot IDs. The private .store-custody/database anchor is a hard link, not
another SQLite entry point. Do not open it with SQLite. Only the installer creates
custody. A copied installation, replaced file, changed path or missing anchor
stays read-only; existing custody cannot be re-enrolled. Old runtimes and installers
cannot write. Compatible rollback must support location_contract=2 and replay facts.

For one-time contract-1 conversion, the management operator calls
release.install(..., adopt_existing_store=True), or uses --adopt-existing-store.
This requires the retained original installation, verified predecessor, unchanged
database path, original binding and journal frontier. Every business row and
store/location ID is preserved. The option is not a business command or permission
to move/copy a store. Keep .store-custody and original releases; retry the exact
installation after interruption. No machine ID edit or platform retirement proof
is required. See [the custody contract](rewrite-migration-protocol.md).

## Sealed transfer between admitted stores

This section describes new source capability. Installation and native adoption
must be reported separately; candidate9 does not contain these commands.

The management operator first installs two independent stores in the same admitted
namespace and pairs their actual store IDs/location digests. Policy entries under
`migration_peers` contain `alias,store_id,location,key_file`. Copy the complete
actual `store.` ID with all 32 lowercase hexadecimal digits from the other store;
do not shorten it or reconstruct it from a display label. Format validation is
not proof of pairing: compare each peer's complete ID/location to the other
store's actual binding. The key is an existing
owned private regular file, never a business artifact. Pairing changes privileged
management configuration and is not a grant to a WI. The same policy fixes every
physical adapter; activation does not rewrite source paths to destination paths.

From the original native holder, `seal_migration` takes `destination` (peer alias),
`successor` (exact actual identity returned by the target native Host), and a bounded
`checkpoint`. It preserves the same WI and seals source writes. Retain the returned
`migration_id`. `inspect --wi WI --view migration_package` returns a private canonical
package with byte count/hash/base64 only to that original sealer. Decode it to a
private file, preserve it, and transfer it within the admitted pair. The destination
copy must be inside that actual native profile's existing artifact roots (for
example its own `work/`), using a real private directory and mode-0600 file.
Check original byte count and digest after transport; retain the source package.
A source-side path is not automatically admitted by the destination. If Host
artifact capture rejects that path before accepting a request, preserve the
failed command, correct only its unconsumed file path, and retry the same request
with identical package bytes and logical/version/access identity. Do not expand
policy scope or copy a database to bypass artifact capture. Do not publish
the package, its originals, native session identity or peer key in public evidence.

The named native successor executes the target installed bundle with an explicit
WI and `prepare_migration` input `migration_id,package`. Capture `package` through
the normal `files` envelope with `access:private`. The target need not already have
this WI. Preparation verifies the exact source seal and imports original history
atomically; it does not grant execution. Then `activate_migration` with the same
`migration_id` activates generation+1 exactly once. Each step has its own stable
request ID. Lost responses retry that ID; a second import does not replace history.

Read source and destination independently. Source stays migration_sealed. The
destination's original history, originals and uncertain attempts remain unchanged,
followed by preparation and activation. A copied prepared destination is read-only.
Fresh destination work must replace inherited workspaces through the product and
wait for actual physical readiness. Merely having the old resource path is not
writable authority. Never delete the predecessor to make replacement succeed.

Existing unsettled effects are not automatically drained. `adopt_migrated_effects`
takes `migration_id,effect_ids,reason` with a real human input reference approving
those exact obligations. The current persistent physical adapter bindings must match
the original physical endpoints, including after adoption. A different host needs
a supported bridge that preserves those endpoints; none is supplied by this slice.
Unknown remains unknown until the original request is observed. A new send requires
actual absence or the existing adapter's original idempotency contract. Source
workers never regain authority from their historical begin/continue receipt.


## Native measurement intervals

Candidate11 contains these source capabilities. Its first real capture exposed
a Pi 0.87.1 detail-field gap; the source now accepts that format. The
[measurement contract](rewrite-measurements-contract.md) separates observed
usage, human attention, independent quality and canonical Outcome facts.

Before bounded work, the actual native producer executes `open_usage_interval`
with `task_attempt` set to its real running attempt, or null for owner-level work.
The Host records the current complete byte boundary of that session; the caller
cannot supply a historical anchor. Keep the returned `interval_id` and start byte.
After work, `usage-range --start-byte START` reads the same actual session's bounded
complete range without any fact mutation or external effects. Then `record_usage`
takes `interval_id,task_attempt,start_byte,end_byte`; the Host captures and extracts
the original bytes privately. The envelope contains no caller counters or paths.
For large intervals, retain distinct nonoverlapping complete chunks; unknown or
malformed source counters fail capture instead of becoming zero.

Task results/abandonment and owner stop, completion, transfer or sealing close
relevant intervals. Only the same native principal can observe its ending cursor;
other or missing native observations produce a closed unavailable boundary, not
an invented byte or an unbounded continuation. A truncated source refuses the
transition rather than recording an impossible interval. Existing usage receipts
remain immutable. Concurrent overlap for the same native principal is rejected
across WIs in the same namespace/store transaction; separate stores do not claim
a global billing deduplication service.

`inspect --wi WI --view measurements` reports exact intervals, missing coverage,
opaque session/configuration references and original evidence digests. Raw native
messages remain private. Missing token fields stay null; cost totals require an
explicit original currency and are never combined across currencies. A recorded
interval is not whole-WI lifetime usage or a replacement consumer adoption proof.
For an exact private usage reference, the existing `--view artifact --artifact-ref`
query also projects the saved interval-opening Host source as `native_usage_sources`.
A verified original header permits a consumer to identify the old range after the
old process/file disappears; missing or mismatched sources remain unavailable.
This is still a private read, not new access, execution or attribution authority;
see the [saved-source contract](rewrite-measurements-contract.md#private-saved-source-projection).
The same authenticated measurements query includes original decision times in
`result.outcome_record_times`; see [original outcome record times](rewrite-measurements-contract.md#original-outcome-record-times).
This is an observation-only extension, not an additional verification or human decision entry.

`record_attention` takes `task_attempt,category,duration_seconds` and a real
human input bound to that exact action. Duration is explicit self-report or null,
never inferred dwell time. `record_quality` takes `candidate_id,rubric,version,
score,evidence` from the acknowledged independent verifier. Score is null or an
explicit 0..1 value; evidence is captured through the normal files envelope. This
does not alter its verification verdict or create a second completion authority.
Measurement commands do not drain pending external effects or grant execution.

Journey8 recovered the original fixed native range on candidate12 and proved
exact retry without another revision. Existing cost/attention/quality consumer
compatibility remains required adoption work. That bounded capture does not
prove whole-WI coverage or consumer replacement.


## Exact program and policy updates

The [policy admission contract](rewrite-policy-admission.md) binds an immutable
program and canonical management policy as one installed writer identity. The
release directory includes both digests; updating configuration uses the same
installer with a revised policy, even if the exact program bytes are unchanged.
Never overwrite a retained release's policy or reuse a key-file name for rotation.
The native `identity` response reports program hash, policy hash and admission.

Existing installation identity and physical endpoints remain fixed. Migration
is required to change their store, namespace, delivery/dispatch or identity roots.
Each compatible installation replays existing facts and switches admission under
the database lock. The active link can switch before SQLite commits; interrupted
installs recover by retrying the same exact pair, without any business command.
Writer contract 3 raises the prior contract-2 floor in the activation transaction
and rejects older installers at the SQL boundary, including programs unaware of
the current audit/manual policy. It does not change location contract 2.
Old policies can inspect history, but cannot write or start another external I/O.
An already-started effect keeps its original attempt and physical destination;
the successor reads it back rather than assuming absence or sending it again.

The real candidate11 capture kept the original interval and fixed range but was
rejected on Pi's `reasoning` field before any usage receipt. Source now records
optional `reasoning`/`cacheWrite1h` detail fields without adding them again to
totals. Detail-bearing ranges use extraction v2. Previously accepted v1 ranges
retain their exact packet and retry identity. Reuse the original rejected command
and fixed range after the repaired release is installed; do not resample the end
or submit caller-produced counters. Cost without native currency stays unavailable.

Source tests and management installation are distinct from original-Pi adoption.
Contract 2 installation and the original fixed-range retry have separate journey8
readbacks. Full policy selection and existing consumer replacement remain open.

## Runtime identity changes with retained storage

Muse journey9 exposed contract 1 mistaking a runtime identity change for a foreign
database. Muse and Relay remained usable. Upgrade both original stores through
the explicit conversion above, preserving failed correction and business evidence.
Do not edit store_binding, manufacture a seal or replace the WI. After independent
installation readback, verify the original native principal/history and continue
pairing/sealing through the actual business entry.


## Waiting and activity inspection

On a release containing the [watch contract](rewrite-watch-contract.md), use
`inspect --wi WI --view watch --limit 100 --age-threshold-seconds 3600`.
The threshold is optional advisory age, not an execution timeout. Follow
`next_offset` with `--after` for another stable page. The response lists current
waiting tasks, recipient responses, verification/review, transfer and unsettled
effects without returning the full private dispatch or historical candidate data.
Its `effects` list covers only that watch page, labelled `effects_scope:watch_page`.

`presence:unknown` means the query has no trusted current process observation;
the last authenticated journal entry cannot prove an executor dead or alive.
`overdue:unknown` includes an absent original business deadline or unavailable
observation clock. A stale age
does not revoke a grant, finish a task or authorize resending an unknown effect.
Archive candidates require terminal facts and recorded resource retirement but
never authorize deletion. An actual resource or process-occupancy gap remains
open even if business delivery is complete. This is an authenticated read only;
native adoption of a new release requires original-session readback separately.


Confirmed graph nodes, `offer_collaboration` and `rollover` may include an
optional `deadline` such as `2026-10-08T18:00:00+08:00`. It is an explicit original
business date-time, not a timeout granting the host permission to end work.
Unknown offset `-00:00`, naive times and relative durations are rejected.
Reassignment preserves a contract's deadline. Watch reports its original source
and `overdue` or `not_due` independently of the advisory age threshold; unknown
clock evidence remains unknown. Existing facts without deadlines remain valid.

Use `inspect --wi WI --view watch --revision N` to read an exact historical
revision; the same option supports historical state. It never writes an old
projection over the current view, and its returned capabilities/next action
still use current authority. Other actions reject `--revision` instead of
silently ignoring it. Historical reads cannot authorize old work.


## Read-only collection scan

Use `inspect --view scan --limit 100 --age-threshold-seconds 3600` for a
current summary of readable WIs in the authenticated namespace. Optional
`--status`, `--owner` and `--text` filters match the normal list contract.
Do not supply `--wi` or `--revision`; no implicit bound WI is selected.
Read the returned next offset until null when a full collection is needed.
Each page uses a fresh read transaction and one observation time, so a changing
collection is not a global snapshot across calls. Retain each row's source
revision and use single-WI watch for its bounded detail pages.

Counts include all waiting observations before detail pagination. Record age
is advisory, deadlines require original facts, runtime presence stays unknown
and an archive candidate grants no deletion authority. This query does not
install a scheduled task, probe the platform or send notifications.


## Recorded check evidence

Capture an original `harness.check-evidence/v1` JSON artifact through the normal
work-result path, preserving its actual source and access scope. See the
[checks contract](rewrite-checks-contract.md) for the four fixed evidence forms.
An evidence packet is a recorded claim; inferred observations do not become
authenticated human decisions. No checked command is executed by this query.

Save the artifact reference returned in the WI's original result to a small
regular JSON file and use
`inspect --wi WI --view checks --artifact-ref reference.json --limit 100`.
This is the existing immutable reference (object/version/sha256/access/size),
not an arbitrary path or an authority context. A historical query may use
`--revision N` only after the artifact was captured in that history.
The same reference with `--view artifact` retrieves original bytes under the
same WI/private access policy. Do not publish private artifact output.

The compact report gives passed/violated/unknown, whole-report counts and
JSON pointers into original evidence. Read next_offset pages when needed.
Unknown rules, missing/inferred observations, ambiguous order, invalid regexes
and budget exhaustion cannot pass. Temporal checks require an earlier
confirmation for the same object/version. Reports neither change WI state nor
authorize actions; native pre-action probes/enforcement remain unimplemented.


## Policy review before installation

Use the release module's management `policy-inspect --root ROOT --policy FILE`
or `policy-diff --root ROOT --policy PROPOSED --against CURRENT`. Both run the
same bounded strict policy and isolated physical preflight as install, without
creating directories or changing the active release. Reports contain exact
section digests/counts and identity fields that require migration; they neither
echo private paths/key references nor approve a permission change. Review the
original private files when deciding the actual policy change. Installation
re-reads and validates the proposed bytes, retaining existing admission/custody.

An actual native caller can run `inspect --view policy` to see the adjacent
installed policy's digest and compact section/role summary, without a WI. Other
WI, revision, pagination, filter, artifact or input flags are rejected. This
view does not perform a management filesystem preflight or touch the business
database. It cannot select another policy, change permissions or dispatch work.
See the [policy review contract](rewrite-policy-preview.md) and
[execution policy contract](rewrite-execution-policy.md). The optional execution
object is exactly mode (manual, audit or automatic) and batch_budget (integer
1..64). Absence defaults to manual/8. Install mode changes through a new immutable
program/policy pair; do not edit active files. Business arguments cannot override
the installed setting. The response reports dispatch_policy; audit masks write
capabilities while retaining original grants and pending effects.

Batch budget bounds driver outcomes, not total job cost or completion.
Ordinary current-state reads may rebuild disposable Views. To compare exact
view bytes without materialization, query an explicit positive --revision.
An audit policy also disables view publication; neither behavior alters facts.

## Independent verifier computation

Optional verification_compute is a complete object with provider, model, thinking
and tools. Review and install the exact policy change through policy-diff/install;
business inputs cannot override it. The absent default remains openai-codex /
gpt-6.1-sol / high with read,bash,edit,write. An explicit example is:
{"provider":"openai-codex","model":"gpt-6.1-sol","thinking":"medium","tools":["read","bash"]}.
This example is a proposal, not confirmation that the installed catalog has that
exact model or that Muse adopted it. No tools are silently added. Without bash,
native identity/ACK/result submission cannot run and dispatch fails before launch.

New assignments use the fixed, manifest-verified SDK runner and the same admitted
Pi installation. One runtime loads the selected provider's local model catalog
without network catalog refresh, resolves exact identifiers and checks the
constructed model, thinking and tool set before any prompt. A missing model,
invalid catalog, clamped thinking or added/removed tool reports a pre-model
failure in runner-result.json, not a successful verification. No fuzzy CLI
fallback runs. selection.json and the native custom session entry retain the
original selection and runner/config hashes; spawn.json retains the child-owned
kernel process stamp. Historical CLI dispatch evidence remains readable.
The stamp is {pid, start_ticks, boot} from procfs on Linux and {pid, started,
boot} from `ps lstart` and kern.bootsessionuuid on macOS. The wrapper, and the
recovery's no-spawn fence, write spawn.json through a fsynced temporary file
hard-linked into place, so it appears whole or not at all and the wrapper execs
only after it exists. An empty spawn.json can come only from an older wrapper
that failed between its O_EXCL create and its write. Without procfs (macOS) that
wrapper's stamp always failed before the write, so it could never exec: with no
session file the launch reads back as failure native_launcher_failed_before_exec
with model_invoked false, and recovery treats it as never started. With procfs a
paused older wrapper could still write and exec, so there it stays unknown.

Management must wait for a stable native frontier before changing the policy.
Retain the original launch files/unknown effects on interruption. An observed
session means only that the assigned native process produced evidence; actual
ACK, criterion checks, verdict and human review remain separate domain actions.
Do not replace missing responses with a fabricated success. Source tests and a
stubbed prompt probe are not installed/native adoption. This setting does not
alter producer conversation preferences or enforce an OS filesystem sandbox.
Broader path confinement, elapsed/token budgets and full tenant migration remain open.


The SDK runner also initializes the admitted Pi distribution's own HTTP dispatcher
before provider calls. It reads only proxy/idle-timeout fields from the existing
bounded operator settings file, preserves environment proxy precedence and normal
certificate verification, and never rewrites settings or credentials. The exact
module path is admitted with the SDK; transport.json records module/config/settings
digests and initialization booleans without URLs or secret values. Invalid
transport/settings fail before prompt. A resolved prompt with an assistant
error/abort records model_interrupted with a nonzero exit; prompt_completed never
substitutes for ACK or verdict. Retain the failed assignment; a later business
revision requires the original WI's explicit human decision, not dispatch replay.



## Installed notice routes and single-attempt delivery

The kernel records original notices, exact immediate/daily batches and independent
resolution through record_notice, flush_notices and resolve_notice. The read-only
notices view supports open, resolved, failed, unresolved_send and backlog filters,
bounded pagination and historical revisions. Notices do not approve candidates
or complete work. A frozen candidate can request its actual pending human review.

Use inspect --wi WI --view notices --status open --after 0 --limit 20.
The same flags accept failed, unresolved_send, resolved and backlog; add
--revision N for a fixed historical query. Empty filtered pages stay empty.
Operational record_notice uses purpose=operational and attention=null.
An explicit_decision uses attention={reason:the exact current decision request}
and the authenticated original human_input_ref. It cannot reuse an old approval.

The optional notification_transport and notification_routes policy sections are
installed together. Omission leaves notification routes unavailable. The complete
transport object is:
{"endpoint":"http://127.0.0.1:PORT/internal/notice",
 "receipts_root":"ABSOLUTE_PATH_INSIDE_THIS_INSTALLATION",
 "token_env":"RELAY_INTERNAL_TOKEN"}.
PORT is the exact approved listener port, not a value selected by a business
command. Only this loopback URL form is accepted. The fixed token environment
reference reuses existing managed authentication; do not copy credentials into
the policy. Changing an installed transport requires the migration boundary.

Each route includes exactly route_id, channel (telegram/feishu), agent_id,
recipient (existing Telegram numeric chat ID or Feishu open ID), mode
(immediate/daily), timezone, audience (operational/owner_attention), max_items
(1..64), and max_bytes (128..4096). Actual identifiers stay in the private
operator policy. A trusted native Host exposes only immutable route semantics
and recipient/transport digests. Audit and verifier profiles cannot send.
Review the exact sections through policy-inspect/policy-diff, then use formal
isolated install. Never hand-edit an active policy.

A business record_notice selects route_id and supplies resources, text, purpose
and attention; flush_notices selects route_id and the original YYYY-MM-DD window.
resolve_notice selects notice_id and reason. Owner attention requires the actual
current candidate_review or an exact authenticated explicit_decision input.
The captured batch carries its original structured authority fingerprint;
current authority is checked immediately before dispatch.

manual mode records the intention; resume/drain sends only the installed batch
budget. automatic mode uses the same native driver. /internal/notice invokes
each channel once; ordinary /internal/push is not a fallback. An exclusive durable
request marker prevents concurrent sends, and the exact returned channel message
ID/content/recipient binding is retained for process-recreation readback. Losing
the response or marker leaves unknown. Only a retained recognized pre-send
rejection proves absence and permits another attempt. HTTP 200 alone never
settles delivery. Do not delete receipt files to retry or resolve uncertainty.

For an isolated experiment, startNoticeOnlyServer from the exact Relay source
exposes only POST /internal/notice on 127.0.0.1 and pins an operator-supplied
agent/channel/recipient set. Reuse the existing SDK client and network agent,
with credentials read in place; no polling, WS, pairing, reload or production
restart is needed. Startup is not a message send. Read back source/port/bindings
before installing the matching native policy. An older Relay or unconfigured
channel remains unavailable; do not widen configuration to hide that gap.

Preserve the existing process's complete admitted network bootstrap, including
any operator NODE_OPTIONS preload used to configure its HTTPS agent. Merely
seeing proxy environment variables does not prove SDK network adoption.
A bounded read-only channel identity request can verify this binding before
business delivery. It never turns an earlier ambiguous send into absence.
An unresolved optional notice is retained for idle readback while independent
authorized effects can use the next bounded batch; no implicit resend occurs.

Source tests cover real loopback HTTP and actual installed SDKs against a local
ambiguous-result server. They do not prove installed Muse adoption or original
Telegram/Feishu messages. Those native observations remain required for
AH-061/A-29; see the [notice contract](rewrite-notifications-contract.md).


### Current native process observation

On a release containing the trusted runtime-observation slice, watch and scan
include current-process observations supplied by NativeHost. They match the
complete admitted Pi principal, with an observation timestamp and opaque proof;
historical queries label this evidence as current observation, not past liveness.
Only the exact admitted process is read. Missing metadata, PID/boot changes,
zombie state or missing admission evidence remain unknown and do not revoke
authority. Other executors and unsupported runtimes remain unknown. Public output
contains no PID, raw boot/session identity or private process path.

This needs no additional command argument or route/policy setting. Business
inputs cannot select a PID or manufacture presence. Querying preserves original
journal and effect facts; it does not install a timer, restart a process or resolve
a notice. Until a candidate containing this slice is formally installed and the
original native caller has adopted it, these are source semantics only.


## Relay release changes

If an existing native policy pins a previous Muse Relay CLI, an operator must
adopt the already activated exact successor through release policy-diff/install
with `--adopt-relay-release`. The formal CD selection, complete release inventory,
live process and unchanged identity/permission fields are required. Keep the
original namespace, store and session. Do not edit immutable policies, redirect
old release paths, construct a native context or create a replacement store.
After install, compare the original principal/role separately from the expected
new program/policy admission and perform actual native commands again.

## Relay daily command contract

A reviewed Pi profile may select `workItemCoreHost` (canonical Python, installed
bundle, command extension). This single operator selection routes Telegram and
Feishu `/wi` ingress into the existing native Pi session. It must bypass the old
WI controller and retry queue. A selected but unavailable native entry fails
closed, retaining the original command and session; it never invokes compatibility
lifecycle writers or creates a replacement conversation.

The Pi extension waits for the real user message to be persisted, then invokes
`channel --human-input-ref REF`. The Host validates the original channel receipt,
tenant and latest native user message. The command parser reads the receipt's
original text; neither the model nor business arguments choose identity or rewrite
the human decision. Re-delivery uses `channel.REF` as the original request ID.
The extension gives the machine result to the Agent for explanation, including
rejection, pending/unknown effects and current execution capability.

Daily vocabulary: `/wi` or `/wi help`, `/wi list`, `/wi status [WI]`,
`/wi continue [WI]`, `/wi watch [WI]`, `/wi checkpoint [WI] SUMMARY`,
`/wi cancel [WI] REASON`, `/wi reinit [WI] REASON` and `/wi switch WI`.
The existing shorthand `/wi WI` means `/wi continue WI`.
Continue resumes bounded effects under the existing native identity; it never
assumes another Agent's grant. When the same Agent holds the WI from another
session (`local_work` reason `held_by_same_agent_in_another_session`),
`/wi continue WI` submits `continue_here` with that slash input as the human
decision: the holder becomes this session, the generation advances, the
previous session's attempts stop counting and its own scopes follow it. Reinit is the explicit human assume-control
decision, not a generic retry. Omitted WI requires exactly one native binding.
Checkpoint scopes come from current authenticated capabilities, not caller paths.
Missing/ambiguous business arguments are rejected before mutation.

Other lifecycle operations use the same native `execute` interface described
above through the installed lifecycle skill, preserving full business inputs and
real artifacts. Relay `/wi rollover [WI] [SUMMARY]` is a Host session operation:
its original authenticated input is retained, `prepare_rollover` fences the
source, and exactly one actual native successor is allocated. Host-attested
`accept_transfer` binds that successor to the original transfer; callers cannot
invent a successor principal or provide another process ID. The successor's
native input record proves bootstrap receipt, independently of usage collection.
Unknown results, failed spawn, restart and redelivery observe and resume the same
original request and successor. Private Host custody stores identities and
original inputs only; committed status and execution authority come from the
core journal. Explicit `rollover(target)` remains available when both real native
sessions already exist. Neither path creates a replacement WI.

An Agent may also roll its own WI over without a human command, because
execution stays with the same Agent. The holder session executes
`request_rollover {summary}` (`harness_rewrite/self_rollover.py`); the kernel
accepts it only when the WI's latest record is a checkpoint by this session and
no effect is `attempt_started` or `unknown`. The decision writes no event, so
authority does not move yet. It is refused for another Agent, a non-holder
session (also another session of the same Agent), a child WI, and a session
outside a Relay conversation. After commit the native Host leaves a private
notice `native-session-handovers/agent-requests/<ref>.json`, with
`ref = digest(["agent_rollover", namespace, wi, request_id])`. Relay starts the
same Host handover with that ref. `prepare` finds no human input for it, reads
the notice, requires the same attested source process, tenant and native
session, reads the committed request back from the journal, and executes
`prepare_rollover` with `session_handover {source, agent_request}`; the kernel
checks readiness again before it fences the source. `accept_transfer` is
unchanged: only the Host-attested successor of the same Agent and surface binds.

`@anchor test test_holder_with_its_checkpoint_last_may_request_and_nothing_moves`
`@anchor test test_request_is_refused_without_a_latest_own_checkpoint`
`@anchor test test_another_agent_or_a_non_holder_session_cannot_request`
`@anchor test test_doubtful_effects_block_both_the_request_and_the_fence`
`@anchor test test_host_fence_of_an_agent_request_rechecks_readiness_and_keeps_the_agent`
`@anchor test test_agent_request_is_fenced_and_bound_to_a_successor_of_the_same_agent`
`@anchor test test_agent_request_notice_must_name_the_attested_source_and_a_committed_request`

Relay `/wi switch WI` moves one conversation between native sessions, one WI
each, in two `relay-channel` phases with the same original input ref. `leave`
runs in the current session: when the session holds no WI or already holds the
target it records nothing; when the target is a child of the WI it holds, it
answers `no_switch` ("执行权已在本会话，无需切换") and records nothing;
otherwise it records the automatic checkpoint `channel.<ref>.leave` on the held
WI. `enter` runs in the target's original or new session: a draft the Agent owns
starts (`start_work`), work the same Agent holds in another session continues
here (`continue_here`), and work already held here is only read. Both phases
read the WI once, drain no effects and answer only `status`, `request_id`,
`error`, `next_action` and `work_item {id, generation, local_revision,
business_progress}`; a repeated phase with the same ref returns the committed
original or the current read. The left WI stays held by the session it left.
A third phase, `readback`, runs in any session of the conversation and answers
`{phase: "readback", wi, committed}`: whether enter's request `channel.<ref>`
committed on the target. Relay uses it after a restart or an unknown enter to
settle on the recorded target session or return; the committed request binds
the session that entered, and another session repeating enter with the same ref
gets `request_conflict`.

An Agent switches its own conversation without a human command through
`request_switch {target, summary}` (`harness_rewrite/self_switch.py`) on the WI
it holds. Readiness is `request_rollover`'s: holder session, its own checkpoint
as the latest record, no `attempt_started` or `unknown` effect, a Relay
conversation. A target that is this WI, its child or already held here commits
`no_switch` with the reason and no request. Otherwise the kernel reads the
target in the same transaction and accepts only a root WI of the same Agent
that is active and held by another of its sessions, or its own draft; another
Agent's work, an unheld, stopped or ended WI, a child of another WI and a
missing WI are refused. The decision writes no event. After commit the native
Host leaves `native-session-handovers/agent-switches/<ref>.json`
(`harness_rewrite/agent_switch.py`), with
`ref = digest(["agent_switch", namespace, wi, request_id])`, naming the WI,
target, request, tenant, source principal and source native session. Relay runs
the same two phases with that ref. `relay-channel` finds no human input for it
and checks the notice: tenant and Agent of this conversation, and for `leave`
the source session itself. Both phases read the committed request back. `leave`
records no further checkpoint, since the Agent's own is the latest record.
`enter` executes `start_work`, or `continue_here` with Host-attested
`session_switch {source, source_wi, request_id, target}` in place of a human
decision; `continue_here` accepts it only for the named target and the same
Agent.

`@anchor test test_holder_may_request_a_switch_and_nothing_moves`
`@anchor test test_request_needs_the_own_checkpoint_last_the_holder_and_the_same_agent`
`@anchor test test_this_wi_or_its_child_needs_no_switch_and_no_checkpoint`
`@anchor test test_targets_that_cannot_take_the_conversation_are_refused`
`@anchor test test_continue_here_accepts_the_host_attested_switch_of_the_same_agent_only`
`@anchor test test_human_switch_to_a_child_answers_before_leaving`
`@anchor test test_human_leave_answers_with_the_bounded_facts_only`
`@anchor test test_agent_request_leaves_its_own_session_and_enters_a_draft_it_owns`
`@anchor test test_agent_request_enters_work_held_by_another_session_of_the_same_agent`
`@anchor test test_agent_switch_notice_needs_a_committed_request_of_this_conversation`
`@anchor test test_request_switch_outside_a_relay_conversation_is_refused`
`@anchor test test_human_switch_enter_reads_back_by_its_original_reference`
`@anchor test test_recovery_reads_a_committed_enter_back_and_reenters_only_as_the_same_target`
`@anchor test test_recovery_reads_an_uncommitted_enter_back_as_absent`

Acceptance requires: exact ingress retained; both channel adapters bypass old
writers when selected; extension runs only the latest authenticated slash input
after persistence; duplicate event commits once; wrong tenant/ref, malformed
command and missing binding commit nothing; native command results arrive in the
original Muse Telegram conversation; the authoritative Coevolution skill and
actual Muse-loaded skill agree. Mock/fixture tests do not replace that readback.


## Original legacy drafts

The operator may install a formal namespace separately from experimental stores.
An optional operator-owned `legacy_catalog` makes native list/inspect/resume disclose
formal source records separately from executable native facts. A matching source
record returns `legacy_not_imported`, not a claim that the WI does not exist.
The catalog is a dated Git snapshot, never execution authority or live global
absence proof.

This section applies only to explicitly requested cross-store migration in isolated mode. Production mode rejects these intents: it uses the original ledger directly.

Only unstarted standard drafts are supported for this cross-store admission. Keep the
original WI id, owner, dates, criteria, source and exact original files. The
operator first prepares the exact old-source tool with
`install-runtime-release.py prepare-operator`; this installs code beside the
stable runtime without changing its pointer, policy or lineage. Using its
`bin/harness` and the registered original ledger:

1. `legacy-migration source` reads the actual source binding;
   `legacy-migration inventory --output FILE` captures discovery.
2. `migrate-draft WI --migration-target FILE --expected-definition SHA`
   seals the original draft through canonical publication. The target file
   contains the actual destination store_id/location/namespace and source_alias.
   The source projection uses the old readable `migrated` disposition with
   the same id at the destination; the immutable original draft is retained.
   Database guards also reject writes from installed older binaries.
3. After publication/replication, run `legacy-migration adopt WI` at each
   original resident writer. `legacy-migration check WI` proves local sealing.
   `legacy-migration export WI --transaction REF --peer FILE --output FILE`
   requires completed canonical publication, exact replication coverage and
   fresh matching readbacks from every writer before authenticating the package.
   Failed publication resumes the original Broker transaction.
4. Place that unchanged private package inside the real recipient's admitted
   artifact root. The operator may add its exact path and SHA-256 under
   catalog source.operator_prepared_transfers, keyed by WI id. This discovery
   hint is not source authority; native preparation still authenticates the
   unchanged package and destination. In the original native Pi session execute:
   `{"request_id":"legacy.prepare.WI","intent":"prepare_legacy_draft","work_item_id":"WI","input":{"package":{"file":"original"}},"files":{"original":{"path":"ABSOLUTE_PACKAGE","logical":"legacy/WI","version":"v1","access":"private"}}}`
5. Read back the returned migration_id and execute
   `{"request_id":"legacy.activate.WI","intent":"activate_legacy_draft","work_item_id":"WI","input":{"migration_id":"RETURNED_ID"}}`
   in the same native recipient. Read the resulting original definition before
   aligning requirements. Neither preparation nor activation starts execution.

The source and receiver share a separately installed operator peer key; do not
put it in business inputs, logs or Telegram. No manual database/YAML edits,
recreated WI or fabricated Host identity can replace these steps. Unsupported
formats remain visible in discovery and require a separate converter.


### Original request recovery and installed entry

After an unavailable result, `pulse_work_item` returns the original `request_id`
and an `inspect` action with `view=request`. This query reads committed/absent
from the same current journal; it creates no request cache or lifecycle decision.
If absent, repeat the exact execute input with that request ID. If committed,
consume its original result and resume the same WI. CLI equivalent:
`inspect --wi WI --view request --request-id REQUEST`.
Relay preserves the installer-owned `active/harness.pyz` pointer for an existing
Pi session. Each call still validates the actual active bundle and policy;
upgrading the core does not freeze the session to a retired release.

A reviewed Relay adoption may converge a stale producer Node path only to the
unchanged canonical `node` already admitted in the installed policy. It must
also match the actual managed Relay process executable. This does not select a
new interpreter, change business identity, or bypass the live release proof.


### Repository identity and authentication

The native policy view exposes admitted repository IDs, source/workspace paths and
credential-free remote identities. Agents use this view instead of reading the
private host-policy file. HTTP authentication is not part of repository ownership
or a WorkItem request. Original records and physical receipts remain immutable;
credential rotation preserves the same repository identity, but changing the
repository path or host remains a conflict.

The optional operator-owned git_config_file supplies Git credential providers
to workspace/publication adapters. It is a private absolute file, never copied
into business facts. Existing credential-bearing facts are replayed in place and
redacted at the native response boundary. Previously disclosed credentials still
require revocation; redaction does not repair their secrecy.


## Capture results from a continued workspace

Native file capture derives additional roots from the current production journal,
then validates the exact physical Git allocation and current principal's intent
and resource scope. Historical continuation therefore grants result capture from
the original workspace without editing static profile roots or re-registering it.
Other WIs, replaced branches, revoked principals and symlink escapes remain denied.
Git source capture preserves ignored build outputs and private evidence. It still
requires an exact HEAD with no tracked or untracked non-ignored changes. Resource
retirement retains its stronger check including ignored files; successful source
capture never authorizes their deletion or production configuration maintenance.


When the admitted repository has a published main observation, source capture
binds its exact common ancestor as a bundle prerequisite and retains only the
unpublished delta. Publication fetches prerequisite history from its registered
target and verifies reachability before importing the immutable bundle. Existing
full bundles remain valid; a later source capture cannot replace an already
recorded snapshot. Consumers fetch the exact captured ref from that verified
transport, preserving source history and normal compare-and-swap publication.

### 原效果的重试预算

`resume` 保留原 effect 和每次尝试；未知结果只读回，不再次发送。两次已证明未发生的发送后
按原尝试时间退避，单轮最多八次发送。`effect_retry_backoff` 返回最早重试时间；
`effect_retry_exhausted` 要求处理实际交付故障。负责该 WI 的当前所有者可用
`retry_effect {effect_id, reason}` 记录修复并开始下一轮预算；原尝试和未结义务仍保留。
只有 `observed_absence` 可开始新轮，未知、成功或取消的效果均不可据此重发。
该维护动作不是新的业务批准、执行授予或自动核销失败。
`cancel_effect {effect_id, reason}` 需人类批准，只结清非必需、仍为 `requested` 或
`observed_absence`（读回证明未发生）的效果；进行中的 WI 由当前持有者提出，已
completed/terminated 的 WI 由当前所有者提出。未知结果和 `workspace_retire` 不可取消。

分配与修订意图在 WI 结束、执行代际改变（子 WI 随父 WI 换代）或资源被替换后成为过时意图
（`inspect` 的效果带 `obsolete: true`），不再发送也不再续做。退休意图只要资源仍指向它、WI
仍为 active、transferring、stopped、completed 或 terminated 就不过时：持有者交接、stop 或 WI 结束都不撤回它。过时的分配或修订若从未
开始过尝试，worker 的 `begin_effect` 把它结清为 `cancelled`（原因
`obsolete_resource_intention`）。已有尝试时，`begin_effect`/`continue_effect` 以
`obsolete_resource_intention` 拒绝，worker 改为 fenced 探查并 `observe_effect`：

- 每次尝试在整个物理动作期间持有该 managed root 的写锁；探查只做非阻塞加锁，锁被
  占用即记为 unknown（`original_writer_may_be_running`），不结清。
- 持锁时，修订缺少动作标记（修订在改动前只做所有权与干净检查，随后先 fsync 动作标记），
  或分配缺少资源标记且无 worktree、分支、登记痕迹（分配在 `worktree add` 前先写资源
  标记），即证明原尝试未动作；探查随即在锁内写入 `<effect_id>.withdrawn.json`，再报告
  absence，内核随该观测结清。之后任何尝试在同一把锁内先检查该标记，存在即不动作，
  所以这个 absence 不会被迟到的写入者推翻。
- 有标记时按物理状态记为成功或未知，不结清、不续做。结果不变时同一观测请求是重放，
  不写入。`workspace_retire` 过时也只读回，不结清。

结清过时的分配或修订时同时记录 `WorkspaceRequestWithdrawn`：修订把资源恢复为发出该
修订前最后确认的记录，当前代次可再修订、替换或回收它；分配移除从未创建的资源，被其
替换的前任恢复为现行资源。所有者经人类批准的 `cancel_effect` 只在效果从未开始尝试
时撤回资源记录，否则资源仍指向该效果。已结清的资源效果收到迟到观测时不改状态，只记入
`late_observations`；结果不是 absence 时效果带 `attention: late_result_after_cancel`，
需人工核对物理状态。预算已耗尽的 `requested`/`observed_absence` 效果在 `retry_effect`
前不再重复提交（过时意图仍走上面的探查）。


## Account resource and source effect service

The protected active installation also exposes `harness-native effect-service inspect --wi <id>`,
`measure --wi <id>`, `drain --wi <id> --budget 8`, and `tick`. These are service operations, not an Agent
or human session: they cannot create work, grant execution, request retirement,
accept a candidate, or turn a completed WI into permission to remove files.
Only the kernel's existing workspace/source effect intentions, and
`merge_pull_request` where policy admits `pull_request_gate`, may advance. Model
launch, notices and artifact delivery are excluded from this service.

`tick` requires the installed automatic execution policy. It reads native WI
identities and their head records from production Git and visits at most four WIs
(fewer once a batch has run 60 seconds), eight effects per WI by default. A visit
requests terminal reclamation, measures disk, advances effects and observes the WI.
Each operation rereads the authoritative facts through the normal kernel.

WIs wait in three queues that take turns leading, one turn per tick; each queue is
least recently visited first. Due: a WI with an open effect of the service's kinds
requested or retried after the journal revision its last visit recorded (none
recorded on a cold start, a lost cursor, an older program's cursor or a WI never
visited, so every open effect counts), or with an entry the tick could not read;
or a WI whose last visit moved an advanceable effect, met a retryable refusal
(retry backoff, busy ledger), or left a merge open (its checks and readback change
on GitHub without a ledger write). A merge the kernel no longer admits (the
effect view's `startable` is false: its WI ended, its PR was withdrawn, a later
round replaced it) is neither begun nor probed and never due, and neither is a
begin the kernel refused for good during the visit; an open attempt is still
read back. The schedule keeps one record per WI under `views_root`
(`effect-service-schedule/<WI>.json`, at most 64 KiB, at most 128 open effects:
the newest requests) with its last visit and its open effects as folded from its
journal, so a tick reads only entries newer than the last tick, a cold start
reads each journal once, and no number of WIs makes the schedule unreadable; a
malformed entry is that WI's read failure only, and a retry of an effect not
tracked there (one past the 128 kept) makes its WI news. Anything occupying a
record's path is replaced; writing or removing one record that still fails loses
only that record and the tick goes on, and the WI's last visit time is kept with
the turn so it never counts as unvisited and goes first again. Priority: any other WI whose head
another writer changed, or that is over its disk quota. Rotation: every other WI,
including one whose last visit left every advanceable effect as it was (the same
unknown read back, a workspace on another Host, a refused begin) and one never
visited. With one visit per tick, a WI waits at most 3 x (WIs in its queue) ticks,
so a WI with a confirmed merge is visited within 3 x (due WIs) ticks. One visit
probes at most two merges waiting on checks (`PROBE_LIMIT`), so k merges of one WI
whose checks pass together all begin within ceil(k / 2) visits after they pass;
the WI stays due while any of its merges is left to advance. A terminal WI with no effect left to advance, no workspace
left to reclaim and no exceeded quota waits six hours unless its head changes. The
visit records are disposable scheduling data, never a work-state
source; removing or corrupting them only repeats idempotent observations. Individual
failures remain in the report; they do not make other WIs depend on the failed WI's
state.

A tick, and each `measure` or `drain`, first discovers the repository copies
Agents made outside the managed roots (AH-086) once and shares the result among
the WIs it visits; the tick report's `agent_copies` gives per-Agent totals
(`null` for copies no Agent owns). A WI's `disk_quota.named_copies` lists the
copies that name it, and an active WI's `disk_quota.agent_copies` lists its
holder's copies that name no WI against the per-Agent limit;
`disk_quota.level_with_agent_copies` is the level they would cause while
`agent_copies_enforced` is false, and `disk_quota.hosts` gives each Host's
record. The service only measures; it never deletes a copy.

`measure --wi <id>` re-measures one WI's disk at once, and `drain --wi <id>`
re-measures before advancing its effects. After deleting the copies listed in
`disk_quota.unregistered`, `disk_quota.named_copies` or `disk_quota.agent_copies.copies`, the Agent runs, as the account user,
`<python> -I -B <scl-core>/active/harness.pyz effect-service measure --wi <id>`;
the printed `disk_quota` is the recorded level after that measurement (an unreadable
tree leaves it unchanged), and below the quota new work is admitted again. Neither
action takes the tick lock: the kernel serializes the write, and a repeated
identical measurement from the same Host is a replay. An audit installation refuses both.

The macOS managed installation seals `com.pulse.native-effects` with the same
program/consumer cohort and canonical Python. Deployment defers while a batch
holds its lock. Compatibility rollback stops and restores this consumer alongside
Relay and Stats, preserving production facts and original effect identities.
The old source-sync and legacy terminal-reclaimer jobs must be explicitly retired
at the maintenance boundary; registering this service is not proof they exited.

## Work record receipt

`harness-native record-receipt --wi <id> --record <record_id>` lets an unattended
check confirm that a run's reported work record exists, without a native session.
The protected active installation is the reader, as for the effect service; it may
read only the named WI and holds no intent or effect kind. The record ID is the
capability: it equals `op_` + digest of namespace and the committing request ID, so
only a holder of that ID can ask for it.

The command reads the normal `history` and `records` views. If the WI journal has
a `WorkRecorded` event from that request, it prints `harness.record_receipt/v1`
with `status: found`, `work_item_id`, `record_id`, `kind`, `author`, `revision`,
`recorded_at` (journal commit instant, UTC) and the production ledger identity, and
exits 0. Summary, artifacts and the author session are not returned. A checkpoint,
a record of another WI or an unknown ID returns `record_absent`; an unreadable WI
returns the kernel's rejection; both exit 2.

## Host file access

`grant_access` (`{paths, mode, reason}`, one to 32 normalized absolute paths,
`mode` read or write) records a session grant on the WI the caller holds; it
needs the human's latest approval in the same conversation (top-level
`human_input_ref`). Relay opens the paths to that one session; the grant ends with
the WI or with `revoke_access` (`{grant_id, reason}`, also human-approved).

`standing: true` asks for a standing grant instead: read only (write is refused
with `invalid_input`), bound to the caller's Agent, not to the WI it was requested
on or to one session. The WI only carries the audit record; the grant stays in
force after that WI ends. When every path is exactly the installed workspace root
of the caller's family counterpart, the kernel admits it without a further human
approval and records the operator's decision `operator_decision.2026-11-05.family_read`
as its source (`admission: family_rule`). The family is a base Agent `<base>` and
each avatar `<base>-<member>` (member: letters, digits, `_`) whose installed
workspace is `.../tenants/<member>/<base>-<member>-space`; base and avatar read
each other's workspace, siblings do not. Workspaces come from the installed host
policy's producer profiles, never from the request. Any other standing request
needs the human's approval as above (`admission: human`); without it the kernel
rejects it with `source_invalid`. A family grant that the installed layout no
longer supports is not applied. Any session of the Agent a standing grant serves
may revoke it without approval, whatever state its carrier WI is in.

`inspect` shows a WI's grants under `access_grants`: session grants with their
`session`, standing ones with `scope: agent`, `actor` and `admission`. Relay
reads access in two ways. `relay-access --input -` (`{"pid"}`, the installed
Relay's direct native child) returns `harness.session_access/v1`: that session's
grants on its bound WI and that WI's children (`work_item_id` names the parent)
and, from the same journal read, its Agent's standing grants (`standing`) and
the workspaces it may write (`workspaces`). The
journal read yields the candidates: every active WI the session holds, children
included, for each workspace that is ready (allocation or revision observed, or
an admitted existing allocation), not superseded, not asked to retire, and on a
branch that is a plain relative ref path. The host then proves each one before
it becomes a row. Git names a worktree's administrative directory after its
last path component and adds a number on a clash, so the directory is never
inferred: a successful allocation or revision records the one it proved
(`git_dir` in the observation details), and the readback takes that record or,
for a workspace without one, the directory the worktree's `.git` link names. It
opens only when that directory lies directly under the common Git directory's
`worktrees/`, its `gitdir` names the worktree's `.git` back, the `.git` link
names it, and its `HEAD` is the recorded branch; otherwise the workspace opens
nothing. A row has `work_item_id`, `resource_id`, `path` (the worktree), `write`
and `deny`. `write` is the worktree followed by exactly what `git add` and
`git commit` there write in the common Git directory: the proven administrative
directory, `objects`, and the branch's own `refs/heads/<branch>`,
`refs/heads/<branch>.lock` and `logs/refs/heads/<branch>`, for a new allocation
(`harness/<WI>/<id>`) and an existing one on its original branch alike; no ref
directory opens, so no other branch or WI does. `deny` stays closed inside them:
the worktree's `.git` link, the administrative `config.worktree`, `commondir` and
`gitdir` (which redirect or configure Git that the effect service runs
unsandboxed in the worktree), `objects/pack` and `objects/info` (packs,
alternates). Another session or Agent gets none; a WI that ends, a retirement
request or a replacement drops the row at the next read.
`relay-standing-access --input -` (`{}`, the installed Relay only) returns
`harness.standing_access/v1`: every Agent's standing grants (`actor`,
`grant_id`, `paths`, `mode`, `reason`, `admission`, `work_item_id`), with
`coverage` and `unavailable_items`. Both replay every WI journal; Relay
caches the second read and applies it when it starts any session of an Agent,
conversation or scheduled run (Relay `specs/relay-runtime-pool.md`).

## Collaboration inbox

`harness-native collaboration-inbox --actor <actor>` lets an unattended gate (a
Relay cron job) learn which original collaboration contracts await one Agent, so
the Agent is woken instead of waiting for one of its sessions to `list`. The
protected active installation is the reader, as for the record receipt; the seal
is checked again after the read. It holds no intent or effect kind.

One read of the production ledger replays every WI journal and keeps Draft and
active WIs. Each item names its `role`. As `recipient`, it lists each contract the
Agent still has to accept (`pending`) or settle (`accepted`, except accepted full
responsibility). As `requester`, it lists each contract the Agent offered that came
back answered and that it has not yet settled (`awaiting_settlement`: `status` is
the most pressing answer, `returned`, `rejected` or `completed`), adding
`answered_by` (the answering Agents) and `options` (the settlement actions, as in
`list`). Every item carries, under `harness.collaboration_inbox/v1` with
`status: read_only`: `role`, `work_item_id`, `contract_id`, `kind`, `status`,
`main_only`, `offered_by` (Agent only), `offered_at` (journal commit instant of
the offer, UTC) and an optional `deadline`; the response adds `coverage`,
`unavailable_items` and the production ledger identity; exit 0. Purpose,
resources, sessions and every other WI fact stay behind native authentication.
A contract offered by the Agent's own session is listed too: another session of
that Agent answers it (AH-088). An invalid actor or unreadable installation exits
2 with the kernel's rejection. A full read of about 700 WIs takes about 15 s on
ownermac, so a scheduled gate polls no more often than every 15 minutes. Relay
also runs an Agent's gate job at once when a new journal entry offers it a contract
or answers a contract it offered, read from the production checkout's local
remote-tracking ref; the schedule stays the fallback (Relay
`specs/relay-scheduler-jobs.md` §4e). The native cohort installs the gate from the
release at `~/.local/share/pulse/tools/collab-inbox/collab-inbox-gate.py`, the path
the jobs run. The gate
`Scripts/collab-inbox-gate.py` keeps its wake and delivery state per contract and
role, and wakes the Agent only for `main_only: false` items (an item without the
field counts as false; one without `role` is a recipient item); main-only items
never start a new session and never enter the gate's wake state. An `accepted`
recipient item does not wake the Agent periodically: only once, marked
`deadline_reminder` in the batch and the wake state, when its deadline is at most
24 hours away or past, and never without a deadline.

A main-only recipient item whose recipient was given a `target_work_item_id`,
and every requester item, also carries `target: {work_item_id, holder}` from the
same read. A requester settles on the WI it offered from, so that WI is its
target. `holder` is `recipient_here` (the inbox Agent holds the active target WI
on this installation's surface), `recipient_elsewhere` (another surface),
`other` (another Agent), `absent` (no such WI, not active, or no holder) or
`unavailable` (the target journal is unreadable). Only `recipient_here` adds
`holder_key = digest(["collab_delivery_holder", session_ref])`, where
`session_ref` is the holder's `session.` reference; the response adds
`holder_scope: {namespace, surface}`. A Relay recomputes the key from the native
session IDs of its own conversations, so the key identifies a session only to the
reader that hosts it. `held` lists every active WI the inbox Agent holds with the
same `{work_item_id, holder[, holder_key]}` report, so a scheduled wake job can
reach the conversation holding the WI it names. The gate asks the local Relay to deliver into that one
conversation each main-only recipient item and each requester item held there
(only the WI holder writes `withdraw_collaboration` or `reassign_collaboration`,
so an unattended session could not settle it), sending `role`, `main_only`,
`offered_by` and, for a requester, `answered_by` and `options`; it records the delivery and its
status, and asks again after the same re-wake interval or at once when the status changed.
Relay delivers each contract once per role and status, keeping the delivered marks under its
data directory across restarts; it does not push an accepted recipient contract (`suppressed`)
except one reminder when its deadline is within 24 hours or past. The inbox itself still lists
accepted contracts as owed, as `list`, `next` and the Stats board do. Every other main-only item stays in the inbox; every other
`main_only: false` requester item wakes the Agent with its `holder` in the batch.

The same read adds `pull_requests`: the results of pull requests on a Draft or
active WI whose latest round the inbox Agent registered (`role: submitter`), so
the holding session continues without a human prompt. A row appears for 24
hours after its result: `merged` (the kernel's merge read back), `changes_requested`,
`checks_failed`, `merge_failed`, `merge_blocked` (merge pending with its retries
spent) or `human_confirmation_required`. It carries `work_item_id`,
`candidate_id` (the round), `repository`, `number`, `status`, `head_sha`, `at`
(journal commit instant of the merge read-back, else of the review, else of the
registration), `next` (`intent`, `agent_action` and, for failed checks,
`failed_checks` cut to the delivery bound of 16 one-line names of at most 200
characters, with `failed_checks_omitted` counting the rest; absent once merged),
`review` (`score` and first-line `summary`, absent once merged) and `target` as a
requester item on its own WI. Every holder the inbox reports, for contracts and
results alike, is read as `children.followed` gives it in the same read: a child
WI is held by its parent's current holder, and by nobody while the parent is
stopped, in transfer, ended or unreadable, so nothing on it is deliverable then. A withdrawn
pull request or one awaiting review or merge has no row. The list is separate
from `items`, so an older gate that does not know the role ignores it. A result
goes only to the session that made the round's head, the one that registered
`submit_pull_request` with it. `author_ended` says whether the journal shows
that session ended, by either of two kernel records: after registering it
reserved a transfer (rollover) in an entry it committed on the WI or, for a
child, on its parent; or it switched away from that WI (its `request_switch`
there was entered in the target by `continue_here` with the Host's switch
proof, `SessionContinued.source`, or, since an entry into a target the session
already holds commits no event, the WI is afterwards held by another
session). Every round records `author_entry`, the Host-attested entry of its
registering session. A round registered by a scheduler run (`author_entry:
scheduler`), a fresh native session that never enters a conversation, goes to
no session: its row says `author_unattended: true` and `author_ended: false`
and has no `author_key`, so no gate delivers it, the holder's included; the
result stays in the pull request record, the inbox and Stats. A round from
before `author_entry` may be a scheduler run, so it counts as ended and its
holder hears of it. A continuation or takeover by another
session does not end it, nor does a conversation missing on some node. The
inbox keeps of each journal only these handoff facts, not its history. While it has not ended, the row carries
`author_key` (`holder_key`'s digest of the registering session) only on the
surface the session registered on; the gate there hands the row to the local
Relay with that key alone, Relay delivers to the conversation whose native
session matches it and waits while that conversation is busy, and every other
surface and session, the holder's included, delivers nothing. Once it has
ended, the row has no `author_key` and the gate where the WI is held hands it
over with the holder's `holder_key`. The gate never wakes an unattended session
for a result; Relay delivers each round once per status.


### ownermac 冷切换的独立旧后台

原 `com.pulse.workspace-sync` 与旧 SCL/reclaimer cron 已在冷切换中退出。原生激活不再生成或执行退役计划，也不读取旧核心的 binding 或 SQLite 封写触发器；旧核心数据只读归档。早先准备的 cohort 中保留的 `background_retirement` 计划只是历史证据，作为回退队列校验时不读取它。

该维护暂停自动提交共享人类目录的旧服务，保留原授权和全部目录内容。已批准的 Agent 源码发布与资源退休由新核效果服务承接。共享人类内容的全部自动提交功能不在此后台的权限内，不继承已完成 WI479 的执行授权。回滚保留这些旧后台的退役状态。

## External entries (ChatGPT web)

Where Relay runs the platform bot (`relay.json` `platformBot`), the installed
policy has `external_entries` and `chatgpt_web` is an entry. A ChatGPT web
session runs local commands through its command tool:

1. `harness-native entry-request --entry chatgpt_web --actor AGENT --wait 600`.
   The owner confirms on the platform bot; the result is
   `{"status":"confirmed","binding_id":...}`, or `pending`, `declined`,
   `expired`. `harness-native entry-wait REQUEST --timeout 600` waits again.
2. Every later command runs under the launcher:
   `harness-native entry BINDING -- execute --input FILE` (also `inspect`,
   `resume`, `list`, `identity`). The core treats the session as AGENT with entry
   `chatgpt_web`; the launcher appends each command to the session log.
3. A command that needs the owner's decision first asks for it:
   `harness-native entry BINDING -- entry-confirm --input DECISION.json`, where
   the file holds `{"intent","work_item_id","input"}` exactly as it will be
   executed. The confirmed result carries `human_input_ref`; pass it as the
   envelope's `human_input_ref` with byte-identical input. Changed input needs a
   new confirmation.

Text from the ChatGPT conversation is never human proof. The owner can revoke a
binding from its confirmation card; later commands are refused.
