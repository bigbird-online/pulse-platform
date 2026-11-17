"""CC consumes the same explicit workspace authority as other native runtimes."""
import json
from copy import deepcopy
from pathlib import Path

from pulse_compute_cost.collaboration_query import CollaborationQuery, query_collaboration
from pulse_compute_cost.ledger import UsageLedger
from pulse_compute_cost.receipt_sources import actor_receipt, collect_registered_sources
from pulse_compute_cost.receipts import Receipt, reference
from pulse_compute_cost.source_registration import actor_candidate_registry

AT = '2026-10-02T10:00:00Z'
CATALOG = {'data': [{'id': 'anthropic/test', 'pricing': {
    'prompt': '1', 'completion': '2', 'input_cache_read': '0.1', 'input_cache_write': '3'}}]}

def setup_source(root):
    native = root / 'native'; native.mkdir()
    cwd = str(root / 'PULSE/local-pulse-space')
    row = {'type': 'assistant', 'sessionId': 'real-session', 'uuid': 'native-message',
           'cwd': cwd, 'timestamp': AT, 'message': {'id': 'provider-message',
           'model': 'anthropic/test', 'stop_reason': 'end_turn', 'usage': {
           'input_tokens': 10, 'output_tokens': 3, 'cache_read_input_tokens': 20,
           'cache_creation_input_tokens': 5}}}
    path = native / 'real-session.jsonl'
    path.write_text(json.dumps(row) + '\n')
    ledger = UsageLedger(root / 'ledger'); ledger.root.mkdir(mode=0o700)
    registry = {'schema': 'computecost_source_registry/v1', 'ledger_root': str(ledger.root),
                'required_receipt_features': ['actor_context.v1'],
                'sources': [{'id': 'original-cc', 'kind': 'cc', 'path': str(native),
                             'session_wi_mcp_since': '2026-10-02T01:51:01Z'}]}
    surfaces = {'agent_id': 'pulse', 'surfaces': [{
        'surface_id': 'surface_v1_local_macos', 'workspace': 'local-pulse-space',
        'deployment': {'site': 'ownermac', 'pulse_home': str(root / 'PULSE'),
                       'kernel_root': str(root / 'kernel')}}]}
    return ledger, registry, surfaces, path, row


def report(ledger, watermark=None):
    return query_collaboration(ledger, CollaborationQuery(scope='activity',
        from_date='2026-10-02', to_date='2026-10-03', watermark=watermark,
        group_by=('agent', 'wi', 'node', 'entrypoint', 'runtime')),
        catalog=CATALOG, catalog_observed_at=AT)


def test_cc_registration_preserves_admission_and_other_sources(tmp_path):
    _, registry, surfaces, _, _ = setup_source(tmp_path)
    registry['sources'].append({'id': 'peer', 'kind': 'receipts', 'path': '/peer'})
    before = deepcopy(registry)
    enrolled = actor_candidate_registry(registry, surfaces, site='ownermac')
    cc = enrolled['sources'][0]
    assert cc['workspace_actors'][0]['runtime'] == 'cc'
    assert cc['scl_surface_id'] == 'surface_v1_local_macos'
    assert cc['scl_kernel_root'] == str(tmp_path / 'kernel')
    assert all(cc[k] == before['sources'][0][k] for k in ('id', 'path', 'session_wi_mcp_since'))
    assert enrolled['sources'][1] == before['sources'][1]
    assert registry == before
    assert actor_candidate_registry(enrolled, surfaces, site='ownermac') == enrolled


