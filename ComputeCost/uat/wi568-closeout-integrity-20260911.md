# WI568 closeout integrity repair — 2026-09-11

Tracking: PULSE Issue #492 and PR #493. Owning work remains WI568 / dependency WI779; this repair is not an S5 eligible sample.

## Reproduced defects

The reviewed default `d26608036f3020ee0de0132a40a359217956a422` passed its 229 Python and 19 extension tests, but independent counterexamples showed:

1. A real capture process killed after mkdir-lock acquisition left two fresh retries waiting approximately 30 seconds each; neither called the sampler or produced a final.
2. A legitimate old same-WI valuation could price a new evidence set, and the public CLI published a 100,500-Token source's cash alongside the conflicting ledger's 302,000-Token total.
3. Equal known subtotals promoted missing-producer/open-epoch evidence to `exact`.

Eight initial report regression cases were run against the old source in isolated Linux scratch: seven failed and the complete-matching positive case passed.

## Root corrections

- The native Python sampler and publisher own one OS flock through private atomic publication. The lock file's existence is not liveness and is never removed to admit another writer. Real process death releases the lock; an orphaned but still-running writer retains it until it finishes. Node independently reads and checks the artifact, baseline descent, source prefix and existing SCL authority.
- Valuation v2 binds the complete source evidence digest. Consumption checks source identity and model/Token-class quantities. Stale, absent or conflicting evidence cannot populate canonical machine cash.
- Partial discovery, unsealed expected epochs and model disagreement remain incomplete. Known priced subsets are explicit `partial_machine_cash_usd`; unpartitioned historical incurred Token remains visible, incomplete and unpriced.

The explicit Pi/SCL snapshot and receipt contracts, flat OpenRouter standard rates, no-subscription policy, and historical immutable evidence remain unchanged.

## Executed verification

Source candidate `d078c696905c1b82acc26d964e5ce5795c7b4107`:

| Formal run | Result |
|---|---|
| ComputeCost CI `34548625011` | 245 Python tests; 20 extension tests; 33 runtime-manager tests passed; spec drift guard passed |
| PULSE UAT control-plane `34548722229` | validator and 213 contract tests passed |
| PULSE UAT Relay `34548724963` | 1791 tests passed, 2 existing skips; resolved candidate SHA explicitly recorded by workflow |
| ownermac Actions `34549239661` | 245 Python tests and 20 extension tests passed using installed Python 3.13; real subprocess lock/crash tests and real Node→Python host transport passed |

The process tests kill the actual sampler after acquiring the kernel lock and the actual writer before atomic publication, verify a live contender cannot bypass ownership, and verify fresh-process retries/readback. A separate public CLI test retains conflicting source quantities while refusing canonical cash. No model call or production workload is used for these regression fixtures.

During verification, default advanced to `520fe1de1bec3ed7b49cd180aabe3cdb79f3538d` with the paired SCL recovery changes. It was merged into this candidate at `0b9b4cf5a0e18a544a96748e5757958f9f796b1a`, preserving identical ComputeCost bytes. That head's ComputeCost CI `34549341768` passed; integrated Relay/control-plane verification was dispatched as `34549513466` / `34549516233`. Their final status must be read from Actions, not assumed from this dispatch record.

## Delivery boundary

Temporary patch-materialization scripts and their writable workflow were removed before the source candidate. The separate read-only macOS verification branch is temporary and must be removed after evidence collection.

These are code and process-level verification results, not an installed business consumer or full-program closeout claim. The initial macOS readback showed installed release `2e7cda80005b857b39f85031b55073bed507ab60`; its legacy-system-Python kernel read was unavailable and is not evidence that canonical work is absent. A later supported-runtime readback is required.

Original WI568 ChatGPT+GitHub prospective evidence and legitimate seven-day/ten-eligible-outcome gates are not waived. No outcome, hidden Token, attention time, authority or current installed revision is inferred or fabricated. Runtime deployment, actual consumer readback, independent outcome and exact-candidate acceptance must be evidenced before their applicable closeout transitions.
