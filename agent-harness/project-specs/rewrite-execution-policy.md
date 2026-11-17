# Explicit native execution policy

AH-063 / A-30 require manual, audit and explicit automatic behavior. The native
Host and effect driver consume one validated policy value; business JSON,
observations and suggestion/shadow reports cannot select a mode or grant rights.
This slice changes native dispatch control and its batch budget, not the domain
authority holder or original journal. It does not claim full model/tool/path
selection, scheduled dispatch, collaboration spawning or an OS sandbox.

## Modes and effects

Optional top-level execution is exactly {mode, batch_budget}. mode is manual,
audit or automatic. batch_budget is an integer 1..64 (never boolean). An absent
execution setting means manual with budget 8. Unknown modes/fields reject at
both installation and native entry. Automatic work requires an explicit
operator-installed automatic mode; changing program bytes cannot opt it in.

Manual mode accepts normally authorized business commands and records their
durable effect intentions without automatically applying them. An explicit
resume/drain invocation advances at most the installed batch budget. Automatic
mode additionally advances that budget after a successful producer command,
excluding the existing measurement-only commands. Budget counts the existing
driver's effect outcomes/attempted operations, not elapsed time, tokens or all
future completion. No caller argument may increase it. A report labels the
actual installed mode/budget and whether automatic draining is enabled.

Audit mode permits native identity/usage boundaries and authenticated read-only
queries. execute rejects before reading command artifacts or capturing human
sources. resume/drain return an authenticated current read-only observation
without constructing adapters or changing effects. The native Host also removes
business and worker intents, physical publication bindings and effect rights.
An audit observation presents all non-read capabilities unavailable and inspect
as next action. Grants, task statuses, generations and existing effects are not
revoked or rewritten; returning to an authorized mode reveals only whatever
current domain authority still exists. A shadow/suggestion consumer only reads
these observations and cannot choose a write mode.

An already running adapter may have applied I/O before an operator changes
policy. Retain its original unknown obligation and read back from an admitted
successor; audit mode neither claims it was absent nor performs the readback
effect transaction. Ordinary communication outside Harness remains independent
of a missing/bad privileged policy. This module does not restart or reconfigure
Relay or native sessions.

## Writer contract 3 and safe activation

A program unaware of audit/manual semantics must not be reactivated under the
new policy. New bundles declare writer_contract=3. Installation under the same
database activation lock raises an existing floor 2 to 3, or initializes 3,
without changing business originals/events/heads, store identity or custody.
The contract UPDATE guard permits only a nondecreasing floor supported by the
invoking writer; DELETE remains forbidden. The old immutable UPDATE guard is
replaced only by the new installer inside that transaction. Floor changes and
pointer/admission commit retain the existing fault-recovery semantics.

Contract-2 installers cannot update the binding at floor 3 and cannot lower the
floor. The new installer refuses a bundle below 3 before preparation. Old
programs may still perform compatible historical reads, but their retired
program-and-policy identity and database contract cannot write or dispatch.
An explicit privileged operator that removes SQL guards remains outside this
protocol guarantee. No manual SQL recovery or replay of business requests is
part of this upgrade.

## Acceptance

Use real fixture effects to prove absent/manual execute does not perform I/O,
explicit resume obeys the configured budget, automatic execute requires the
explicit setting, and audit execute/resume/drain leave DB and target bytes
unchanged. Test audit Host worker defenses and current capability/next-action
masking without domain mutation; switching modes never invents a grant.
Reject malformed settings without partial installation. Create a contract-2 predecessor fixture using the former SQL guard protocol,
retain its original facts, upgrade to 3, then prove a contract-2 activation
cannot regain admission or lower the floor. Real old-bundle installation
upgrade on Muse remains a separate native acceptance. Exercise interruption
before/after pointer switch and retry of the same exact policy/program pair.
Preserve custody-specific failures and native original-session recovery.
Installed Muse adoption and source tests must be reported separately.

Source validation: 446 isolated tests passed in 51.75 seconds; strict mypy
checked 29 modules and ruff/diff passed. The supported upgrade replaces the old
trigger only at floor 2. Exact floor-3 retries preserve DB bytes, including
original custody conversion recovery. Native manual adoption is now independently checked on the original Muse Pi: target manual/1 records approval at revision 118 and two publication/adoption intentions at 119 without attempts or physical changes. The original source stays manual/8 and sealed. Both installed stores retain their original facts under floor 3. Target audit/1 was then adopted by the original Pi: resume and drain each returned read-only; one new checkpoint was rejected by policy_read_only. Independent verification of 17 commands and full DB/view bytes confirms revision 119 and all original resources/deliveries were retained. After restoring manual/1, two explicit resumes separately published the exact approved commit at revision 121 and adopted it in the actual consumer at 123. Each response contains one effect, each original effect has one successful attempt, and the intermediate consumer stayed at the old clean commit. Explicit automatic/1 then delivered A at revision 126. A proposed checkpoint correctly rejected because the terminal candidate remained frozen; it added no event or effect. Exact retry of the original declaration preserved its revision-124 decision and advanced only the original B effect at 128. Both files match the approved report and both effects have one successful attempt; no explicit resume/drain was used. Sixteen native command records and the journal/physical files were independently checked. The exact-request recovery regression passes the 16-test execution-policy module. See the execution-policy installation and native evidence files.


Final native settlement: the same approved candidate completed once at revision
129; exact retry returned the same original receipt, with one WorkCompleted
event. Required effects, both retained resources and original files were checked
independently. Management restored manual/1 through the formal installer without
changing business rows or custody; the original Pi adopted it. The final ordinary
state query legitimately rebuilt a disposable view from revision 103 to 129.
The controller's initial view-byte gate was unsuitable for that query, and the
native summary misidentified which commands had run; raw records were retained.
Explicit target revision 129 and source revision 79 queries then preserved all
database and view bytes. This is the isolated execution slice, not whole-WI UAT.


## Runtime tools and host maintenance

Once the Host has admitted the process and selected its configured tools,
ComputeCost baseline/progress/seal state is measurement evidence only. Pending,
failed, missing, or sealed metering cannot remove those tools, delay dispatch,
restart without tools, or remove the native
`pulse_work_item` recovery entry. Measurement failure is visible as unavailable
coverage, never as an OS permission denial or as a new grant. The native kernel
still decides every WI command from the production journal; host isolation,
configured tool allowlists and actual filesystem permissions remain in force.
An unadmitted process remains conversation-only. No receipt or scope is fabricated.

Explicitly authorized host maintenance is distinct from WI lifecycle mutation.
Absence of a production file from WI workspace resources is not proof that the
operating system denies access and does not create a requirement to invent an
SCL maintenance admission. Preserve the existing target writer's locking,
preimage checks and audit semantics. Do not substitute WI bookkeeping for the
requested operation. Report the observed restriction and its originating layer.

Acceptance: reproduce cold resume with hung/failed/sealed measurement; configured
write and native recovery tools stay available, excluded tools stay excluded,
unadmitted workspaces remain restricted, and usage is not falsely reported as
bounded. Exercise the actual Pi tool registry without invoking a user session.
