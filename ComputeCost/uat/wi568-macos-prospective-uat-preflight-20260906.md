# WI568 macOS prospective UAT preflight — 2026-09-06

## Result

The macOS prospective UAT has a frozen implementation scope, but it is **blocked before baseline**. No macOS usage epoch was opened, no Token was counted, and no UAT gate passed.

- validation default: `f0a1a14a7dc3ee3fe19cc8659172b9133da0e247` (PR #412 merge);
- installed PULSE: `307bb878ae1d7531984b946b0fc371a6bfc87c52`;
- installed relevant bytes match the validation default: **PASS**;
- managed workspace capacity admission: **PASS**;
- target workspace registration: **BLOCKED — `authority_unavailable`**;
- zero baseline, real increment, final seal, Token conservation and canonical join: **UNAVAILABLE**.

Machine-readable evidence is in `Bigbird/ComputeCost/uat/wi568-macos-prospective-uat-preflight-20260906.json`.

## Frozen implementation

GitHub reports PR #412 merged into the default branch. The default-branch Git blob identities for `pi-usage-binding.mjs`, `scl_measurement.py`, and `wi_report.py` equal the local candidate blobs, and the installed macOS files have the same SHA-256 values previously accepted on cloud-linux-b:

| File | SHA-256 |
|---|---|
| `runtime/pi-usage-binding.mjs` | `ef1be16dcab33d9faad273a0ee7e49ad4bf95629276c47d2e67703069dbc7b04` |
| `pulse_compute_cost/scl_measurement.py` | `140d303bb72b4e6c000723a2c9e9a158531cf2715d1f2cd01e08a7269de27cb4` |
| `pulse_compute_cost/wi_report.py` | `dba042f09b2d54f86828aee959ce70765dffba5bd51a3be749e94d71fcf07aaf` |

The installed Relay uses the reviewed host-native Pi profile and the installed ComputeCost extension. No prior native snapshot for this WI exists on the macOS surface, which is a precondition check only—not a zero baseline receipt.

## Workspace and authority boundary

Both required repositories received exact `managed_workspaces/v1` allocations. Capacity admission allowed the PULSE and authority worktrees with projected disk use remaining at 76%; both allocated worktrees were created at the exact destinations and read back clean.

Registration was then rejected with `authority_unavailable`: the live serial execution fence still belongs to cloud-linux-b, while the target-side legacy registration writer has no sealed authority to create macOS registration bytes. The failed command retained a partial one-repository manifest, but its authority validation fails and it is not executable. Neither target worktree was used for execution. No direct state edit, synthetic host identity, competing surface execution, or fallback workspace was used.

## Loaded contract diagnosis

The current CollaborateWithAI default is `bccd918a38b46f6322d7bac6292464512fa5d06f`; the loaded SCL source is its ancestor `1582deaec585a336dd1509c19155ff2c37d47719`, and the loaded `workspace_policy.py` and `scl_lifecycle.py` bytes match current default. This is therefore a current-contract limitation, not stale installation.

The replacement claim can record a target surface, but runtime workspace admission accepts a target only when the recorded target surface is absent or equals the current source surface. A macOS target differs from the active cloud source, and its registration digest necessarily differs because surface and filesystem facts are part of the manifest. No ordinary sealed operation currently rotates those facts before target process admission. Active-surface recovery is not a legal substitute: the source remains live and healthy, while that command is reserved for an exact stale authority with separate explicit human recovery approval.

## Acceptance impact

This preflight preserves the serial-surface rule and exposes a real entry prerequisite. It does not weaken or satisfy any remaining gate:

- macOS prospective UAT: **blocked before baseline**;
- Tencent Cloud prospective UAT: **not started**;
- linked outcome and human attention: **unavailable**;
- ChatGPT+GitHub prospective full coverage: **pending**;
- controlled live crash UAT: **pending**;
- S5-001 seven-day/ten-eligible-outcome gate: **pending**.

The next safe action is to implement, review, merge, install, and read back a sealed cross-surface workspace-authority transition that atomically validates the complete target registration before process spawn. Only after that readback may host-native Pi establish a mode-0600 zero baseline and consume naturally occurring work.