def test_cc_identity_refreshes_with_cached_native_usage(tmp_path):
    ledger, registry, surfaces, _, _ = setup_source(tmp_path)
    collect_registered_sources(registry, ledger)
    before = ledger.receipt_snapshot(); old = report(ledger)
    originals = {r.fact_ref: r.to_dict() for r in before['facts'] if r.kind == 'usage'}
    assert old['groups'][0]['key']['agent'] == 'unassigned'
    enrolled = actor_candidate_registry(registry, surfaces, site='ownermac')
    collect_registered_sources(enrolled, ledger)
    result = report(ledger)
    assert result['groups'][0]['key'] == {'agent': 'pulse', 'wi': 'unassigned',
        'node': 'ownermac', 'entrypoint': 'unassigned', 'runtime': 'cc'}
    assert result['summary']['usage']['known_total_tokens'] == 38
    assert result['summary']['cost']['partial_machine_cash_usd'] == old['summary']['cost']['partial_machine_cash_usd']
    assert ledger.receipt_snapshot(watermark=before['watermark'])['sha256'] == before['sha256']
    after = {r.fact_ref: r.to_dict() for r in ledger.receipt_snapshot()['facts']}
    assert all(after[k] == v for k, v in originals.items())
    warm = collect_registered_sources(enrolled, ledger)
    assert warm['inserted'] == 0
    assert warm['parser_cache']['parsed_files'] == 0


def test_cc_session_keeps_its_start_folder_after_chdir(tmp_path):
    ledger, registry, surfaces, path, row = setup_source(tmp_path)
    other = deepcopy(row); other['cwd'] += '-copy'
    other['uuid'] = 'other'; other['message']['id'] = 'other-provider-message'
    path.write_text(json.dumps(row) + '\n' + json.dumps(other) + '\n')
    collect_registered_sources(actor_candidate_registry(registry, surfaces, site='ownermac'), ledger)
    groups = report(ledger)['groups']
    assert {g['key']['agent'] for g in groups} == {'pulse'}
    assert report(ledger)['summary']['usage']['event_count'] == 2


def test_cc_session_started_outside_a_workspace_is_not_claimed_by_a_later_folder(tmp_path):
    ledger, registry, surfaces, path, row = setup_source(tmp_path)
    first = deepcopy(row); first['cwd'] = str(tmp_path / 'unregistered')
    first['uuid'] = 'first'; first['message']['id'] = 'first-provider-message'
    path.write_text(json.dumps(first) + '\n' + json.dumps(row) + '\n')
    collect_registered_sources(actor_candidate_registry(registry, surfaces, site='ownermac'), ledger)
    assert {g['key']['agent'] for g in report(ledger)['groups']} == {'unassigned'}


def test_cc_subagent_transcript_uses_the_main_session_start(tmp_path):
    ledger, registry, surfaces, path, row = setup_source(tmp_path)
    child = deepcopy(row); child['cwd'] = str(tmp_path / 'nested-worktree'); child['isSidechain'] = True
    child['uuid'] = 'child'; child['message']['id'] = 'child-provider-message'
    folder = path.parent / 'real-session' / 'subagents'; folder.mkdir(parents=True)
    (folder / 'agent-a1.jsonl').write_text(json.dumps(child) + '\n')
    collect_registered_sources(actor_candidate_registry(registry, surfaces, site='ownermac'), ledger)
    assert {g['key']['agent'] for g in report(ledger)['groups']} == {'pulse'}


