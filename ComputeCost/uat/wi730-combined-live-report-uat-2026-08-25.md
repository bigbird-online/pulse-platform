# WI730 combined ComputeCost live report UAT — 2026-08-25

## Scope

A privacy-safe Pi native snapshot from the current WI730 session was joined with the previously captured digest-bound App Server account/quota snapshot. No model workload was manufactured for UAT; these are costs of the ongoing WI730 work itself.

The Pi epoch remains open. The report therefore exposes exact observed-to-date counters while refusing to call them final WI cost.

## Pi snapshot

`Bigbird/ComputeCost/uat/wi730-pi-native-live-snapshot-20260825.json`

- generated_at: `2026-08-25T00:51:24.117964Z`
- lifecycle: `open`
- native usage events: `374`
- total Token: `85,173,760`
- cache-read Token: `83,898,368`
- provider-estimated cost: `$74.797038999999997950` — not billed cash
- semantic snapshot SHA-256: `b962dc15b7b2e72ad568fab4fee0402b001b2c7d66b904f462b1ff0c6bee4d0a`
- evidence file SHA-256: `1d3b8daa2ef70dc5c716a2ad9982a22cf71da31c390a20e6a09cdf4a47051724`

Raw Pi session/entry/response ids, message content, cwd, and source path are absent.

## Combined WI report

`Bigbird/ComputeCost/uat/wi730-combined-live-report-20260825.json`

- Pi status: `exact_open`
- quota scope: `account_not_work_item`
- primary quota: `27%` used in the `10,080` minute window
- App Server sealed WI usage: unavailable, not zero
- actual marginal WI cash: unavailable, not zero
- outcome/artifact/rate-card/business-value joins: unavailable
- semantic report SHA-256: `a9af22c9c9b460aee402d1003c7f008d4ab0786a0ca1b2549feabc683052459b`
- evidence file SHA-256: `aab56308280e57cb3a92871584e48243b95755a635c825e9c80caf0e23bc3c19`

## Result boundary

PASS for digest-bound Pi snapshot validation and WI report join. This report makes the present long-context amplification visible without claiming final cost or allocating subscription cash. Final Pi source seal, exact App Server thread usage, Harness outcome/artifact joins, and business-value evidence remain pending.
