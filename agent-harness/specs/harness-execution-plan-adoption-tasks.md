---
feature_name: "execution-plan-adoption"
version: "0.2.0"
status: "in_progress"
created: "2026-08-20T00:00:00+0800"
modified: "2026-10-07T00:00:00+0800"
owner: "Pulse"
tags: [tasks, execution-plan, adoption, wi-20260820-126]
---

# Harness Execution Plan Adoption — Tasks

Authority: `wi_20260820_126` + `ho_20260820_850`. This task is the priority CR-02 dependency of `wi_20260820_272`; it does not authorize Pulse to execute the first real Phase-A adoption on WI272.

| ID | Task | State |
|---|---|---|
| EPA-T-001 | Requirements / Design / Tasks for durable plan adoption and `/adopt` WI scope | implemented candidate |
| EPA-T-002 | `ExecutionPlanAdoption` model, repository-relative plan digest, durable store | implemented candidate |
| EPA-T-003 | per-WI locking, deterministic idempotency, single-winner conflict behavior | implemented candidate |
| EPA-T-004 | Atomic kernel adoption and event; disposable file projections | implemented candidate |
| EPA-T-005 | `harness execution adopt/show/validate` stable CLI + JSON result contract | implemented candidate |
| EPA-T-006 | `/adopt` backend scope resolver: explicit/unique-current WI only | implemented candidate |
| EPA-T-007 | automated normal/failure/idempotency/tamper/concurrency/privacy/CLI tests | local PASS: focused 18; broader 306; full Harness 1956 passed / 1 skipped |
| EPA-T-008 | GitHub PR CI + UAT + merge + default-branch smoke | real CLI UAT PASS; PR/CI/merge/default smoke pending |
| EPA-T-009 | reverse handoff to Hestia with commands/evidence; Hestia performs WI272 Phase-A dogfood | pending; Hestia-owned action after delivery |
| EPA-T-010 | WI018 single-writer migration, original history, plan input archive, publication replay and damaged-view tests | source candidate; installation and independent UAT pending |

## Definition of Done boundary

Pulse delivery is complete only after EPA-T-008 and reverse handoff in EPA-T-009. The root `wi_20260820_126` remains open until Hestia performs and verifies the required WI272 Phase-A dogfood; Pulse must not manufacture that final acceptance evidence.