def test_cc_actor_evidence_preserves_wi_and_revises_an_old_message_folder_identity(tmp_path):
    ledger, registry, surfaces, path, row = setup_source(tmp_path)
    enrolled = actor_candidate_registry(registry, surfaces, site='ownermac')
    rule = enrolled['sources'][0]['workspace_actors'][0]
    enrolled['sources'][0]['workspace_actors'].append({**rule,
        'workspace_root': '/other/duke-space', 'agent': 'duke',
        'registry_ref': reference('registry', 'duke')})
    other = deepcopy(row); other['cwd'] = '/other/duke-space'
    other['uuid'] = 'other'; other['message']['id'] = 'other-provider-message'
    path.write_text(json.dumps(row) + '\n' + json.dumps(other) + '\n')
    collect_registered_sources(enrolled, ledger)
    usages = [r for r in ledger.receipt_snapshot()['facts'] if r.kind == 'usage']
    target = next(r for r in usages if r.entity_ref == reference('usage', ['real-session', 'other-provider-message']))
    # The earlier collector named the message's own folder; that head is revised.
    old = actor_receipt(target, node='ownermac', executor='duke', workspace_ref=reference('workspace', ['ownermac', '/other/duke-space', 'x']),
                        observed=AT, evidence=(reference('native_cc_message_context', 'old'),))
    wi = 'wi_20261001_297'
    task = Receipt.make('attribution', 'existing-task', {
        'target_ref': target.entity_ref, 'wi': wi, 'executor': None,
        'responsible': None, 'delegator': None, 'state': 'assigned',
        'policy': 'existing_task.v1', 'effective_at': AT},
        observed_at=AT, occurred_at=AT, producer='test')
    ledger.ingest_receipts([old, task])
    assert 'ambiguous' in {g['key']['agent'] for g in report(ledger)['groups']}
    collect_registered_sources(enrolled, ledger)
    groups = report(ledger)['groups']
    assert {g['key']['agent'] for g in groups} == {'pulse'}
    assert wi in {g['key']['wi'] for g in groups}
    assert report(ledger)['summary']['usage']['known_total_tokens'] == 76
    assert collect_registered_sources(enrolled, ledger)['inserted'] == 0


