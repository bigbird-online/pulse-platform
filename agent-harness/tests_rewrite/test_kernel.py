"""S1 executable evidence: request identity, transactions, authority and read-only replay.

These tests deliberately use a trusted in-memory Host, not production identity
proof. Task attempt, full external-effect, adapter and migration gates remain open.
"""

from __future__ import annotations

import ast
import json
import random
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from harness_rewrite import Command, HostContext, Kernel, Query, SourceInput
from harness_rewrite.model import digest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = SourceInput("native-input-1", "native_agent", b"Create a bounded test work item")
CREATOR = HostContext(
    "team",
    "producer",
    "private-session-original",
    "local",
    0,
    frozenset({"*"}),
    frozenset({"create_draft", "create_lightweight"}),
    SOURCE,
)
INTENTS = frozenset(
    {
        "start_work",
        "checkpoint",
        "record_work",
        "grant_scope",
        "revoke_scope",
        "rollover",
        "accept_transfer",
        "stop",
    }
)
OWNER = replace(CREATOR, generation=1, allowed_intents=INTENTS, source=None)
OTHER = replace(OWNER, actor="collaborator", session="private-session-other")
INPUT = {
    "summary": "Fixture work for tests.",
    "why": "A deterministic acceptance exercise",
    "what": "One preserved result",
    "acceptance": ["Original facts survive retries"],
    "owner": "producer",
    "resources": ["repo/src", "repo/tests"],
}


class Host:
    def __init__(self):
        self.contexts = {"create": CREATOR, "owner": OWNER, "other": OTHER}

    def authenticate(self, handle):
        if not isinstance(handle, str) or handle not in self.contexts:
            raise PermissionError("No native identity")
        return self.contexts[handle]


@pytest.fixture
def setup(tmp_path):
    host = Host()
    kernel = Kernel(tmp_path / "facts.sqlite", host.authenticate)
    return kernel, host


def create(kernel, *, draft=False):
    result = kernel.execute(
        Command("create", "create_draft" if draft else "create_lightweight", INPUT),
        "create",
    )
    assert result["status"] == "local_committed", result
    return result["work_item"]["id"]


def start(kernel):
    wi = create(kernel)
    assert (
        kernel.execute(Command("start", "start_work", {}, wi), "owner")["status"]
        == "local_committed"
    )
    return wi


def checkpoint(wi, request="progress", resources=None):
    return Command(
        request,
        "checkpoint",
        {
            "summary": "Known completed work and remaining risk",
            "resources": resources or ["repo/src"],
        },
        wi,
    )


def code(result):
    return result["error"]["code"] if result["error"] else None


def cap(result, name):
    return next(c["state"] for c in result["capabilities"] if c["name"] == name)


def test_draft_has_original_definition_but_no_execution_grant(setup):
    kernel, host = setup
    wi = create(kernel, draft=True)
    response = kernel.resume(wi, "owner")
    assert response["result"]["current_observation"]["definition"] == INPUT
    assert cap(response, "local_work") == "unavailable"
    assert code(kernel.execute(checkpoint(wi), "owner")) == "authority_denied"
    assert (
        code(kernel.execute(Command("start", "start_work", {}, wi), "owner")) == "domain_conflict"
    )
    assert response["next_action"]["intent"] == "adopt_plan"


def test_original_result_is_immutable_while_observation_advances(setup):
    kernel, _ = setup
    wi = start(kernel)
    first = kernel.execute(checkpoint(wi), "owner")
    kernel.execute(checkpoint(wi, "later"), "owner")
    retry = kernel.execute(checkpoint(wi), "owner")
    assert retry["result"]["original_request_result"] == first["result"]["original_request_result"]
    assert retry["work_item"]["local_revision"] == first["work_item"]["local_revision"] + 1
    changed = replace(checkpoint(wi), input={"summary": "different", "resources": ["repo/src"]})
    assert code(kernel.execute(changed, "owner")) == "request_conflict"
    assert cap(kernel.execute(changed, "owner"), "local_read") == "available"


