from dataclasses import replace
import base64
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness_rewrite import Command, Query, SourceInput, cli
from harness_rewrite.checks import assess
from harness_rewrite.model import ArtifactInput, Rejected, encoded, digest
from harness_rewrite.native_host import NativeHost
from tests_rewrite.test_kernel import setup as setup, start
from tests_rewrite.test_native_host import native as native


def observed(**kwargs):
    return {"source_ref": "private/original-log:1", "evidence": "observed", **kwargs}


def rule(kind, check, key="check"):
    return {
        "id": key,
        "kind": kind,
        "actions": ["record_work"],
        "source_ref": "private/rules:23",
        "check": check,
    }


def packet(rules, **facts):
    return {
        "schema": "harness.check-evidence/v1",
        "action": "record_work",
        "rules": rules,
        "facts": facts,
    }


def report(value, **kwargs):
    artifact = ArtifactInput("repo/checks", "v1", encoded(value), "private")
    return assess(
        artifact.content, artifact.reference(), 7, Query(view="checks", **kwargs)
    )


def one(kind, check, **facts):
    return report(packet([rule(kind, check)], **facts))["items"][0]


@pytest.mark.parametrize(
    "state,size,nonempty,expected",
    [
        ("present", 1, True, "passed"),
        ("present", 0, True, "violated"),
        ("present", 0, False, "passed"),
        ("absent", None, True, "violated"),
        ("unknown", 1, True, "unknown"),
        ("present", None, True, "unknown"),
        ("present", True, True, "unknown"),
    ],
)
def test_file_results_are_recorded_observations(state, size, nonempty, expected):
    assert (
        one(
            "file_exists",
            {"paths": ["/private/a"], "nonempty": nonempty},
            files=[observed(path="/private/a", state=state, bytes=size)],
        )["status"]
        == expected
    )


def test_missing_duplicate_inferred_file_and_declared_scope_never_pass():
    check = {"paths": ["a"]}
    fact = observed(path="a", state="present", bytes=2)
    for facts in (
        [],
        [fact, fact],
        [{**fact, "evidence": "inferred"}],
        [{**fact, "source_ref": ""}],
    ):
        assert one("file_exists", check, files=facts)["status"] == "unknown"
    assert (
        one(
            "file_exists",
            {"paths": ["a", "b"]},
            files=[observed(path="b", state="absent")],
        )["status"]
        == "violated"
    )
    p = packet([rule("file_exists", check)], files=[fact])
    p["action"] = "publish_source"
    row = report(p)["items"][0]
    assert row["applicable"] is False and row["status"] == "unknown"
    assert row["reason"] == "action_not_declared"


@pytest.mark.parametrize(
    "state,exit_code,evidence,expected",
    [
        ("completed", 0, "observed", "passed"),
        ("completed", 2, "observed", "violated"),
        ("timeout", 0, "observed", "unknown"),
        ("unavailable", None, "observed", "unknown"),
        ("completed", False, "observed", "unknown"),
        ("completed", 0, "inferred", "unknown"),
    ],
)
def test_recorded_commands_never_execute_programs(
    state, exit_code, evidence, expected, monkeypatch
):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("A reviewed command was executed"),
    )
    assert (
        one(
            "command",
            {"command_id": "probe"},
            commands=[
                observed(
                    id="probe", state=state, exit_code=exit_code, evidence=evidence
                )
            ],
        )["status"]
        == expected
    )


def test_pattern_checks_use_bounded_worker_and_do_not_echo_evidence():
    facts = {"fields": [observed(name="decisions", values=["Secret: Review APPROVED"])]}
    assert (
        one(
            "pattern_match",
            {"field": "decisions", "patterns": ["review.*approved"]},
            **facts,
        )["status"]
        == "passed"
    )
    assert (
        one("pattern_match", {"field": "decisions", "patterns": ["rejected"]}, **facts)[
            "status"
        ]
        == "violated"
    )
    assert (
        one("pattern_match", {"field": "decisions", "patterns": ["["]}, **facts)[
            "reason"
        ]
        == "invalid_pattern"
    )
    facts["fields"][0]["evidence"] = "inferred"
    assert (
        one("pattern_match", {"field": "decisions", "patterns": ["approved"]}, **facts)[
            "status"
        ]
        == "unknown"
    )
    assert (
        one("pattern_match", {"field": "missing", "patterns": ["a"]})["status"]
        == "unknown"
    )
    assert (
        one(
            "pattern_match",
            {"field": "empty", "patterns": ["a"]},
            fields=[observed(name="empty", values=[])],
        )["status"]
        == "violated"
    )


def test_pathological_regex_is_unknown_within_worker_budget():
    row = one(
        "pattern_match",
        {"field": "body", "patterns": ["(a+)+$"]},
        fields=[observed(name="body", values=["a" * 10000 + "!"])],
    )
    assert row["status"] == "unknown"
    assert row["reason"] == "pattern_budget_or_worker_unavailable"


def event(kind, seq, obj="report", version="v1", **kwargs):
    return observed(kind=kind, sequence=seq, object=obj, version=version, **kwargs)


