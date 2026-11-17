"""A-31: real consumers share bounded SSOT diagnostics, without lifecycle writes."""

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest
import yaml

from harness_rewrite.ssot import (
    MAX_BYTES, Agent, ConfigurationError, Entry, Projection, Registry,
    inspect_registry, validate_dependencies,
)


def fixture(root):
    (root / "docs").mkdir()
    (root / "docs/base.md").write_text("base")
    (root / "docs/top.md").write_text("top")
    (root / "copy.md").write_text("top")
    (root / "link.md").symlink_to("docs/base.md")
    (root / "agent").mkdir()
    (root / "agent/IDENTITY.md").write_text("identity")
    return Registry("2.0", {
        "base": Entry("docs/base.md", "document", projections=(
            Projection("link.md", "symlink"),
        )),
        "top": Entry("docs/top.md", "document", ("base",), (
            Projection("copy.md", "reference"),
        )),
    }, {"worker": Agent("agent", "agent/IDENTITY.md", ("top",))})


def observe(root, registry=None, **kwargs):
    return inspect_registry(registry or fixture(root), root, **kwargs)


def row(report, check, target=None):
    return next(item for item in report["observations"]
                if item["check_id"] == check and (target is None or item["target"] == target))


def test_transitive_selection_and_quick_mode_for_every_selector(tmp_path):
    reg = fixture(tmp_path)
    for selector in ({}, {"agent": "worker"}, {"entry": "top"}):
        full = observe(tmp_path, reg, **selector)
        assert full["verdict"] == "pass"
        assert full["selected_entries"] == ["base", "top"]
        assert full["counts"]["passed"] == len(full["observations"])
        quick = observe(tmp_path, reg, mode="quick", **selector)
        assert quick["skipped_checks"] == ["C1", "C2"]
        assert not any(r["check_id"].startswith("C") for r in quick["observations"])
    (tmp_path / "docs/base.md").unlink()
    assert observe(tmp_path, reg, agent="worker")["verdict"] == "fail"
    assert row(observe(tmp_path, reg, entry="top"), "S1", "docs/base.md")["status"] == "violated"


@pytest.mark.parametrize("options", [
    {"mode": "bogus"}, {"agent": "missing"}, {"entry": "missing"},
    {"agent": "worker", "entry": "top"},
])
def test_bad_selectors_reject_before_filesystem_observation(tmp_path, monkeypatch, options):
    reg = fixture(tmp_path)
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("must validate first"))
    with pytest.raises(ConfigurationError):
        observe(tmp_path, reg, **options)


@pytest.mark.parametrize("change", [
    {"schema": "99"},
    {"entries": {"a": Entry("x", "unknown")}},
    {"entries": {"a": Entry("x", "document", ("absent",))}},
    {"entries": {"a": Entry("x", "document", ("b",)), "b": Entry("y", "document", ("a",))}},
    {"entries": {"a": Entry("../outside", "document")}},
    {"entries": {"a": Entry("/absolute", "document")}},
    {"entries": {"a": Entry("x", "document", projections=(Projection("x", "magic"),))}},
    {"entries": {"a": Entry("x", "document", projections=(Projection("../x", "embed"),))}},
])
def test_invalid_configuration_rejected(tmp_path, change):
    reg = Registry("2.0", {}, {})
    with pytest.raises(ConfigurationError):
        observe(tmp_path, replace(reg, **change))


def test_dependency_validation_is_iterative_and_checks_unselected_rows(tmp_path):
    entries = {str(i): (str(i - 1),) if i else () for i in range(2000)}
    validate_dependencies(entries, {})
    with pytest.raises(ConfigurationError, match="Circular"):
        validate_dependencies({"a": ("b",), "b": ("a",)}, {})
    reg = fixture(tmp_path)
    reg = replace(reg, entries={**reg.entries, "bad": Entry("bad", "document", ("absent",))})
    with pytest.raises(ConfigurationError):
        observe(tmp_path, reg, entry="base")


@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "fifo"])
def test_document_missing_empty_and_special_files_are_findings(tmp_path, kind):
    path = tmp_path / "source"
    if kind == "empty":
        path.write_bytes(b"")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    reg = Registry("1.0", {"a": Entry("source", "document")}, {})
    report = observe(tmp_path, reg)
    assert report["verdict"] == "fail"
    assert row(report, "S1")["status"] == "violated"


