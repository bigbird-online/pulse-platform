# WI568 current-source / authority / Stats acceptance repair

Base: PULSE main 458b642c3d07ff5fd1a73aca093851a7c303f98f. Continues WI568,
PR526 (already merged); no new WI and no relaxation of WI779/S5 gates.

Consumer result: existing Stats cost panel reports the actual configured runtime
sources, justified WI/executor attribution, effective usage time and unassigned
scale. Snapshot freshness is not usage freshness or source coverage.

P1 scope: runtime-derived registration and migrated Harness root; explicit Pi
protocol enumeration (do not fabricate headers or lose later files); exact native
identity/bounded execution join and overlap reconciliation; same-source native /
collector / ledger / query / publisher / Swift and incremental acceptance.

Risk: high. Candidate replay first; production changes only via exact-SHA Actions
with backups and readback. No model calls, source edits, deletion of ledger facts,
price changes, new UI design or ROI work. Red regressions and live evidence follow.
