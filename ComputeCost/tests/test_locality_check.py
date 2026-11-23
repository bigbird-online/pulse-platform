"""Offline locality check: zero differences on a synthetic ledger; a dropped link key is named."""
import contextlib
import io
import json
import os
import random
import sqlite3

from pulse_compute_cost import session_packages
from pulse_compute_cost.cli import main as cli_main
from pulse_compute_cost.locality_check import PHASES

from support.synthetic_ledger import generate, pi_turn, split, write


def ledger(tmp_path, seed, sessions, extra=()):
    rows = generate(seed, sessions=sessions) + list(extra)
    write(tmp_path / 'ledger', split(rows, random.Random(seed)))
    registry = tmp_path / 'sources.json'
    registry.write_text(json.dumps({'ledger_root': str(tmp_path / 'ledger'), 'sources': [],
                                    'required_receipt_features': ['non_wi_activity.v1', 'runtime_subcall.v1']}))
    os.chmod(registry, 0o600)
    watermark = len(list((tmp_path / 'ledger' / 'receipts').glob('*.json')))
    return rows, registry, watermark


def check(registry, watermark, out, phase):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = cli_main(['locality-check', '--registry', str(registry), '--watermark', str(watermark),
                         '--out', str(out), '--phase', phase])
    assert code == 0
    return json.loads(stdout.getvalue())


def test_packages_and_stages_equal_the_whole_ledger(tmp_path):
    rows, registry, watermark = ledger(tmp_path, 3, 24)
    out = tmp_path / 'out'
    results = {phase: check(registry, watermark, out, phase) for phase in PHASES}
    assert results['index']['facts'] == len(rows) and results['index']['packages'] > 1
    assert results['full']['rows'] > 0
    assert results['packages']['row_diffs'] == 0 and not results['packages']['global_diffs']
    assert results['packages']['unplaced_rows'] == 0
    assert all(p['peak_bytes'] > 0 for p in results['packages']['probes'].values())
    assert results['stages']['diff_counts'] == {}
    # The comparison is not vacuous: the stages produced claims to compare.
    assert results['stages']['produced']['turn_residual_receipts']
    assert results['stages']['produced']['standing_non_wi_receipts']
    report = json.loads((out / 'locality-report.json').read_bytes())
    assert report['gates']['packages_zero_diff'] and report['gates']['stages_zero_diff']
    assert report['gates']['probe_peak_within_limit']
    assert results['report']['gates'] == report['gates']
    assert all(oct(p.stat().st_mode)[-3:] == '600' for p in out.iterdir() if p.is_file())


def test_a_dropped_link_key_is_reported_with_the_missing_reference(tmp_path, monkeypatch):
    rows, registry, watermark = ledger(tmp_path, 4, 12)
    original = session_packages.link_nodes

    def without_execution(row):
        nodes = original(row)
        if row.kind == 'usage' and row.payload['execution_ref']:
            nodes = tuple(n for n in nodes if n != session_packages.node(row.payload['execution_ref']))
        return nodes

    monkeypatch.setattr(session_packages, 'link_nodes', without_execution)
    out = tmp_path / 'out'
    for phase in ('index', 'full', 'packages'):
        result = check(registry, watermark, out, phase)
    assert result['row_diffs'] > 0
    diffs = [json.loads(line) for line in (out / 'packages-diffs.jsonl').read_text().splitlines()]
    counters = {r.entity_ref: r.payload['execution_ref'] for r in rows
                if r.kind == 'usage' and r.payload['granularity'] == 'turn_aggregate'}
    named = [d for d in diffs if d['entity'] in counters]
    assert named, diffs[:3]
    for diff in named:
        # The counter lost its execution's task claim; the list names that execution.
        assert diff['field'] in {'projected', 'row'}
        assert any(ref.startswith(counters[diff['entity']] + '@') for ref in diff['foreign_refs']['execution_ref'])


def package_diffs(registry, watermark, out):
    for phase in ('index', 'full', 'packages'):
        result = check(registry, watermark, out, phase)
    diffs = [json.loads(line) for line in (out / 'packages-diffs.jsonl').read_text().splitlines()]
    return result, diffs


