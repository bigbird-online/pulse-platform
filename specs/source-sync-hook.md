# Event-driven four-end source synchronization

Owner: Pulse. Work item: WI479. Scope: source and documents only.

## Contract

Each of CollaborateWithAI, PULSE and Oikos enrolls a native GitHub repository
`push` webhook. A signed `refs/heads/main` update in any repository wakes the
same standing-authorized three-repository `harness sync-workspace run-batch`.
Author, bot identity, commit message and changed paths do not suppress it.
This includes GITHUB_TOKEN-created commits and skip-CI messages; it does not
depend on Actions' push-workflow triggering rules.

The receiver is independent of Harness/Relay runtimes. It binds loopback only.
A dedicated Vultr Tailscale Funnel HTTPS 443 route publishes only this receiver
through a loopback-only SSH reverse forward. Existing ownermac 443 Serve routes
remain private and unchanged. TLS verification stays enabled
on GitHub. Shared HMAC secrets and configuration are owner-only and Git-external.
The receiver verifies the raw-body SHA-256 HMAC and enrolled repository ID/name
before admission, bounds request size/concurrency/time, and never executes
payload-supplied commands, paths or Git refs.

Admission commits minimal delivery metadata into a FULL-synchronous SQLite
inbox before responding 202. GitHub delivery IDs deduplicate retries; reusing
an ID with a changed digest is rejected. The single worker snapshots the inbox
sequence and only acknowledges through that sequence after one exact typed
batch receipt proves all three repositories complete, clean and at current
authority. Events arriving during a run remain pending for the next batch.
Synchronization-produced pushes take the same path; a clean follow-up batch
does not itself manufacture a commit, so there is no author-based suppression.

The service lock excludes duplicate receivers. The execution lock is inherited
by the batch child, so an adapter restart cannot overlap its surviving child.
Harness remains responsible for its authorization, Broker, preservation and
transaction recovery. Inherited Git environment and interactive SCL identity
are not passed into the standing-authorized batch.

Failed or unknown attempts retain pending events and use capped exponential
backoff. This is retry of outstanding work, not periodic source polling. Public
health returns liveness only; owner-local `status` exposes pending/error state.
GitHub commit status `four-end-source-sync` reports pending, failure and success.
Status-publication failures are separately logged, never treated as sync success.

Failed batches retain repository-level error codes and bounded reasons in the
worker error, rather than reporting only batch_incomplete. The owner-only
last-failure.json preserves the latest completed failed payload and process exit
code across subsequent attempts truncating last-attempt.stdout. This record is
diagnostic only and never acknowledges an inbox event. Index-lock recovery belongs
to the authorized Harness workspace updater, never the webhook receiver.

## Delivery limitation and recovery

GitHub does **not** automatically redeliver failed webhooks. A durable inbox
cannot protect a request that never reached it. Every service startup therefore
enqueues one full current-authority catch-up, including after outages longer
than GitHub's three-day delivery-history window. A later valid push also causes
a full three-repository batch, covering missed intermediate commits. On ownermac,
native `scutil -W -r api.github.com` reachability callbacks also enqueue catch-up
on network changes. This is an OS event subscription, not timed Git polling.
If that watcher exits, launchd restarts this hook and startup recovery runs.

An upstream ingress-only outage which recovers without a local network callback,
service restart or later delivery can leave an undelivered update outstanding. No periodic polling
is installed to conceal this limitation. Operator recovery is a GitHub delivery
redelivery or a restart of this hook alone, never a runtime restart. Further
transport recovery beyond these local events requires an upstream durable
delivery service; it is not claimed by inbox retry alone.

## Deployment

`Scripts/install-source-sync-hook.py prepare` requires an exact PULSE revision
reachable from current GitHub main. It packages only the receiver and existing
`pulse-gh.py` gateway by content digest, creates the owner-only secret/config,
and prepares `com.pulse.source-sync-hook.plist`. It does not activate it, install
Harness, change Relay, copy a repository release, or restore old polling jobs.
The installer always selects `$HOME/.local/share/coevolution/python-env/bin/python`
and verifies its `sys.prefix` before creating persistent state. The service,
configuration validator and GitHub gateway use that same interpreter. CI uses
the registered resident toolchain and a complete candidate checkout; it does
not inherit an earlier job's sparse selection or use the system Python for tests.

Use a stable owner-local Harness command path; do not pin a disposable runtime
release in a persistent service definition.

After inspecting existing Serve/Funnel configuration, use installer `activate`
to link only this plist into the owner's LaunchAgents directory and bootstrap
this isolated service (including next-login persistence).

Use the installer from merged main to `prepare-tunnel --state-directory <state>
--ssh-target <registered-cloud-alias>`, then `activate-tunnel --state-directory
<state>`. It creates only `com.pulse.source-sync-hook-tunnel`, using existing SSH
identity and host-key policy. The reverse listener is **127.0.0.1:18794** on the
cloud host, forwarding to the same loopback port on ownermac. SSH connection
keepalives detect broken transports; launchd reconnects a failed connection.
Neither component reads Git periodically. Inspect remote listening sockets first;
an occupied port fails closed. Do not turn off host-key checks or bind publicly.

On the independently verified cloud host with no conflicting Serve route, run
`tailscale funnel --bg --https=443 http://127.0.0.1:18794`. Do not configure
Funnel's backend directly to a remote tailnet HTTPS name: that failed in the
deployed path, while the loopback transport passed the same public checks.
Check TLS and unsigned-request rejection before calling the installer's `enroll` with
the exact `/github/push` URL. Enrollment retains a Git-external hook ID manifest,
is resumable after partial creation, and refuses conflicting existing hooks.
For an explicitly selected replacement endpoint, enrollment updates the recorded
hook IDs through GitHub's config-only endpoint, preserving their HMAC secrets.
It retains not-yet-migrated records if interrupted and refuses identity drift.

The deployed URL is
`https://vultr-pulse-192-0-2-10.tailnet.example/github/push`.
After verified cutover remove only the retired ownermac 8443 mapping. Never
publish ownermac's unrelated private 443 service.

Rollback: disable only the three recorded hook IDs, boot out only
`com.pulse.source-sync-hook` and `com.pulse.source-sync-hook-tunnel`, and remove
only this cloud 443 Funnel mapping. Preserve
the inbox, receipts, secret and package for replay. Never reset all Funnel
configuration or delete another service's files.

## Verification

CI defaults to `pulse-linux-trusted` and executes only isolated fixtures.
Real acceptance separately requires successful signed GitHub deliveries for
all three enrolled repository IDs, event-to-batch correlation, and an exact
three-repository four-end receipt. Test duplicate/invalid requests, burst arrival,
worker crash, pending retry and receiver restart. HTTP 202, health, source
publication, and local tests are not end-to-end convergence evidence. Independent
Outcome, human review and terminal settlement remain separate WI gates.

References: [GitHub delivery failures](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries),
[GITHUB_TOKEN trigger rules](https://docs.github.com/en/actions/concepts/security/github_token).

[WI479 deployment evidence](evidence/wi479-source-sync-hook-ingress.md) records
point-in-time transport failures, recovery and native-delivery acceptance.