def test_duplicate_creation_is_atomic_under_concurrency(setup):
    kernel, _ = setup
    command = Command("creation-race", "create_draft", INPUT)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: kernel.execute(command, "create"), range(16)))
    assert all(r["status"] == "local_committed" for r in results), results
    assert len({r["operation_id"] for r in results}) == 1
    assert {r["work_item"]["local_revision"] for r in results} == {1}


def test_scope_competition_and_nonoverlapping_collaboration(setup):
    kernel, _ = setup
    wi = start(kernel)

    def grant(request, resource):
        return Command(
            request,
            "grant_scope",
            {
                "principal": OTHER.principal(),
                "resources": [resource],
                "intents": ["checkpoint"],
            },
            wi,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda n: kernel.execute(grant(n, "repo/src"), "owner"), ["a", "b"])
        )
    assert sorted(r["status"] for r in results) == ["local_committed", "rejected"]
    accepted = next(r for r in results if r["status"] == "local_committed")
    assert (
        kernel.execute(checkpoint(wi, "collab", ["repo/src/deep"]), "other")["status"]
        == "local_committed"
    )
    assert (
        kernel.execute(checkpoint(wi, "unrelated", ["repo/tests"]), "owner")["status"]
        == "local_committed"
    )
    assert code(kernel.execute(checkpoint(wi, "outside", ["repo"]), "other")) == "authority_denied"
    assert code(kernel.execute(checkpoint(wi, "holder-overlap"), "owner")) == "authority_denied"
    grant_id = accepted["result"]["original_request_result"]["grant_id"]
    assert (
        kernel.execute(
            Command("revoke", "revoke_scope", {"grant_id": grant_id, "reason": "done"}, wi),
            "owner",
        )["status"]
        == "local_committed"
    )
    assert code(kernel.execute(checkpoint(wi, "after-revoke"), "other")) == "authority_denied"
    assert kernel.execute(checkpoint(wi, "holder-restored"), "owner")["status"] == "local_committed"


def test_rollover_fences_source_before_successor_and_retry_keeps_original_result(setup):
    kernel, host = setup
    wi = start(kernel)
    checkpoint_result = kernel.execute(checkpoint(wi), "owner")
    transfer = kernel.execute(
        Command(
            "rollover",
            "rollover",
            {"summary": "Continue the same outcome", "target": OTHER.principal()},
            wi,
        ),
        "owner",
    )
    assert transfer["status"] == "local_committed"
    assert cap(kernel.resume(wi, "owner"), "local_work") == "unavailable"
    assert cap(kernel.resume(wi, "other"), "local_work") == "unavailable"
    transfer_id = transfer["result"]["original_request_result"]["transfer_id"]
    accept = Command("accept", "accept_transfer", {"transfer_id": transfer_id}, wi)
    assert kernel.execute(accept, "other")["status"] == "local_committed"
    host.contexts["successor"] = replace(OTHER, generation=2)
    assert cap(kernel.resume(wi, "successor"), "local_work") == "available"
    assert code(kernel.execute(checkpoint(wi, "stale"), "owner")) == "authority_changed"
    retry = kernel.execute(checkpoint(wi), "owner")
    assert (
        retry["result"]["original_request_result"]
        == checkpoint_result["result"]["original_request_result"]
    )
    assert cap(retry, "local_work") == "unavailable"
    assert kernel.execute(accept, "other")["result"]["original_request_result"]["revision"] == 5
    assert kernel.resume(wi, "successor")["work_item"]["local_revision"] == 5
    historical = kernel.inspect(Query(wi, revision=2), "owner")
    assert cap(historical, "local_work") == "unavailable"


