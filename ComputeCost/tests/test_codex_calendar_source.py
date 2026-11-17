"""One admitted Codex source follows new months without changing its identity."""

import copy
import json
from pathlib import Path
import pytest
from pulse_compute_cost.receipt_sources import collect_registered_sources
from pulse_compute_cost.receipts import current_receipts
import test_scl_session_attribution as f


@pytest.fixture
def setup_calendar():
    base = f.SessionAttributionTests()
    base.setUp()
    root = base.root / "sessions"
    first = root / "2026" / "01"
    first.mkdir(parents=True)
    archive = base.root / "archive"
    archive.mkdir()
    for path in base.logs.glob("*.jsonl"):
        path.rename(first / path.name)
    source = {k: v for k, v in base.source.items() if k != "path"}
    source.update(
        paths=[str(first), str(archive)],
        layout="codex_calendar_months.v1",
        calendar_root=str(root),
        calendar_from="2026-01",
    )
    registry = {**base.registry, "sources": [source]}
    try:
        yield base, root, source, registry
    finally:
        base.doCleanups()


def test_new_month_is_collected_without_registry_edit(setup_calendar):
    base, root, source, registry = setup_calendar
    before = copy.deepcopy(registry)
    collect_registered_sources(registry, base.ledger)
    snap = base.ledger.receipt_snapshot()
    mark = snap["watermark"]
    original = [
        r.to_dict() for r in current_receipts(snap["facts"])[0] if r.kind == "usage"
    ]
    assert len(original) == 1
    month = root / "2026" / "02"
    month.mkdir()
    added = base.native(f.SECOND, "2026-02-01T01:00:00Z", "2026-02-01T01:01:00Z")
    added.rename(month / added.name)
    collect_registered_sources(registry, base.ledger)
    current = [
        r.to_dict()
        for r in current_receipts(base.ledger.receipt_snapshot()["facts"])[0]
        if r.kind == "usage"
    ]
    assert len(current) == 2
    assert all(r in current for r in original)
    assert base.ledger.receipt_snapshot(watermark=mark)["sha256"] == snap["sha256"]
    assert collect_registered_sources(registry, base.ledger)["inserted"] == 0
    assert registry == before


def test_calendar_crosses_year_and_does_not_import_earlier_months(setup_calendar):
    from pulse_compute_cost.source_layout import codex_calendar_paths

    _, root, source, _ = setup_calendar
    (root / "2025" / "12").mkdir(parents=True)
    future = root / "2027" / "01"
    future.mkdir(parents=True)
    paths = codex_calendar_paths(source)
    assert future in paths
    assert root / "2025" / "12" not in paths
    assert set(map(Path, source["paths"])) <= set(paths)


@pytest.mark.parametrize(
    "change",
    [
        {"calendar_from": "2026-13"},
        {"calendar_from": "2025-12"},
        {"calendar_root": "relative"},
        {"kind": "pi"},
        {"layout": "registered_recursive.v1"},
        {"runtime_manifest": "/unrelated"},
    ],
)
def test_calendar_rejects_unbound_or_invalid_selector(setup_calendar, change):
    from pulse_compute_cost.source_layout import codex_calendar_paths

    _, _, source, _ = setup_calendar
    with pytest.raises((ValueError, TypeError)):
        codex_calendar_paths({**source, **change})


def test_calendar_rejects_symlink_month(setup_calendar):
    from pulse_compute_cost.source_layout import codex_calendar_paths

    base, root, source, _ = setup_calendar
    (root / "2026" / "02").symlink_to(base.root / "archive", target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe native calendar month"):
        codex_calendar_paths(source)


def test_source_registration_cannot_add_an_overlapping_future_month(setup_calendar):
    from pulse_compute_cost.source_registration import candidate_registry

    base, root, source, registry = setup_calendar
    month = root / "2026" / "02"
    month.mkdir()
    with pytest.raises(ValueError, match="overlapping native roots"):
        candidate_registry(
            {**registry, "ledger_root": str(base.ledger.root)}, {"codex": [str(month)]}
        )


def installer():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "Stats/deploy.py"
    spec = importlib.util.spec_from_file_location("calendar_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enrollment_keeps_identity_ledger_and_mcp_boundary(setup_calendar):
    base, root, source, registry = setup_calendar
    old = {**registry, "ledger_root": str(base.ledger.root)}
    original = copy.deepcopy(old)
    old["sources"][0] = {
        k: v
        for k, v in source.items()
        if k not in ("layout", "calendar_root", "calendar_from")
    }
    old["sources"][0]["counter_dependency_paths"] = [str(root)]
    before = copy.deepcopy(old)
    deploy = installer()
    result = deploy.session_wi_source_registry(
        old, source["id"], f.BEFORE, calendar_root=str(root), calendar_from="2026-01"
    )
    assert old == before
    assert result["ledger_root"] == original["ledger_root"]
    assert result["sources"][0]["id"] == source["id"]
    assert result["sources"][0]["paths"] == source["paths"]
    assert (
        deploy.session_wi_source_registry(
            result,
            source["id"],
            f.BEFORE,
            calendar_root=str(root),
            calendar_from="2026-01",
        )
        == result
    )
    with pytest.raises(ValueError, match="immutable"):
        deploy.session_wi_source_registry(
            result,
            source["id"],
            f.BEFORE,
            calendar_root=str(root),
            calendar_from="2026-02",
        )
    with pytest.raises(ValueError, match="rollback cannot erase"):
        deploy.require_retained_purpose_cutover(result, before)


def test_calendar_enrollment_requires_already_registered_root(setup_calendar):
    _, root, source, registry = setup_calendar
    with pytest.raises(ValueError, match="already be an enrolled native dependency"):
        installer().session_wi_source_registry(
            registry,
            source["id"],
            f.BEFORE,
            calendar_root=str(root),
            calendar_from="2026-01",
        )


def test_reader_feature_gate_rejects_old_calendar_reader(setup_calendar, monkeypatch):
    _, root, _, registry = setup_calendar
    deploy = installer()
    monkeypatch.setattr(deploy, "run", lambda *a, **kw: json.dumps("old").encode())
    with pytest.raises(ValueError, match="cannot read enrolled native calendar"):
        deploy.verify_reader_features(root, registry)
    monkeypatch.setattr(
        deploy, "run", lambda *a, **kw: json.dumps("codex_calendar_months.v1").encode()
    )
    deploy.verify_reader_features(root, registry)


def test_enrollment_works_from_isolated_installer_interpreter(setup_calendar):
    import subprocess
    import sys

    _, root, source, registry = setup_calendar
    registry = copy.deepcopy(registry)
    registry["sources"][0]["counter_dependency_paths"] = [str(root)]
    path = Path(__file__).resolve().parents[2] / "Stats/deploy.py"
    code = (
        "import importlib.util,json,sys;"
        's=importlib.util.spec_from_file_location("deploy",sys.argv[1]);'
        "d=importlib.util.module_from_spec(s);s.loader.exec_module(d);"
        "r=d.session_wi_source_registry(json.loads(sys.argv[2]),sys.argv[3],"
        '"2026-01-01T00:00:00Z",calendar_root=sys.argv[4],calendar_from="2026-01");'
        "print(json.dumps(r))"
    )
    result = json.loads(
        subprocess.check_output(
            [
                sys.executable,
                "-I",
                "-c",
                code,
                str(path),
                json.dumps(registry),
                source["id"],
                str(root),
            ],
            text=True,
        )
    )
    assert result["sources"][0]["id"] == source["id"]
    assert result["sources"][0]["calendar_from"] == "2026-01"
