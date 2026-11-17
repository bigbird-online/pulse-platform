# Shared production reader compatibility

Every installed SCL client consumes the same production Git journal. A new
writer event is a reader contract change for every live consumer, including a
host that is no longer the preferred UAT node but still serves conversations.

Individual WI observations and independent writes replay only their own WI.
Projection repair is scoped to the WI being written, including exact request
readback. Another WI cannot fail this transaction merely because this reader
does not understand its events. No unknown event is discarded or projected.

The complete hash chain, original references, sequence and footer are checked before
semantic replay. An unsupported event returns `journal_reader_upgrade_required`
and identifies the affected WI. Actual corruption remains `journal_invalid`.
Global queries and cross-WI usage attribution require complete relevant facts;
they fail explicitly when a required WI is unreadable rather than presenting a
partial collection or bypassing duplicate attribution checks.

CD must adopt the main-sourced native program on every live reader of the shared
ledger. Installation replays the whole ledger with the exact proposed bundle;
passing on one node does not prove adoption on another. Node-local installation
receipts are deployment evidence, never a second business authority.


Muse uses the existing `deploy-muse-relay.yml` exact-main authorization, then its
Hatch CD consumer. Normal Relay adoption includes native adoption; a running
consumer needing only the reader upgrade uses `muse-cd.py adopt-native-core`
with the same fresh exact-main authorization. Both paths use the registered
production Git repository, immutable bundle builder and production installer.
The latter preserves the current policy, Relay PID, manifests and transcript
roots. CD and installer locks plus the UAT idle gate protect pointer changes.
The proposed bundle must replay every current WI before adoption. A healthy
Relay or successful authorization alone cannot count as native core adoption.

## Growing production journals

A successfully published journal must remain readable by the same program.
Per-input capture limits do not limit the accumulated journal container. Git
batch transport must use private temporary backing rather than a single stdout
buffer; every declared object length, identity, type and separator is checked.
The old container is read only for compatibility and verified conversion. New
decisions use bounded immutable segments; public original bytes are loaded only
for explicit evidence or compatibility reconstruction. Private originals stay
outside Git and their absence cannot invalidate admitted decisions. A single-WI
read or write selects only that journal and its actual dependencies. Cross-WI
request and usage locators resolve to original decisions; unrelated legacy
records and unrelated transcripts are not inputs to this transaction.

Existing decision bytes, original digests, request identities and production
authority are unchanged by storage conversion. Adopt readers for the v2 storage
contract on every active node before the writer cutover. No history rewrite,
truncation, substitute ledger or rolling size-threshold increase is a recovery
step. Tests cross the former 32 MiB aggregate boundary through normal commands,
read the original request from a second client, preserve all originals, and
reject truncated or ambiguous Git transport without reporting partial success.
See [the production storage contract](rewrite-production-storage.md) for bounded
objects, private evidence custody, conversion and push-race recovery.