def test_queries_never_mutate_or_publish_and_history_is_bounded(setup):
    kernel, host = setup
    wi = start(kernel)
    kernel.execute(checkpoint(wi), "owner")
    db = kernel._database
    before, mtime = db.read_bytes(), db.stat().st_mtime_ns
    host.contexts["reader"] = replace(OTHER, allowed_intents=frozenset())
    for _ in range(5):
        result = kernel.inspect(Query(wi, view="history", after=1, limit=1), "reader")
        assert [h["revision"] for h in result["result"]["history"]] == [2]
        assert result["result"]["next_cursor"] == 2
        assert kernel.resume(wi, "reader")["status"] == "read_only"
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == mtime
    assert cap(result, "external_publish") == "unavailable"
    assert "private-session" not in json.dumps(result)
    assert SOURCE.content.decode() not in json.dumps(result)
    assert db.stat().st_mode & 0o777 == 0o600


def test_namespace_and_native_context_are_required(setup):
    kernel, host = setup
    wi = start(kernel)
    assert code(kernel.execute(checkpoint(wi), OWNER)) == "authority_denied"
    host.contexts["outsider"] = replace(OWNER, namespace="other-team")
    assert code(kernel.inspect(Query(wi), "outsider")) == "domain_conflict"
    host.contexts["unreadable"] = replace(OWNER, readable_wis=frozenset())
    assert code(kernel.execute(checkpoint(wi), "unreadable")) == "authority_denied"


def test_stop_requires_exact_human_source_and_does_not_claim_outcome(setup):
    kernel, host = setup
    wi = start(kernel)
    data = {"reason": "Human stopped this work"}
    command = Command("stop", "stop", data, wi)
    assert code(kernel.execute(command, "owner")) == "source_invalid"
    source = SourceInput(
        "native-decision-2",
        "native_human",
        b"Stop this specific work",
        "stop",
        digest({"intent": "stop", "work_item_id": wi, "input": data}),
    )
    host.contexts["human-host"] = replace(OWNER, source=source)
    result = kernel.execute(command, "human-host")
    assert result["work_item"]["business_progress"] == "stopped"
    assert cap(result, "local_work") == "unavailable"
    assert "accepted" not in json.dumps(result)
    assert kernel.execute(command, "human-host")["result"] == result["result"]
    assert code(kernel.execute(checkpoint(wi), "owner")) == "authority_denied"


def test_approval_cannot_be_reused_for_another_work_item(setup):
    kernel, host = setup
    first = start(kernel)
    created = kernel.execute(Command("second-create", "create_lightweight", INPUT), "create")
    second = created["work_item"]["id"]
    kernel.execute(Command("second-start", "start_work", {}, second), "owner")
    data = {"reason": "Stop a single work item"}
    host.contexts["approval"] = replace(
        OWNER,
        source=SourceInput(
            "human",
            "native_human",
            b"Stop first only",
            "stop",
            digest({"intent": "stop", "work_item_id": first, "input": data}),
        ),
    )
    assert (
        code(kernel.execute(Command("wrong-stop", "stop", data, second), "approval"))
        == "source_invalid"
    )
    assert (
        kernel.execute(Command("right-stop", "stop", data, first), "approval")["status"]
        == "local_committed"
    )


def test_host_policy_revocation_is_visible_without_rewriting_history(setup):
    kernel, host = setup
    wi = start(kernel)
    host.contexts["readonly-owner"] = replace(OWNER, allowed_intents=frozenset())
    assert cap(kernel.resume(wi, "readonly-owner"), "local_work") == "unavailable"


@pytest.mark.parametrize(
    "field,value", [("next_state", {"status": "completed"}), ("human_confirmed", True)]
)
def test_no_generic_state_or_literal_approval_input(setup, field, value):
    kernel, _ = setup
    command = Command("create", "create_draft", {**INPUT, field: value})
    assert code(kernel.execute(command, "create")) == "invalid_input"
    assert not hasattr(kernel, "append_event")
    assert not hasattr(kernel, "set_state")


