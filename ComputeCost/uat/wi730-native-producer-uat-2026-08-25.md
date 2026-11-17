# WI730 ComputeCost v2 native producer UAT — 2026-08-25

## Scope

Read-only validation of two real producer contracts. No production service, route, model, session, or runtime configuration was changed.

## Pi natural workload

The current resident Pi session injected its own `PI_SESSION_FILE`; the probe did not accept or print a raw session path or id. The session was ongoing, so this is a prospective partial-epoch observation rather than a final seal.

Command contract:

```text
pulse_compute_cost pi-session-probe --work-item wi_20260822_730 --phase work_item --epoch 1 --procurement-source chatgpt_subscription
```

Privacy-safe result is committed as `Bigbird/ComputeCost/uat/wi730-pi-native-live-probe-20260825.json`:

- generated_at: `2026-08-25T00:17:36.595277Z`
- native usage events: `296`
- total Token: `53,080,970`
- cache-read Token: `51,964,928`
- provider-estimated cost: `$40.072118999999999040` — this is not actual billed cash
- source prefix SHA-256: `69f822f3fcb3cc06c1568e4af4451668e941d060671e2a4b52467a3dfe0ac88b`
- evidence JSON SHA-256: `0a210dfbd00c2ba8cacdbea6edae2c5e8e0949e05c986c33b38e72b4316326b3`
- raw session/entry/response ids, content, cwd, and source path are absent

This sample demonstrates the long-context tool/model loop amplification that ComputeCost v2 must expose. It does not establish a final WI cost because the current Pi epoch remains open.

## ChatGPT embedded App Server contract

The running ChatGPT host owns `/Applications/ChatGPT.app/Contents/Resources/codex` version `0.149.0-alpha.4`; binary SHA-256 is `10afbeddd6f951635d8fcfbb337034d37934bb3495c16d053b3560d75747619b`.

Its generated experimental v2 schemas prove:

- `thread/tokenUsage/updated` includes `cacheWriteInputTokens` and nullable `modelContextWindow`; schema SHA-256 `aba4f6c7e4a19b2b842c08ee793b57000c07dafd57b922ad0d8e7c76609108c2`.
- `account/usage/read` can accept `threadId` and return thread-level Token groups plus estimated credits/USD when the billing route provides them; response schema SHA-256 `a58e58c852c888e5de925d99a66688d55ebb2fff038e795aadf04f8a8426dff8`.

The PULSE parser was corrected to preserve cache-write and accept unavailable context-window evidence. Generated inspection files were not committed and are not runtime dependencies.

## Result boundary

PASS for native Pi parsing/privacy and current App Server schema compatibility. ChatGPT App's existing stdio-owned app-server process is not externally attachable; persistent host subscription and final prospective App Server epoch remain pending.