@pytest.mark.parametrize("method", ["reference", "embed"])
def test_content_comparison_and_quick_has_no_content_reads(tmp_path, method, monkeypatch):
    reg = Registry("1.0", {"a": Entry("source", "document", projections=(
        Projection("copy", method),
    ))}, {})
    (tmp_path / "source").write_bytes(b"original")
    (tmp_path / "copy").write_bytes(b"different")
    report = observe(tmp_path, reg)
    assert row(report, "C2")["reason"] == "content_mismatch"
    assert "original" not in json.dumps(report)
    assert "different" not in json.dumps(report)
    monkeypatch.setattr(os, "read", lambda *a: pytest.fail("quick must not read content"))
    assert observe(tmp_path, reg, entry="a", mode="quick")["verdict"] == "pass"


@pytest.mark.parametrize("kind", ["wrong", "plain", "broken", "outside", "directory"])
def test_projection_link_failures_do_not_pass_or_read_outside(tmp_path, kind, monkeypatch):
    reg = fixture(tmp_path)
    link = tmp_path / "link.md"
    link.unlink()
    if kind == "wrong":
        link.symlink_to("docs/top.md")
    elif kind == "plain":
        link.write_bytes(b"base")
    elif kind == "directory":
        link.mkdir()
    elif kind == "broken":
        link.symlink_to("missing")
    else:
        outside = tmp_path.parent / (tmp_path.name + "-private")
        outside.write_bytes(b"PRIVATE_SECRET")
        link.symlink_to(outside)
    opened = []
    original = os.open
    def capture(path, *args, **kwargs):
        opened.append(str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", capture)
    report = observe(tmp_path, reg, entry="base")
    assert row(report, "C1")["status"] == "violated"
    assert "PRIVATE_SECRET" not in json.dumps(report)
    if kind == "outside":
        assert row(report, "C1")["reason"] == "path_outside_root"
        assert not any("-private" in path for path in opened)


def test_unsupported_package_content_is_explicit_unknown(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "copy").write_bytes(b"x")
    reg = Registry("1.0", {"a": Entry("source", "package", projections=(
        Projection("copy", "embed"),
    ))}, {})
    report = observe(tmp_path, reg)
    assert report["verdict"] == "unknown"
    assert row(report, "C2")["reason"] == "unsupported_package_comparison"
    assert report["summary"]["unknown"] == 1
    assert report["findings"][0]["severity"] == "major"


def test_large_content_is_unknown(tmp_path):
    reg = fixture(tmp_path)
    (tmp_path / "copy.md").write_bytes(b"x" * (MAX_BYTES + 1))
    report = observe(tmp_path, reg)
    assert report["verdict"] == "unknown"
    assert row(report, "C2")["reason"] == "content_size_limit"


def test_permission_failure_is_unknown_without_exception_text(tmp_path, monkeypatch):
    reg = fixture(tmp_path)
    original = os.open
    def denied(path, *args, **kwargs):
        if str(path) == "copy.md":
            raise PermissionError("PRIVATE_ERROR")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", denied)
    report = observe(tmp_path, reg)
    assert report["verdict"] == "unknown"
    assert report["counts"]["unknown"] == 2
    assert "PRIVATE_ERROR" not in json.dumps(report)


def test_content_mutation_during_read_is_unknown(tmp_path, monkeypatch):
    reg = fixture(tmp_path)
    original = os.read
    changed = False
    def change(fd, count):
        nonlocal changed
        data = original(fd, count)
        if not changed and data == b"top":
            (tmp_path / "docs/top.md").write_bytes(b"changed-source")
            changed = True
        return data
    monkeypatch.setattr(os, "read", change)
    report = observe(tmp_path, reg)
    assert row(report, "C2")["reason"] == "content_changed_during_read"
    assert report["verdict"] == "unknown"


@pytest.mark.parametrize(("contents", "status", "reason"), [
    ("agent_id: worker", "passed", "policy_identity_matches"),
    ("agent_id: PRIVATE_OTHER", "violated", "agent_identity_mismatch"),
    ("[PRIVATE_BAD", "unknown", "policy_decode_failed"),
    ("agent_id: 7", "unknown", "policy_identity_unavailable"),
    ("other: PRIVATE_VALUE", "unknown", "policy_identity_unavailable"),
])
def test_policy_diagnostics_do_not_expose_values(tmp_path, contents, status, reason):
    reg = fixture(tmp_path)
    (tmp_path / "policy.yaml").write_text(contents)
    reg = replace(reg, agents={"worker": replace(reg.agents["worker"], policy_path="policy.yaml")})
    report = observe(tmp_path, reg, decode_policy=yaml.safe_load)
    result = row(report, "S6")
    assert result["status"] == status
    assert result["reason"] == reason
    assert "PRIVATE_" not in json.dumps(report)


def test_policy_absence_no_decoder_and_retired_agent(tmp_path):
    reg = fixture(tmp_path)
    reg = replace(reg, agents={"worker": replace(
        reg.agents["worker"], policy_path="policy.yaml", retired=True,
    )})
    assert row(observe(tmp_path, reg), "S6")["status"] == "violated"
    (tmp_path / "policy.yaml").write_text("agent_id: worker")
    assert row(observe(tmp_path, reg), "S6")["status"] == "unknown"
    report = observe(tmp_path, reg, decode_policy=yaml.safe_load)
    assert row(report, "S6")["reason"] == "retired_agent_has_policy"
    assert row(report, "S6")["severity"] == "minor"


def test_critical_violation_dominates_but_unknown_count_is_retained(tmp_path):
    reg = fixture(tmp_path)
    (tmp_path / "agent/IDENTITY.md").unlink()
    (tmp_path / "copy.md").write_bytes(b"x" * (MAX_BYTES + 1))
    report = observe(tmp_path, reg)
    assert report["verdict"] == "fail"
    assert report["counts"]["unknown"] == 1
    assert report["counts"]["violated"] == 1


def write_registry(root):
    fixture(root)
    data = {
        "schema_version": "2.3.0", "repo_root": str(root), "repos": {"declared": {}},
        "owners": {"human": {"tg_chat_id": "123456789"}},
        "entries": {
            "base": {"path": "docs/base.md", "owner": "human", "type": "document",
                     "description": "base"},
            "top": {"path": "docs/top.md", "owner": "human", "type": "document",
                    "description": "top", "upstream": ["base"],
                    "projections": [{"target": "copy.md", "method": "reference"}]},
        },
        "agents": {"worker": {"workspace": "agent", "identity": "agent/IDENTITY.md",
                               "depends_on": ["top"], "runtime_status": "bootstrap"}},
    }
    path = root / "custom-registry.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_symlink_loop_is_unknown(tmp_path):
    (tmp_path / "a").symlink_to("b")
    (tmp_path / "b").symlink_to("a")
    report = observe(tmp_path, Registry("1.0", {"a": Entry("a", "document")}, {}))
    assert report["verdict"] == "unknown"
    assert row(report, "S1")["reason"] == "path_observation_unavailable"


def test_outside_content_projection_and_fifo_are_not_read(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-external")
    outside.write_text("PRIVATE_CONTENT")
    (tmp_path / "source").write_text("source")
    (tmp_path / "copy").symlink_to(outside)
    reg = Registry("1.0", {"a": Entry("source", "document", projections=(
        Projection("copy", "reference"),
    ))}, {})
    report = observe(tmp_path, reg)
    assert row(report, "C2")["reason"] == "path_outside_root"
    assert "PRIVATE_CONTENT" not in json.dumps(report)
    (tmp_path / "copy").unlink()
    os.mkfifo(tmp_path / "copy")
    assert row(observe(tmp_path, reg), "C2")["reason"] == "wrong_path_kind"


def test_empty_scope_and_relative_root_are_explicit(tmp_path):
    report = observe(tmp_path, Registry("1.0", {}, {}))
    assert report["counts"] == {"passed": 0, "violated": 0, "unknown": 0}
    assert report["selected_entries"] == report["selected_agents"] == []
    with pytest.raises(ConfigurationError):
        inspect_registry(Registry("1.0", {}, {}), Path("."))