@pytest.mark.parametrize(
    "value", [None, {}, [None], [["bad"]], ["../escape"], ["/absolute"], ["repo//src"]]
)
def test_malformed_scope_is_rejected_without_partial_facts(setup, value):
    kernel, _ = setup
    response = kernel.execute(
        Command("create", "create_draft", {**INPUT, "resources": value}), "create"
    )
    assert code(response) == "invalid_input"
    wi = create(kernel)
    assert kernel.inspect(Query(wi), "owner")["work_item"]["local_revision"] == 1


@pytest.mark.parametrize("damage", ["content", "original", "tail", "head"])
def test_corrupt_facts_are_never_reconstructed_from_a_fake_view(setup, damage):
    kernel, _ = setup
    wi = start(kernel)
    with sqlite3.connect(kernel._database) as conn:
        if damage == "content":
            conn.execute("UPDATE journal SET content='{}' WHERE revision=1")
        elif damage == "original":
            conn.execute("UPDATE originals SET content=x'00'")
        elif damage == "tail":
            conn.execute("DELETE FROM journal WHERE revision=2")
        else:
            conn.execute("UPDATE journal_heads SET revision=900")
    (kernel._database.parent / "wi-cache.json").write_text('{"status":"active","owner":"producer"}')
    assert code(kernel.resume(wi, "owner")) == "journal_invalid"
    assert code(kernel.execute(checkpoint(wi), "owner")) == "journal_invalid"


@pytest.mark.parametrize(
    "stage,committed",
    [
        ("after_inputs", False),
        ("after_journal", False),
        ("before_commit", False),
        ("after_commit", True),
    ],
)
def test_process_exit_at_transaction_boundary_recovers_original_request(setup, stage, committed):
    kernel, _ = setup
    wi = start(kernel)
    child = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness_rewrite import Command, HostContext, Kernel
ctx = HostContext("team", "producer", "private-session-original", "local", 1,
                  frozenset({"*"}), frozenset({"checkpoint"}))
def fault(stage):
    if stage == sys.argv[4]:
        os._exit(73)
