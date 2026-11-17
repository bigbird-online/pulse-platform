# WI730 S0R dogfood — WI568 resident continuation latency

Date: 2026-08-22
Root WI: `wi_20260822_730`
Observed WI: `wi_20260820_568`

## Observation

A real Feishu bot continuation on a resident Linux cloud surface eventually restored WI568 correctly, but spent about seven minutes before returning to the WI business frontier. The Session reread workspace contracts, inspected local/default branch drift, rediscovered PR/Actions state, diagnosed the old `chatgpt_web` binding, inspected the multi-surface implementation, resolved the surface registry, performed resident entry, opened a companion ledger PR, ran tests, and waited for CI.

The prior `/rollover` checkpoint revision 4 already contained the critical work frontier: completed facts, remaining 7-day/10-outcome/human-review gates, no-backfill decisions, risks, and next action. The correctness path therefore passed while continuation usability failed.

## Regression verdict

- durable same-WI/owner authority: PASS
- resident surface adoption: PASS
- no historical backfill: PASS
- work-frontier recovery latency: FAIL
- observed TTFWA: approximately 7 minutes
- S0R target: resident P95 < 30 seconds

## Required fix

Normal continuation becomes:

```text
injected/resolved surface descriptor
→ one `harness continue`
→ ResumePacket
→ only stale_if / must_read revalidation
→ first WI business action
```

Git/PR ledger replication remains auditable but is not a synchronous prerequisite for each same-Agent resident Session replacement. This evidence does not claim the future S0R UAT has passed; a new real-channel dogfood is required after the fast path lands.