@pytest.mark.parametrize(
    "events,complete,status",
    [
        ([event("confirmed", 1), event("written", 2)], True, "passed"),
        (
            [event("confirmed", 1, obj="different"), event("written", 2)],
            True,
            "violated",
        ),
        ([event("confirmed", 1, version="v0"), event("written", 2)], True, "violated"),
        ([event("written", 1), event("confirmed", 2)], True, "violated"),
        ([event("written", 2)], False, "unknown"),
        (
            [event("confirmed", 1, evidence="inferred"), event("written", 2)],
            True,
            "unknown",
        ),
        (
            [event("confirmed", 1), event("written", 2, evidence="inferred")],
            True,
            "unknown",
        ),
        ([event("confirmed", 1), event("written", 1)], True, "unknown"),
        ([event("confirmed", 1)], True, "unknown"),
        ([event("written", 2), event("confirmed", 1)], True, "passed"),
    ],
)
def test_confirmation_must_match_exact_object_version_and_explicit_order(
    events, complete, status
):
    assert (
        one(
            "temporal_check",
            {"objects": ["report"]},
            events=events,
            events_complete=complete,
        )["status"]
        == status
    )


@pytest.mark.parametrize(
    "bad",
    [
        rule("new_unknown_kind", {}),
        rule("file_exists", {"paths": []}),
        rule("file_exists", {"paths": ["a"], "pass_condition": "true"}),
        rule("command", {"command_id": "probe", "shell": "arbitrary"}),
        rule("pattern_match", {"field": "a", "patterns": ["x" * 257]}),
        rule("temporal_check", {"objects": []}),
        {"id": "missing"},
        None,
    ],
)
def test_unknown_and_malformed_individual_rules_remain_unknown(bad):
    assert report(packet([bad]))["counts"] == {"passed": 0, "violated": 0, "unknown": 1}


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema":"x","schema":"y"}',
        b'{"value":NaN}',
        b"null",
        b"x" * (2 * 1024 * 1024 + 1),
        encoded(packet([rule("command", {"command_id": "x"})] * 2)),
        encoded(
            packet([rule("command", {"command_id": "x"}, str(i)) for i in range(101)])
        ),
        encoded(packet([rule("command", {"command_id": "x"})], commands=[{}] * 1001)),
    ],
)
def test_malformed_or_oversized_packet_is_structurally_rejected(content):
    with pytest.raises(Rejected):
        assess(content, {}, 1, Query(view="checks"))


def test_report_counts_precede_pagination_and_reference_private_originals():
    p = packet(
        [
            rule("command", {"command_id": "probe"}, "first"),
            rule("unknown", {}, "second"),
        ],
        commands=[observed(id="probe", state="completed", exit_code=0)],
    )
    result = report(p, limit=1)
    assert result["total_count"] == 2 and result["next_offset"] == 1
    assert result["counts"] == {"passed": 1, "violated": 0, "unknown": 1}
    assert result["items"][0]["evidence_pointers"] == ["/facts/commands/0"]
    assert result["items"][0]["rule_pointer"] == "/rules/0"
    assert (
        result["evidence_trust"] == "recorded_artifact_claims"
        and not result["human_authority"]
    )
    assert result["source_revision"] == 7 and not result["lifecycle_mutation"]
    assert all(
        s not in json.dumps(result) for s in ["private/original", "private/rules"]
    )
    assert report(p, after=1, limit=1)["next_offset"] is None


