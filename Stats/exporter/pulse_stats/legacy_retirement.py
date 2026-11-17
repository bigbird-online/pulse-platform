"""Owner-approved retirement, separate from recovery and incident history."""
from .live_contract import _capability, _short_hash

RETIRED_SOURCES = frozenset({'ownermac-probe','vultr-ack-probe','vultr-ledger-lint-probe',
    'vultr-executor-reaper','vultr-harness-daily-flush','vultr-units-in-git'})
DECISION = 'PULSE#603:owner-approved-retirement-2026-09-18'


def is_retired(incident):
    return incident.incident_key in {
        f'meta.probe_heartbeat:source_stale:{_short_hash(source)}' for source in RETIRED_SOURCES
    }


def capability(identifier):
    return _capability(identifier,health='not_applicable',freshness='not_applicable',observed_at=None,
        max_age_seconds=None,reason='旧常驻探针已按用户决定退役；不推定其曾恢复。',
        implementation_status='retired',aggregation_role='coverage_only',evidence=(DECISION,))