def test_cc_relay_session_binding_names_the_agent_over_the_folder():
    from pulse_compute_cost.receipt_sources import cc_workspace_actor_receipts, relay_cc_executors, usage_receipt
    scope = reference('native_session', ['cc', 'relay-session'])
    execution = Receipt.make('execution', 'attempt', {'wi': None, 'executor': 'duke', 'responsible': None,
        'delegator': None, 'runtime': 'cc', 'assignment_ref': None, 'parent_ref': None,
        'state': 'completed', 'expected_usage': True}, occurred_at=AT, observed_at=AT, producer='relay_host.v1')
    usage = {'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
    host = usage_receipt('cc', ['host', 'm'], usage, at=AT, observed=AT, model='anthropic/test',
        execution_ref=execution.entity_ref, common={'wi': None, 'executor': 'duke', 'responsible': None, 'delegator': None},
        mapping='cc_message.v1', native_scope_ref=scope)
    other = Receipt.make('execution', 'other', {**execution.payload, 'executor': 'pulse'},
        occurred_at=AT, observed_at=AT, producer='relay_host.v1')
    assert relay_cc_executors([execution, host]) == {scope: ('duke', execution.entity_ref)}
    disagreeing = usage_receipt('cc', ['host', 'n'], usage, at=AT, observed=AT, model='anthropic/test',
        execution_ref=other.entity_ref, common={'wi': None, 'executor': 'pulse', 'responsible': None, 'delegator': None},
        mapping='cc_message.v1', native_scope_ref=scope)
    assert relay_cc_executors([execution, host, other, disagreeing]) == {}
    row = {'type': 'assistant', 'sessionId': 'relay-session', 'uuid': 'u', 'cwd': '/registered/pulse',
           'timestamp': AT, 'message': {'id': 'm', 'model': 'anthropic/test', 'stop_reason': 'end_turn', 'usage': usage}}
    native = usage_receipt('cc', ['relay-session', 'm'], usage, at=AT, observed=AT, model='anthropic/test',
        final=True, mapping='cc_message.v1', native_scope_ref=scope)
    source = {'node': 'ownermac', 'workspace_actors': [{'workspace_root': '/registered/pulse', 'agent': 'pulse',
        'node': 'ownermac', 'runtime': 'cc', 'registry_ref': reference('registry', 'pulse')}]}
    data = (json.dumps(row) + '\n').encode()
    path = Path('/nonexistent/relay-session.jsonl')
    bound = cc_workspace_actor_receipts(path, data, source, [native], AT,
        relay_executors={scope: ('duke', execution.entity_ref)})
    assert [r.payload['executor'] for r in bound] == ['duke']
    assert [r.payload['executor'] for r in cc_workspace_actor_receipts(path, data, source, [native], AT)] == ['pulse']

    # A request already recorded both for the Relay Agent and, after a chdir,
    # for the folder's Agent: only the contrary head is revised.
    agreeing = actor_receipt(native, node='ownermac', executor='duke', observed=AT, evidence=(reference('old', 'duke'),))
    agreeing = Receipt.make('attribution', None, agreeing.payload, entity_ref=reference('old', 'a'),
        occurred_at=AT, observed_at=AT, producer=agreeing.producer, evidence=agreeing.evidence)
    contrary = Receipt.make('attribution', None, {**agreeing.payload, 'executor': 'pulse'},
        entity_ref=reference('old', 'b'), occurred_at=AT, observed_at=AT, producer=agreeing.producer,
        evidence=(reference('old', 'pulse'),))
    revised = cc_workspace_actor_receipts(path, data, source, [native], AT,
        relay_executors={scope: ('duke', execution.entity_ref)},
        prior_actors={native.entity_ref: [agreeing, contrary]})
    assert [(r.entity_ref, r.supersedes, r.payload['executor']) for r in revised] == [
        (contrary.entity_ref, contrary.fact_ref, 'duke')]


def test_cc_split_content_retains_first_actor_boundary(tmp_path):
    ledger, registry, surfaces, path, first = setup_source(tmp_path)
    first['message']['stop_reason'] = None
    first['message']['usage']['output_tokens'] = 1
    last = deepcopy(first)
    last['uuid'] = 'next-content-block'
    last['timestamp'] = '2026-10-02T10:00:02Z'
    last['message']['stop_reason'] = 'end_turn'
    last['message']['usage']['output_tokens'] = 3
    path.write_text(json.dumps(first) + '\n' + json.dumps(last) + '\n')
    collect_registered_sources(actor_candidate_registry(registry, surfaces, site='ownermac'), ledger)
    result = report(ledger)
    assert result['summary']['usage']['event_count'] == 1
    assert result['groups'][0]['key']['agent'] == 'pulse'
    assert result['summary']['usage']['known_total_tokens'] == 38
    original = ledger.receipt_snapshot()
    collect_registered_sources(actor_candidate_registry(registry, surfaces, site='ownermac'), ledger)
    assert ledger.receipt_snapshot()['watermark'] == original['watermark']


def test_incremental_cc_targets_match_a_full_rescan():
    from pulse_compute_cost.receipt_sources import _cc_usage_targets

    def usage(entity, at, mapping='cc_message.v1', kind='usage'):
        body = json.dumps({'mapping': mapping}).encode()
        return Receipt(kind, entity, entity + str(at), at, AT, 'p', (), None, body)

    batches = [[usage('a', '2026-10-02T10:05:00Z'), usage('b', '2026-10-02T10:01:00Z')],
               [usage('a', '2026-10-02T10:02:00Z'), usage('c', None), usage('d', '2026-10-02T10:00:00Z', 'pi')],
               [usage('b', '2026-10-02T10:01:00Z'), usage('a', '2026-10-02T10:09:00Z'),
                usage('e', '2026-10-02T10:03:00Z', kind='attribution')]]
    facts, cache = [], {}
    for batch in batches:
        facts.extend(batch)
        incremental = _cc_usage_targets(facts, cache)
        assert incremental == _cc_usage_targets(facts, None)
    # Earliest per entity; of equal times the first seen stays.
    assert {k: v.fact_ref for k, v in incremental.items()} == {
        'a': 'a2026-10-02T10:02:00Z', 'b': 'b2026-10-02T10:01:00Z'}
    assert incremental['b'] is batches[0][1]
    # A different list is never served from another list's index.
    assert _cc_usage_targets(batches[0], cache) == _cc_usage_targets(batches[0], None)