k = Kernel(Path(sys.argv[2]), lambda handle: ctx, fault=fault)
k.execute(Command("crash", "checkpoint", {"summary":"crash probe", "resources":["repo/src"]}, sys.argv[3]), "host")
"""
    run = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            child,
            str(ROOT),
            str(kernel._database),
            wi,
            stage,
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 73, run.stderr
    before = kernel.inspect(Query(wi), "owner")
    assert before["work_item"]["local_revision"] == (3 if committed else 2)
    command = Command(
        "crash", "checkpoint", {"summary": "crash probe", "resources": ["repo/src"]}, wi
    )
    # The production Host policy is not part of mutable business semantics.
    recovered = kernel.execute(command, "owner")
    assert recovered["status"] == "local_committed", recovered
    assert recovered["work_item"]["local_revision"] == 3
    assert kernel.execute(command, "owner")["work_item"]["local_revision"] == 3


def test_storage_failure_preserves_read_only_access(setup):
    kernel, host = setup
    wi = start(kernel)

    def fail(stage):
        if stage == "before_commit":
            raise OSError(28, "No space left")

    broken = Kernel(kernel._database, host.authenticate, fault=fail)
    result = broken.execute(checkpoint(wi), "owner")
    assert code(result) == "storage_unavailable"
    assert cap(result, "local_read") == "available"
    assert cap(result, "lifecycle_write") == "unavailable"
    assert kernel.resume(wi, "owner")["work_item"]["local_revision"] == 2
    assert kernel.execute(checkpoint(wi), "owner")["status"] == "local_committed"


def test_postcommit_failure_does_not_retract_committed_result(setup):
    kernel, host = setup
    wi = start(kernel)

    def fail(stage):
        if stage == "after_commit":
            raise OSError(28, "response sink unavailable")

    broken = Kernel(kernel._database, host.authenticate, fault=fail)
    result = broken.execute(checkpoint(wi), "owner")
    assert result["status"] == "local_committed"
    assert (
        result["result"]["original_request_result"]
        == kernel.execute(checkpoint(wi), "owner")["result"]["original_request_result"]
    )


def test_symbolic_link_cannot_redirect_journal(setup):
    kernel, host = setup
    wi = start(kernel)
    link = kernel._database.parent / "redirect.sqlite"
    link.symlink_to(kernel._database)
    assert (
        code(Kernel(link, host.authenticate).execute(checkpoint(wi), "owner"))
        == "storage_unavailable"
    )
    assert kernel.resume(wi, "owner")["work_item"]["local_revision"] == 2


def test_physically_corrupt_database_is_not_a_cache_failure(setup):
    kernel, _ = setup
    wi = start(kernel)
    kernel._database.write_bytes(b"This is not a SQLite journal")
    assert code(kernel.resume(wi, "owner")) == "journal_invalid"
    assert code(kernel.execute(checkpoint(wi), "owner")) == "journal_invalid"


def test_replay_and_request_properties_across_seeded_sequences(setup):
    kernel, _ = setup
    wi = start(kernel)
    rng = random.Random(18023)
    requests = []
    for n in range(40):
        command = checkpoint(wi, f"progress-{n}")
        result = kernel.execute(command, "owner")
        requests.append((command, result["result"]["original_request_result"]))
        for old, expected in rng.sample(requests, min(3, len(requests))):
            assert kernel.execute(old, "owner")["result"]["original_request_result"] == expected
        assert kernel.resume(wi, "owner")["work_item"]["local_revision"] == n + 3


def test_new_core_imports_no_old_lifecycle_or_dynamic_loader():
    allowed = {
        "__future__",
        "copy",
        "dataclasses",
        "typing",
        "json",
        "hashlib",
        "hmac",  # Authenticate sealed bytes between mutually trusted operator peers.
        "math",  # Reject nonfinite observed amounts without substituting zero.
        "re",
        "os",
        "sqlite3",
        "contextlib",
        "datetime",
        "zoneinfo",  # Installed IANA zones retain original aggregation windows.
        "http",  # Single-attempt loopback transport, without automatic redirects.
        "urllib",  # Strict parsing of installed loopback endpoint only.
        "pathlib",
        "base64",
        "stat",
        "tempfile",
        "fcntl",
        "argparse",
        "sys",
        "subprocess",
        "zipfile",
        "functools",  # Cache the native hardware identity within one process.
        "platform",
        "plistlib",  # macOS IORegistry's structured identity response.
        "uuid",  # An installation identity, never an execution grant.
        "ctypes",  # OS ACL inspection through libc; no plugin/code loading.
        "errno",
        "pwd",  # The account HOME for gh and the Relay resolver; an Agent HOME differs.
    }
    for path in (ROOT / "harness_rewrite").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if path.name == "production_install.py" and [name.name for name in node.names] == [
                    "inspect"
                ]:
                    continue  # Read this installer's fixed probe source for an older sealed reader.
                module_allowed = allowed | {
                    "production_history.py": {"yaml"},
                    # Read base-branch workflow triggers as data (safe_load), never run them;
                    # bounded waits on one read-only pulse-gh session.
                    "github_merge.py": {"yaml", "selectors", "time"},
                    # Standard-library bounded pipe waits; no plugin/old core.
                    "git_objects.py": {"selectors", "time"},
                    "standalone.py": {"signal", "time"},  # Bounded original child shutdown only.
                    "effect_service.py": {"time"},  # Bounded account service pass; no dynamic imports.
                    "review_dispatch.py": {"time"},  # Bounded wait for the shared review gate lock.
                    "external_entry.py": {"time"},  # Bounded wait for the owner's platform answer.
                    "production_install.py": {"time"},  # Bounded retry of the storage-contract probe.
                    "native_host.py": {"shlex"},  # Parse observed OS argv, never execute it.
                    # Parse a recorded tool command's `git -C`, never execute it.
                    "session_compute.py": {"shlex"},
                    "project_page.py": {"html"},  # Escape page text; output only.
                }.get(path.name, set())
                assert all(name.name.split(".")[0] in module_allowed for name in node.names), path
            elif isinstance(node, ast.ImportFrom) and not node.level:
                if (
                    path.name in {"native_compute.py", "project_page.py"}
                    and node.module == "importlib.resources"
                    and [item.name for item in node.names] == ["files"]
                ):
                    continue  # Fixed package bytes, not a dynamic code loader.
                if (
                    path.name == "relay_session.py"
                    and node.module == "types"
                    and [item.name for item in node.names] == ["SimpleNamespace"]
                ):
                    continue  # Narrow retired-source readback, not code loading.
                assert node.module.split(".")[0] in allowed, path
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"__import__", "eval", "exec"}, path


def test_historical_replay_digest_belongs_to_selected_revision(setup):
    from harness_rewrite.model import digest

    kernel, _ = setup
    wi = start(kernel)
    kernel.execute(checkpoint(wi), "owner")
    before = kernel._database.read_bytes()
    with kernel._connection(write=False) as connection:
        current, revision, head, history = kernel._replay(connection, OWNER.namespace, wi)
        for at in range(1, revision + 1):
            selected, actual, selected_head, prefix = kernel._replay(
                connection, OWNER.namespace, wi, at
            )
            assert actual == at and prefix == history[:at]
            assert selected_head == digest(history[at - 1])
            if at == revision:
                assert selected_head == head and selected == current
    assert kernel._database.read_bytes() == before


def test_host_rollover_does_not_accept_a_self_named_successor(setup):
    kernel, host = setup
    wi = start(kernel)
    host.contexts["owner"] = replace(
        OWNER, allowed_intents=OWNER.allowed_intents | {"prepare_rollover"}
    )
    prepare = Command("prepare-host", "prepare_rollover", {"summary": "original same work"}, wi)
    assert kernel.execute(prepare, "owner")["status"] == "rejected"
    host.contexts["owner"] = replace(
        host.contexts["owner"], session_handover={"source": OWNER.principal()}
    )
    prepared = kernel.execute(prepare, "owner")
    assert prepared["status"] == "local_committed", prepared
    transfer_id = prepared["result"]["original_request_result"]["transfer_id"]
    accept = Command("accept-host", "accept_transfer", {"transfer_id": transfer_id}, wi)
    host.contexts["successor"] = replace(OWNER, session="actual-new-session")
    assert kernel.execute(accept, "successor")["status"] == "rejected"
    host.contexts["successor"] = replace(
        host.contexts["successor"],
        session_handover={"source": OWNER.principal(), "transfer_id": transfer_id},
    )
    accepted = kernel.execute(accept, "successor")
    assert accepted["status"] == "local_committed", accepted
    assert kernel.execute(accept, "successor")["work_item"]["generation"] == 2


def test_resume_names_the_latest_checkpoint_for_a_successor(setup):
    # A rollover or continuation resumes from the handover checkpoint: resume
    # gives it directly instead of leaving it among every record.
    kernel, _ = setup
    wi = start(kernel)
    assert "latest_checkpoint" not in kernel.resume(wi, "owner")["result"]
    assert kernel.execute(checkpoint(wi, "first"), "owner")["status"] == "local_committed"
    handover = kernel.execute(checkpoint(wi, "handover"), "owner")
    assert handover["status"] == "local_committed"
    latest = kernel.resume(wi, "owner")["result"]["latest_checkpoint"]
    assert latest["record_id"] == handover["result"]["original_request_result"]["record_id"]
    assert latest["summary"] == "Known completed work and remaining risk"
    assert latest["position"] == 2