def test_fixed_artifact_query_enforces_history_privacy_integrity_and_readonly(
    setup, tmp_path
):
    kernel, host = setup
    wi = start(kernel)
    artifact = ArtifactInput(
        "repo/checks",
        "v1",
        encoded(
            packet(
                [rule("command", {"command_id": "test"})],
                commands=[observed(id="test", state="completed", exit_code=0)],
            )
        ),
        "private",
    )
    host.contexts["owner"] = replace(
        host.contexts["owner"],
        artifacts=(artifact,),
        source=SourceInput("observed", "native_agent", b"Capture recorded checks"),
    )
    r = kernel.execute(
        Command(
            "record",
            "record_work",
            {
                "summary": "Recorded check observations",
                "resources": ["repo/src"],
                "artifacts": [artifact.reference()],
            },
            wi,
        ),
        "owner",
    )
    assert r["status"] == "local_committed", r
    revision = r["work_item"]["local_revision"]
    db = tmp_path / "facts.sqlite"
    before = db.read_bytes()
    q = Query(wi, view="checks", artifact_ref=artifact.reference())
    result = kernel.inspect(q, "owner")
    assert result["status"] == "read_only", result
    assert result["result"]["items"]["counts"]["passed"] == 1
    assert (
        result["effects"] == []
        and result["result"]["effects_scope"] == "checks_read_only"
    )
    assert result["result"]["current_observation"].keys() == {
        "source",
        "generation",
        "business_progress",
    }
    assert (
        kernel.inspect(replace(q, revision=revision - 1), "owner")["error"]["code"]
        == "authority_denied"
    )
    assert kernel.inspect(q, "other")["error"]["code"] == "authority_denied"
    from tests_rewrite.test_kernel import INPUT

    second = kernel.execute(Command("second-wi", "create_draft", INPUT), "create")
    assert second["status"] == "local_committed"
    other_wi = second["work_item"]["id"]
    assert (
        kernel.inspect(replace(q, work_item_id=other_wi), "owner")["error"]["code"]
        == "authority_denied"
    )
    before = db.read_bytes()
    altered = {**artifact.reference(), "version": "fabricated"}
    assert (
        kernel.inspect(replace(q, artifact_ref=altered), "owner")["error"]["code"]
        == "authority_denied"
    )
    assert (
        kernel.inspect(replace(q, view="state"), "owner")["error"]["code"]
        == "invalid_input"
    )
    raw = kernel.inspect(replace(q, view="artifact"), "owner")
    assert base64.b64decode(raw["result"]["artifact"]["base64"]) == artifact.content
    assert db.read_bytes() == before
    stop_data = {"reason": "Original task stopped"}
    host.contexts["owner"] = replace(
        host.contexts["owner"],
        source=SourceInput(
            "fixture-human-stop",
            "native_human",
            b"Stop this fixture",
            approved_intent="stop",
            approved_input_digest=digest(
                {"intent": "stop", "work_item_id": wi, "input": stop_data}
            ),
        ),
    )
    stopped = kernel.execute(
        Command("stop", "stop", {"reason": "Original task stopped"}, wi), "owner"
    )
    assert stopped["status"] == "local_committed", stopped
    historical = kernel.inspect(replace(q, revision=revision), "owner")
    current = kernel.inspect(Query(wi), "owner")
    assert (
        historical["capabilities"] == current["capabilities"]
        and historical["next_action"] == current["next_action"]
    )
    c = sqlite3.connect(db)
    c.execute(
        "UPDATE originals SET content=? WHERE digest=?",
        (b"corrupt", artifact.reference()["sha256"]),
    )
    c.commit()
    c.close()
    assert kernel.inspect(q, "owner")["error"]["code"] == "journal_invalid"


@pytest.mark.parametrize(
    "args",
    [
        ["identity", "--artifact-ref", "not-read"],
        ["inspect", "--artifact-ref", "not-read"],
        ["inspect", "--view", "checks"],
        ["resume", "--view", "checks"],
    ],
)
def test_cli_never_ignores_reference_or_checks_on_other_action(args, capsys):
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_native_cli_reads_real_journal_artifact_checks(
    native, tmp_path, monkeypatch, capsys
):
    policy, proc, _, _ = native
    data = json.loads(policy.read_text())
    for name in ["delivery", "dispatch", "views"]:
        (tmp_path / name).mkdir()
    data.update(
        database=str(tmp_path / "facts.sqlite"),
        delivery_root=str(tmp_path / "delivery"),
        dispatch_root=str(tmp_path / "dispatch"),
        views_root=str(tmp_path / "views"),
    )
    policy.write_text(json.dumps(data))
    monkeypatch.setattr(
        cli, "NativeHost", lambda _: NativeHost(policy, proc=proc, pid=101)
    )
    monkeypatch.setattr(cli.sys, "argv", [str(Path(__file__).resolve())])
    command = tmp_path / "command.json"

    def run(args):
        exit_code = cli.main(args)
        result = json.loads(capsys.readouterr().out)
        assert exit_code == 0, result
        return result

    def send(intent, data, wi=None, files=None):
        command.write_text(
            json.dumps(
                {
                    "intent": intent,
                    "work_item_id": wi,
                    "input": data,
                    "files": files or {},
                }
            )
        )
        return run(["execute", "--input", str(command)])

    created = send(
        "create_lightweight",
        {
            "summary": "Fixture work for tests.",
            "why": "Observe recorded checks",
            "what": "Read fixed evidence",
            "acceptance": ["Read-only tri-state report"],
            "resources": ["repo"],
        },
    )
    wi = created["work_item"]["id"]
    send("start_work", {}, wi)
    artifact_root = Path(data["profiles"][0]["artifact_roots"][0])
    evidence = artifact_root / "checks.json"
    evidence.write_bytes(
        encoded(
            packet(
                [rule("command", {"command_id": "probe"})],
                commands=[observed(id="probe", state="timeout", exit_code=None)],
            )
        )
    )
    send(
        "record_work",
        {
            "summary": "Capture actual observed timeout",
            "resources": ["repo"],
            "artifacts": [{"file": "checks"}],
        },
        wi,
        {
            "checks": {
                "path": str(evidence),
                "logical": "repo/checks",
                "version": "v1",
                "access": "private",
            }
        },
    )
    reference = tmp_path / "reference.json"
    reference.write_text(
        json.dumps(
            ArtifactInput(
                "repo/checks", "v1", evidence.read_bytes(), "private"
            ).reference()
        )
    )
    db = (tmp_path / "facts.sqlite").read_bytes()
    result = run(
        ["inspect", "--wi", wi, "--view", "checks", "--artifact-ref", str(reference)]
    )
    assert result["result"]["items"]["counts"] == {
        "passed": 0,
        "violated": 0,
        "unknown": 1,
    }
    assert result["effects"] == []
    assert (tmp_path / "facts.sqlite").read_bytes() == db
