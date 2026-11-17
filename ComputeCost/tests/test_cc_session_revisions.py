"""One CC provider message may have several content-block timestamps."""
import json
from pulse_compute_cost import mcp_request_purpose as m
from pulse_compute_cost.receipt_sources import read_cc
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from test_mcp_cc_session_wi import SESSION, WI, OTHER_WI, usage

OBSERVED = "2026-01-02T00:00:00Z"

def fixture(tmp_path):
    first, last = usage(SESSION, 1, "same"), usage(SESSION, 2, "same")
    first["uuid"] = "block-a"
    first["message"]["stop_reason"] = None
    first["message"]["usage"]["output_tokens"] = 1
    path = tmp_path / "native.jsonl"
    path.write_text(json.dumps(first) + "\n" + json.dumps(last) + "\n")
    rows = read_cc(path, OBSERVED)
    scope = reference("native_session", ["cc", SESSION])
    declarations = {scope: [{"wi": WI, "native_evidence_ref": reference("proof", "original-call")}]}
    return rows, declarations

def query(ledger, **kwargs):
    return query_collaboration(ledger, CollaborationQuery(scope="activity", from_date="2026-01-01", to_date="2026-01-02", timezone="UTC", group_by=("wi",), **kwargs))

def legacy_claim(row, declarations, *, wi=WI):
    scope = row.payload["native_scope_ref"]
    proof = reference("native_session_wi", [scope, wi, declarations[scope][0]["native_evidence_ref"]])
    return Receipt.make("attribution", [m.POLICY, row.entity_ref], {
        "target_ref": row.entity_ref, "wi": wi, "executor": None, "responsible": None, "delegator": None,
        "state": "assigned", "policy": m.POLICY, "effective_at": row.occurred_at},
        occurred_at=row.occurred_at, observed_at=OBSERVED, producer=m.POLICY,
        evidence=(proof, reference("native_session_usage", [scope, row.entity_ref])))

def test_content_blocks_produce_one_stable_session_assignment(tmp_path):
    rows, declarations = fixture(tmp_path)
    assignments = m.session_attributions([], rows, declarations, OBSERVED)
    assert len(assignments) == 1
    assert assignments[0].payload["effective_at"] == rows[0].occurred_at
    ledger = UsageLedger(tmp_path / "ledger")
    ledger.ingest_receipts([*rows, *assignments])
    assert query(ledger)["groups"][0]["key"] == {"wi": WI}
    assert m.session_attributions(ledger.receipt_snapshot()["facts"], rows, declarations, OBSERVED) == []

def test_reverified_same_declaration_repairs_only_its_timestamp_forks(tmp_path):
    rows, declarations = fixture(tmp_path)
    ledger = UsageLedger(tmp_path / "ledger")
    original = [*rows, *(legacy_claim(r, declarations) for r in rows)]
    ledger.ingest_receipts(original)
    before = ledger.receipt_snapshot()
    assert query(ledger)["groups"][0]["key"] == {"wi": "ambiguous"}
    fixes = m.session_attributions(before["facts"], rows, declarations, OBSERVED)
    assert len(fixes) >= 1
    ledger.ingest_receipts(fixes)
    assert query(ledger)["groups"][0]["key"] == {"wi": WI}
    assert query(ledger, watermark=before["watermark"])["groups"][0]["key"] == {"wi": "ambiguous"}
    assert ledger.receipt_snapshot(watermark=before["watermark"])["sha256"] == before["sha256"]
    assert m.session_attributions(ledger.receipt_snapshot()["facts"], rows, declarations, OBSERVED) == []
    assert query(ledger)["summary"]["usage"]["known_total_tokens"] == query(ledger, watermark=before["watermark"])["summary"]["usage"]["known_total_tokens"]

def test_contrary_wi_and_foreign_timestamps_are_not_repaired(tmp_path):
    rows, declarations = fixture(tmp_path)
    first = legacy_claim(rows[0], declarations)
    contrary = legacy_claim(rows[1], declarations, wi=OTHER_WI)
    assert m.session_attributions([*rows, first, contrary], rows, declarations, OBSERVED) == []
    wrong = Receipt.make("attribution", None, {**first.payload, "effective_at": "2026-01-01T01:00:59Z"},
        entity_ref=first.entity_ref, occurred_at="2026-01-01T01:00:59Z", observed_at=OBSERVED,
        producer=m.POLICY, evidence=first.evidence)
    assert m.session_attributions([*rows, first, wrong], rows, declarations, OBSERVED) == []


def test_competing_native_finals_do_not_duplicate_session_purpose(tmp_path):
    rows, declarations = fixture(tmp_path)
    finals = [Receipt.make("usage", None, {**r.payload, "final": True}, entity_ref=r.entity_ref, occurred_at=r.occurred_at, observed_at=OBSERVED, producer=r.producer) for r in rows]
    assignments = m.session_attributions([], finals, declarations, OBSERVED)
    assert len(assignments) == 1
    ledger = UsageLedger(tmp_path / "final-ledger")
    ledger.ingest_receipts([*finals, *assignments])
    assert query(ledger)["summary"]["usage"]["conflicted_event_count"] == 1


def test_session_declaration_covers_exact_earlier_host_observation(tmp_path):
    rows, declarations = fixture(tmp_path)
    p = {**rows[-1].payload, 'interval_start':'2026-01-01T01:00:00Z',
         'interval_end':rows[-1].occurred_at, 'execution_ref':reference('execution','host'), 'executor':'pulse'}
    host = Receipt.make('usage', None, p, entity_ref=rows[0].entity_ref,
                        occurred_at=rows[-1].occurred_at, observed_at=OBSERVED, producer='cc_message.v1')
    ledger = UsageLedger(tmp_path/'host-ledger')
    ledger.ingest_receipts([host,*rows,legacy_claim(rows[-1],declarations)])
    before = ledger.receipt_snapshot()
    fixes = m.session_attributions(before['facts'], rows, declarations, OBSERVED)
    assert fixes[0].payload['effective_at'] == host.payload['interval_start']
    ledger.ingest_receipts(fixes)
    assert query(ledger)['groups'][0]['key'] == {'wi':WI}
    assert query(ledger)['summary']['usage']['agent_unassigned_event_count'] == 0
    assert ledger.receipt_snapshot(watermark=before['watermark'])['sha256'] == before['sha256']
    wrong_scope = Receipt.make('usage', None, {**p, 'native_scope_ref':reference('native_session','other')},
                               entity_ref=rows[0].entity_ref, occurred_at=rows[-1].occurred_at,
                               observed_at=OBSERVED, producer='cc_message.v1')
    assert m.session_attributions([wrong_scope],rows,declarations,OBSERVED)[0].payload['effective_at'] == rows[0].occurred_at
