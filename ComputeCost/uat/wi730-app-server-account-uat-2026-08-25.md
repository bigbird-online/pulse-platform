# WI730 App Server account usage/quota UAT — 2026-08-25

## Scope

A separate read-only App Server process used the host-bound embedded ChatGPT binary and CODEX_HOME. It performed only `initialize`, `account/usage/read`, and `account/rateLimits/read`; no thread, turn, model workload, production route, or account setting was created or changed.

The binary path and CODEX_HOME came from host environment bindings and are absent from output. App Server initialize metadata, account/user/thread ids, reset-credit ids, and multi-bucket private keys are also omitted.

## Live facts

Privacy-safe snapshot: `Bigbird/ComputeCost/uat/wi730-app-server-account-live-probe-20260825.json`

- generated_at: `2026-08-25T00:37:26.640094Z`
- account lifetime Token: `16,221,198,886`
- daily coverage: `302` buckets from `2025-09-12` through `2026-08-24`, conserving the lifetime total
- peak daily Token: `1,484,326,240`
- plan: `pro`
- primary quota: `27%` used, `10,080` minute window, reset at `1788137010`
- credits: balance `0`, `has_credits=false`, `unlimited=false`
- snapshot semantic SHA-256: `86f1b1e220f6e0f42742f7fb8c754725ce4e5bb2781e3d10c67d7e862295c72c`
- evidence file SHA-256: `0059536640b78da0d4d50e9adba66c9f5f9cfe3540fa7127c2805c282ff11a7d`

Account lifetime/daily usage is reconciliation context, not WI attribution. Quota is current account pressure, not a WI cost.

## WI report join

`Bigbird/ComputeCost/uat/wi730-account-context-report-20260825.json` consumes the digest-bound snapshot and proves:

- WI usage remains `unavailable` without a sealed WI epoch; account lifetime Token is not copied into WI usage.
- quota is included with `scope=account_not_work_item`.
- `actual_marginal_cash_usd` remains `unavailable`; zero add-on credit balance is not rewritten as zero WI cash.
- report semantic SHA-256: `1e3b1d7eb28104704eeac831f9b51885e48306902bc7cfc0590759a98f5e22fd`
- evidence file SHA-256: `92eee60ba111b7bdb912cc211a6e0e991eb78e1f2b8822c59c6e2a978b8ab4e7`

## Result boundary

PASS for real App Server account/quota read, sanitization, digest validation, and account-context WI report join. Thread-level credits and exact WI App Server usage still require a host-provided thread binding and final source seal.