def test_a_host_execution_and_native_requests_joined_only_by_their_input_share_a_package(tmp_path, monkeypatch):
    """G1 on the real ledger: 105 differing rows were Pi requests whose Host execution lay in another package."""
    turn = pi_turn('input-only', 900)
    rows, registry, watermark = ledger(tmp_path, 21, 6, turn)
    result, diffs = package_diffs(registry, watermark, tmp_path / 'out')
    assert result['row_diffs'] == 0 and not result['global_diffs'] and result['unplaced_rows'] == 0
    full = sqlite3.connect(tmp_path / 'out' / 'full.sqlite')
    requests = [r.entity_ref for r in turn if r.kind == 'usage']
    for ref in requests:  # The full judgement did join them: the comparison is not vacuous.
        record = json.loads(full.execute('SELECT record FROM result WHERE entity = ?', (ref,)).fetchone()[0])
        assert json.loads(record['row'])['payload']['execution_ref'] == turn[0].entity_ref
    monkeypatch.setattr(session_packages, 'input_node', lambda context: None)
    result, diffs = package_diffs(registry, watermark, tmp_path / 'unlinked')
    assert {d['entity'] for d in diffs} == set(requests)
    assert {d['field'] for d in diffs} == {'actor_evidence', 'projected', 'row'}


def test_a_binding_alias_is_read_with_every_package(tmp_path, monkeypatch):
    """G1 on the real ledger: 48 differing rows compared assignments through an alias claim in another package."""
    turn = pi_turn('aliased', 900, alias=True)
    rows, registry, watermark = ledger(tmp_path, 22, 6, turn)
    result, diffs = package_diffs(registry, watermark, tmp_path / 'out')
    assert result['row_diffs'] == 0 and not result['global_diffs'] and result['unplaced_rows'] == 0
    index = json.loads((tmp_path / 'out' / 'index.json').read_bytes())
    monkeypatch.setattr(session_packages, 'is_shared', lambda row: False)
    result, diffs = package_diffs(registry, watermark, tmp_path / 'unshared')
    usages = {r.entity_ref for r in turn if r.kind == 'usage'}
    assert {d['entity'] for d in diffs} == usages
    assert {'conflict', 'conflict_reasons', 'reconciled', 'suppressed'} <= {d['field'] for d in diffs}
    # The alias was a package of its own; shared, it is one more public fact.
    unshared = json.loads((tmp_path / 'unshared' / 'index.json').read_bytes())
    assert (index['packages'], index['public_package']) == (unshared['packages'] - 1, unshared['public_package'] + 1)


def test_a_probe_reports_its_own_peak_not_the_parent_process_peak(tmp_path):
    """G1 on the real ledger: every probe reported the 3.4 GB the parent had reached before starting it."""
    from pulse_compute_cost.fact_index import FactIndex
    from pulse_compute_cost.locality_check import peak_bytes, probe
    _, registry, watermark = ledger(tmp_path, 23, 4)
    out = tmp_path / 'out'
    check(registry, watermark, out, 'index')
    identity = json.loads((out / 'index.json').read_bytes())['binding']['stream_identity']
    index = FactIndex.open(out / 'fact-index.sqlite', identity)
    ballast = bytearray(300 * 1024 * 1024)
    ballast[::4096] = b'x' * len(ballast[::4096])  # Touch every page: the parent's peak now exceeds 300 MB.
    measured = probe(index, identity, index.packages()[0][0])
    assert 0 < measured['peak_bytes'] < peak_bytes() - len(ballast) // 2


def run_cli(registry, watermark, out, phase):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli_main(['locality-check', '--registry', str(registry), '--watermark', str(watermark),
                         '--out', str(out), '--phase', phase])


def test_phases_of_different_ledger_prefixes_never_form_a_report(tmp_path):
    """Review repro: same watermark, other prefix; A's full/packages with B's index/stages must not pass."""
    _, registry_a, watermark = ledger(tmp_path / 'a', 5, 10)
    rows_b = generate(6, sessions=10)
    size = -(-len(rows_b) // watermark)
    write(tmp_path / 'b' / 'ledger', [rows_b[i:i + size] for i in range(0, len(rows_b), size)])
    assert len(list((tmp_path / 'b' / 'ledger' / 'receipts').glob('*.json'))) == watermark
    registry_b = tmp_path / 'b' / 'sources.json'
    registry_b.write_text(json.dumps({'ledger_root': str(tmp_path / 'b' / 'ledger'), 'sources': []}))
    os.chmod(registry_b, 0o600)
    out_a, out_b = tmp_path / 'out-a', tmp_path / 'out-b'
    for phase in PHASES:
        assert run_cli(registry_a, watermark, out_a, phase) == 0
        assert run_cli(registry_b, watermark, out_b, phase) == 0
    mixed = tmp_path / 'mixed'
    mixed.mkdir(mode=0o700)
    for name, source in (('index.json', out_b), ('fact-index.sqlite', out_b), ('stages.json', out_b),
                         ('full.json', out_a), ('full.sqlite', out_a), ('packages.json', out_a)):
        (mixed / name).write_bytes((source / name).read_bytes())
        os.chmod(mixed / name, 0o600)
    assert run_cli(registry_b, watermark, mixed, 'report') != 0
    assert not (mixed / 'locality-report.json').exists()
    # Each phase that reads another phase's output checks it judged the same prefix.
    assert run_cli(registry_b, watermark, mixed, 'packages') != 0
    assert run_cli(registry_a, watermark, mixed, 'stages') != 0


def test_cli_reads_the_registered_ledger_read_only(tmp_path, monkeypatch):
    from pulse_compute_cost import observation_authority
    _, registry, watermark = ledger(tmp_path, 7, 4)
    seen = []
    original = observation_authority.registered_ledger

    def spy(registry, **kwargs):
        seen.append(kwargs)
        return original(registry, **kwargs)

    monkeypatch.setattr(observation_authority, 'registered_ledger', spy)
    assert run_cli(registry, watermark, tmp_path / 'out', 'index') == 0
    assert seen == [{'read_only': True}]


def test_stages_settle_actor_claims_exactly_as_the_routine_collection(tmp_path):
    """Review repro: a tenant membership on a revised usage supersedes the old one, as collection writes it."""
    from datetime import datetime, timezone
    from pulse_compute_cost.ledger import UsageLedger
    from pulse_compute_cost.locality_check import run_stages
    from pulse_compute_cost.receipt_sources import actor_receipt, collect_registered_sources, usage_receipt
    from pulse_compute_cost.receipts import Receipt, reference
    from pulse_compute_cost.tenant_context import FEATURE, SCHEMA
    start, end = '2026-09-15T01:00:00Z', '2026-09-15T01:01:00Z'
    scope = reference('native_session', 'tenant-scope')
    usage = usage_receipt('codex', 'one', {'input_tokens': 120, 'cached_input_tokens': 20,
                          'cache_write_input_tokens': 0, 'output_tokens': 10}, at=end, observed=end,
                          model='openai/test', mapping='codex_cli.v1', interval=(start, end), native_scope_ref=scope)
    actor = actor_receipt(usage, node='ownermac', executor='shared', entrypoint='cli', observed=end,
                          evidence=(reference('workspace', 'w'),))
    tenants = tmp_path / 'tenants.json'
    tenants.write_text(json.dumps({'schema': SCHEMA, 'bindings': [{
        'tenant_id': 'team-a', 'tenant_name': 'Team A', 'agent': 'shared', 'node': 'ownermac', 'runtime': 'codex',
        'native_scope_ref': scope, 'effective_from': start, 'effective_to': '2026-09-16T00:00:00Z'}]}))
    os.chmod(tenants, 0o600)
    registry = {'schema': 'computecost_source_registry/v1', 'ledger_root': str(tmp_path / 'ledger'), 'sources': [],
                'required_receipt_features': [FEATURE], 'tenant_registry_path': str(tenants)}
    ledger = UsageLedger(tmp_path / 'ledger', clock=lambda: datetime(2026, 9, 15, 4, tzinfo=timezone.utc))
    ledger.ingest_receipts([usage, actor])
    collect_registered_sources(registry, ledger)  # Membership of the first usage version.
    revised = dict(usage.payload, tokens=dict(usage.payload['tokens'], output=11))
    ledger.ingest_receipts([Receipt.make('usage', None, revised, occurred_at=usage.occurred_at, observed_at=end,
                                         producer=usage.producer, entity_ref=usage.entity_ref,
                                         supersedes=usage.fact_ref)])
    before = ledger.receipt_snapshot()
    known = {r.fact_ref for r in before['facts']}
    offline = {r.fact_ref: r for rows in run_stages(before['facts'], registry, end).values() for r in rows
               if r.fact_ref not in known}
    collect_registered_sources(registry, ledger)
    appended = {r.fact_ref: r for r in ledger.receipt_snapshot()['facts'] if r.fact_ref not in known}
    memberships = [r for r in appended.values() if r.payload.get('context', {}).get('tenant')]
    assert memberships and all(r.supersedes in known for r in memberships)
    assert set(offline) == set(appended)


def test_report_rejects_stage_results_of_another_configuration(tmp_path):
    """Review repro: same ledger and watermark, runtime_subcall.v1 turned off after stages ran."""
    _, registry, watermark = ledger(tmp_path, 11, 8)
    out = tmp_path / 'out'
    for phase in PHASES:
        assert run_cli(registry, watermark, out, phase) == 0
    document = json.loads(registry.read_text())
    document['required_receipt_features'] = ['non_wi_activity.v1']
    registry.write_text(json.dumps(document))
    assert run_cli(registry, watermark, out, 'report') != 0
    assert run_cli(registry, watermark, out, 'stages') == 0
    assert run_cli(registry, watermark, out, 'report') == 0


def test_offline_phases_create_nothing_in_a_local_ledger_root(tmp_path):
    """Review repro: no lock file appears in a ledger root that has none."""
    _, registry, watermark = ledger(tmp_path, 12, 4)
    root = tmp_path / 'ledger'
    (root / '.usage-ledger.lock').unlink()
    listing = sorted(str(p.relative_to(root)) for p in root.rglob('*'))
    for phase in PHASES:
        assert run_cli(registry, watermark, tmp_path / 'out', phase) == 0
    assert sorted(str(p.relative_to(root)) for p in root.rglob('*')) == listing


def test_report_checks_the_registered_ledger_still_holds_the_judged_prefix(tmp_path):
    """Review repro: after a passing run the registry's ledger holds another prefix at the same watermark."""
    import shutil
    _, registry, watermark = ledger(tmp_path, 13, 8)
    out = tmp_path / 'out'
    for phase in PHASES:
        assert run_cli(registry, watermark, out, phase) == 0
    (out / 'locality-report.json').unlink()
    other = generate(14, sessions=8)
    shutil.rmtree(tmp_path / 'ledger' / 'receipts')
    size = -(-len(other) // watermark)
    write(tmp_path / 'ledger', [other[i:i + size] for i in range(0, len(other), size)])
    assert len(list((tmp_path / 'ledger' / 'receipts').glob('*.json'))) == watermark
    assert run_cli(registry, watermark, out, 'report') != 0
    assert not (out / 'locality-report.json').exists()


def test_output_inside_the_ledger_root_is_refused_before_anything_is_created(tmp_path):
    _, registry, watermark = ledger(tmp_path, 15, 4)
    root = tmp_path / 'ledger'
    listing = sorted(str(p) for p in root.rglob('*'))
    for out in (root, root / 'derived', tmp_path / 'alias' / 'derived'):
        if out.parent.name == 'alias':
            (tmp_path / 'alias').symlink_to(root)
        assert run_cli(registry, watermark, out, 'index') != 0
    assert sorted(str(p) for p in root.rglob('*')) == listing


def test_stage_inputs_bind_the_rules_as_the_stage_admits_them(tmp_path, monkeypatch):
    """Review repro: a symlink to the same bytes makes the stage read no rules; old stages must not pass."""
    from pulse_compute_cost import standing_non_wi
    copy = tmp_path / 'standing-non-wi.json'
    copy.write_bytes(standing_non_wi.RULES.read_bytes())
    link = tmp_path / 'standing-link.json'
    link.symlink_to(copy)

    def use(path):
        monkeypatch.setattr(standing_non_wi, 'RULES', path)
        monkeypatch.setattr(standing_non_wi.load_rules, '__defaults__', (path,))

    use(copy)
    _, registry, watermark = ledger(tmp_path, 16, 12)
    out = tmp_path / 'out'
    for phase in PHASES:
        assert run_cli(registry, watermark, out, phase) == 0
    assert json.loads((out / 'stages.json').read_bytes())['produced']['standing_non_wi_receipts']
    use(link)
    assert run_cli(registry, watermark, out, 'report') != 0
    assert run_cli(registry, watermark, out, 'stages') == 0
    assert json.loads((out / 'stages.json').read_bytes())['produced']['standing_non_wi_receipts'] == 0
    assert run_cli(registry, watermark, out, 'report') == 0
