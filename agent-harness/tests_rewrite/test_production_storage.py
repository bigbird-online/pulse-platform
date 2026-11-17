"""Production storage regressions use actual Git transactions, never live WIs."""

import base64
from dataclasses import replace
import hashlib
import json
import stat
import subprocess

import pytest

from harness_rewrite import Command, Query
from harness_rewrite.model import ArtifactInput
from harness_rewrite import native_usage, production_segments as segments
from tests_rewrite.test_production_ledger import Rig, WI, RECORD, git


def objects(repository):
    rows = git(repository, "ls-tree", "-rl", "main", "--", ".harness/core").splitlines()
    return [(line.split()[2], int(line.split()[3]), line.split("\t")[1]) for line in rows]


def test_private_transcript_never_enters_git_and_absent_custody_does_not_block_work(tmp_path):
    from tests_rewrite.test_measurements import log, HEADER, row
    rig = Rig(tmp_path)
    sentinel = "PRIVATE_TOOL_FILE_CONTENT_4a813cc7"
    path = log(tmp_path, {"type": "message", "message": {"role": "toolResult", "content": sentinel}},
               row({"input": 13, "output": 7, "totalTokens": 20}))
    artifact, _ = native_usage.capture(path, HEADER, 0, path.stat().st_size)
    rig.context = replace(rig.context, artifacts=(artifact,))
    assert rig.approve()["status"] == "local_committed"
    for sha, size, _ in objects(rig.remote):
        content = subprocess.check_output(["git", "-C", str(rig.remote), "cat-file", "blob", sha])
        assert sentinel.encode() not in content
        assert base64.b64encode(artifact.content) not in content
        assert size <= segments.CHUNK
    original = rig.kernel.inspect(Query(WI, view="artifact", artifact_ref=artifact.reference()), "native")
    assert base64.b64decode(original["result"]["artifact"]["base64"]) == artifact.content
    peer = rig.client(rig.backend(rig.b))
    assert peer.inspect(Query(WI), "native")["status"] == "read_only"
    unavailable = peer.inspect(Query(WI, view="artifact", artifact_ref=artifact.reference()), "native")
    assert unavailable["error"]["code"] == "evidence_unavailable"
    rig.context = replace(rig.context, artifacts=())
    assert rig.approve(peer, request="continue-without-private-custody")["status"] == "local_committed"


def test_over_100_mib_private_inputs_keep_git_objects_bounded(tmp_path):
    rig = Rig(tmp_path)
    for number in range(7):
        content = bytes([65 + number]) * (16 * 1024 * 1024)
        artifact = ArtifactInput("native/usage/range", str(number), content, "private")
        rig.context = replace(rig.context, artifacts=(artifact,))
        result = rig.approve(request="large-private-" + str(number))
        assert result["status"] == "local_committed", result
        assert rig.ledger.evidence.read(hashlib.sha256(content).hexdigest()) == content
    assert max(size for _, size, _ in objects(rig.remote)) <= segments.CHUNK
    assert sum(size for _, size, _ in objects(rig.remote)) < 1024 * 1024
    assert rig.client(rig.backend(rig.b)).inspect(Query(WI), "native")["status"] == "read_only"


def test_unrelated_main_commit_is_preserved_and_original_command_completes(tmp_path):
    rig = Rig(tmp_path)
    def race(stage):
        if stage == "before_push":
            rig.ledger.fault = lambda _: None
            (rig.b / "README.md").write_text("unrelated human PR\n")
            git(rig.b, "add", "README.md")
            git(rig.b, "commit", "-m", "Unrelated PR")
            git(rig.b, "push", "origin", "main")
    rig.ledger.fault = race
    result = rig.approve()
    assert result["status"] == "local_committed", result
    assert git(rig.remote, "show", "main:README.md") == "unrelated human PR"
    prior = git(rig.remote, "rev-parse", "main")
    assert rig.approve()["status"] == "local_committed"
    assert git(rig.remote, "rev-parse", "main") == prior


def test_bad_unrelated_legacy_wi_is_not_loaded_by_a_native_write(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    git(rig.b, "pull", "--ff-only")
    other = "wi_20260823_184"
    path = rig.b / f".harness/work-items/{other}.yaml"
    path.write_text(json.dumps({**RECORD, "work_item_id": other, "title": None,
                                "unrelated_large_content": "x" * (16 * 1024 * 1024)}))
    git(rig.b, "add", str(path))
    git(rig.b, "commit", "-m", "Unrelated malformed legacy WI")
    git(rig.b, "push", "origin", "main")
    loaded = []
    original = rig.ledger.blobs
    def observe(mapping):
        loaded.extend(mapping)
        return original(mapping)
    monkeypatch.setattr(rig.ledger, "blobs", observe)
    assert rig.approve(request="new-decision")["status"] == "local_committed"
    assert not any(other in p for p in loaded)
    assert rig.kernel.inspect(Query(other), "native")["error"]["code"] == "ledger_invalid"


def test_actual_dependency_conflict_is_retryable(tmp_path):
    rig = Rig(tmp_path)
    other = rig.client(rig.backend(rig.b))
    def race(stage):
        if stage == "before_push":
            rig.ledger.fault = lambda _: None
            assert rig.approve(other, request="competing")["status"] == "local_committed"
    rig.ledger.fault = race
    result = rig.approve()
    assert result["error"]["code"] == "ledger_conflict"
    assert result["error"]["retryable"] is True
    assert rig.approve()["status"] == "local_committed"


def test_server_policy_rejection_is_not_concurrency(tmp_path):
    rig = Rig(tmp_path)
    hook = rig.remote / "hooks/pre-receive"
    hook.write_text('#!/bin/sh\necho "GH013: protected branch rule" >&2\nexit 1\n')
    hook.chmod(0o700)
    result = rig.approve()
    assert result["error"]["code"] == "ledger_publication_denied"
    assert result["error"]["retryable"] is False
    assert git(rig.remote, "rev-parse", "main") == rig.before


def test_global_request_identity_survives_scoped_transactions(tmp_path):
    rig = Rig(tmp_path)
    assert rig.approve()["status"] == "local_committed"
    result = rig.kernel.execute(Command("affiliation", "create_lightweight", {
        "summary": "Fixture work for tests.",
        "why": "same request is not another WI", "what": "new", "acceptance": ["done"],
        "resources": ["repo"]}), "native")
    assert result["error"]["code"] == "request_conflict", result


@pytest.mark.parametrize("mode", [0o700, 0o2700])
def test_private_store_accepts_owner_only_directory_with_inherited_setgid(tmp_path, mode):
    from harness_rewrite.production_evidence import Evidence
    store = Evidence(tmp_path / "private")
    store.root.mkdir()
    store.root.chmod(mode)
    content = b"private original on a setgid filesystem"
    sha = hashlib.sha256(content).hexdigest()
    store.put(sha, content)
    assert store.read(sha) == content
    assert stat.S_IMODE(store.root.stat().st_mode) == mode
    assert stat.S_IMODE((store.root / sha).stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o705, 0o750, 0o2750, 0o2770, 0o1700, 0o4700])
def test_private_store_rejects_directory_access_or_unexpected_special_bits(tmp_path, mode):
    from harness_rewrite.production_evidence import Evidence
    from harness_rewrite.model import Rejected
    store = Evidence(tmp_path / "private")
    store.root.mkdir()
    store.root.chmod(mode)
    content = b"must not publish in an unsafe directory"
    with pytest.raises(Rejected, match="owner-only"):
        store.put(hashlib.sha256(content).hexdigest(), content)
    assert list(store.root.iterdir()) == []


def test_private_store_rejects_symlinks_and_unsafe_permissions(tmp_path):
    from harness_rewrite.production_evidence import Evidence
    from harness_rewrite.model import Rejected
    store = Evidence(tmp_path / "private")
    content = b"private original"
    sha = hashlib.sha256(content).hexdigest()
    store.put(sha, content)
    (store.root / sha).chmod(0o644)
    with pytest.raises(Rejected, match="owner-only"):
        store.read(sha)
    (store.root / sha).unlink()
    (tmp_path / "other").write_bytes(content)
    (store.root / sha).symlink_to(tmp_path / "other")
    with pytest.raises(Rejected):
        store.read(sha)


def test_v1_conversion_preserves_exact_decisions_without_reexecuting_them(tmp_path):
    from tests_rewrite.test_production_ledger import v1_record
    from harness_rewrite.production_ledger import FORMAT, SCHEMA
    rig = Rig(tmp_path)
    secret = b"historical-private-tool-output" * 50000
    rig.context = replace(rig.context, artifacts=(ArtifactInput("native/usage/range", "old", secret, "private"),))
    before = rig.approve()
    old = v1_record(rig)
    git(rig.b, "pull", "--ff-only")
    for name in ("journals", "requests", "objects"):
        git(rig.b, "rm", "-r", ".harness/core/" + name)
    path = rig.b / f".harness/core/work-items/{WI}.json"
    path.write_text(json.dumps(old))
    format_path = rig.b / FORMAT
    binding = json.loads(format_path.read_text())
    binding["schema"] = SCHEMA
    format_path.write_text(json.dumps(binding))
    git(rig.b, "add", ".harness")
    git(rig.b, "commit", "-m", "Former production format fixture")
    git(rig.b, "push", "origin", "main")
    prior = rig.kernel.inspect(Query(WI), "native")
    converted = rig.ledger.reformat(WI)
    assert converted["preserved_decisions"] == len(old["entries"])
    assert converted["business_mutation"] is False
    assert rig.kernel.inspect(Query(WI), "native") == prior
    assert rig.approve()["result"]["original_request_result"] == before["result"]["original_request_result"]
    rig.ledger.finish_reformat()
    head = git(rig.remote, "rev-parse", "main")
    rig.ledger.reformat(WI)
    rig.ledger.finish_reformat()
    assert git(rig.remote, "rev-parse", "main") == head
    for sha, _, _ in objects(rig.remote):
        content = subprocess.check_output(["git", "-C", str(rig.remote), "cat-file", "blob", sha])
        assert secret not in content
        assert base64.b64encode(secret) not in content


def test_usage_facts_survive_without_raw_and_cross_wi_overlap_is_rejected(tmp_path):
    from tests_rewrite.test_production_ledger import workflow_rig
    from tests_rewrite.test_measurements import log, row, admission, values
    from tests_rewrite.test_workflow import DEFINITION, original
    rig = Rig(tmp_path)
    workflow = workflow_rig(rig, "lightweight")
    original(workflow.send("start_work", {}))
    path = log(tmp_path, row({"input": 2, "output": 5, "totalTokens": 7}))
    data = admission(workflow, path)
    original(workflow.send("record_usage", data, handle="usage", request="usage-one"))
    assert values(workflow)["usage"][0]["groups"][0]["tokens"]["totalTokens"]["value"] == 7
    first = workflow.wi
    response = workflow.kernel.execute(Command("second-light", "create_lightweight", {
        **DEFINITION, "why": "CR-ONE: second work"}), "create")
    workflow.wi = original(response)["work_item_id"]
    original(workflow.send("start_work", {}))
    data = admission(workflow, path)
    duplicate = workflow.send("record_usage", data, handle="usage", request="usage-two")
    assert duplicate["error"]["code"] == "domain_conflict", duplicate
    peer = rig.client(rig.backend(rig.b))
    assert peer.inspect(Query(first, view="measurements"), "native")["status"] == "read_only"


def test_unrelated_large_public_original_is_lazy_even_in_the_same_wi(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    content = b"large team deliverable" * 300000
    artifact = ArtifactInput("result", "v1", content, "team")
    rig.context = replace(rig.context, artifacts=(artifact,))
    assert rig.approve()["status"] == "local_committed"
    loaded = 0
    original = rig.ledger.blobs
    def observe(mapping):
        nonlocal loaded
        result = original(mapping)
        loaded += sum(map(len, result.values()))
        return result
    monkeypatch.setattr(rig.ledger, "blobs", observe)
    rig.context = replace(rig.context, artifacts=())
    assert rig.approve(request="without-original-read")["status"] == "local_committed"
    assert loaded < 100000
    peer = rig.client(rig.backend(rig.b))
    result = peer.inspect(Query(WI, view="artifact", artifact_ref=artifact.reference()), "native")
    assert base64.b64decode(result["result"]["artifact"]["base64"]) == content


class GateRig:
    """Lightweight WIs of one Agent on the real two-client ledger, for merge authority."""

    def __init__(self, tmp_path):
        from tests_rewrite.test_production_ledger import Rig as Ledger
        self.rig = Ledger(tmp_path)
        self.base = self.rig.context

    def run(self, context, intent, data, wi, request, kernel=None):
        from harness_rewrite.domain import INTENTS
        self.rig.context = replace(context, allowed_intents=INTENTS)
        return (kernel or self.rig.kernel).execute(Command(request, intent, data, wi), "native")

    @staticmethod
    def human(context, intent, data, wi):
        from harness_rewrite import SourceInput
        from harness_rewrite.model import digest
        return replace(context, source=SourceInput(
            "human-" + intent, "native_human", b"Approved", intent,
            digest({"intent": intent, "work_item_id": wi, "input": data})))

    @staticmethod
    def ok(response):
        assert response["status"] == "local_committed", response
        return response["result"]["original_request_result"]

    def started(self, name):
        from harness_rewrite import SourceInput
        created = self.run(replace(self.base, source=SourceInput("initial", "native_agent", b"Create")),
                           "create_lightweight",
                           {"summary": "Fixture work for tests.", "why": "w", "what": "x",
                            "acceptance": ["done"], "resources": [name], "owner": "producer"},
                           None, name + "-create")
        wi = created["work_item"]["id"]
        affiliation = {"type": "operating_area", "ref": "OA-PULSE", "work_class": "improvement",
                       "reason": "Human decision"}
        self.ok(self.run(self.human(self.base, "record_affiliation", affiliation, wi),
                         "record_affiliation", affiliation, wi, name + "-affiliation"))
        self.ok(self.run(replace(self.base, source=None), "start_work", {}, wi, name + "-start"))
        return wi

    def grant(self, wi, request, kernel=None):
        from tests_rewrite.test_pull_requests import GATE
        standing = {"reason": "Standing"}
        # The same approval bytes on every WI: one human message.
        return self.run(self.human(GATE, "grant_merge_authority", standing, wi),
                        "grant_merge_authority", standing, wi, request, kernel)


def test_a_merge_authority_revoked_on_its_carrier_before_publication_conflicts(tmp_path):
    from tests_rewrite import test_pull_requests as gate

    g = GateRig(tmp_path)
    rig = g.rig
    carrier, work = g.started("carrier"), g.started("work")
    grant = g.ok(g.grant(carrier, "grant"))["grant_id"]

    def submit(head, request):
        context = replace(gate.AUTHOR, pull_request_observation=gate.opened(head))
        return g.ok(g.run(context, "submit_pull_request",
                          {"repository": gate.REPO, "number": 7, "head_sha": head}, work, request))

    candidate = submit(gate.HEAD, "submit-1")["candidate_id"]
    reviewer = replace(gate.REVIEWER, session_compute={**gate.SOL, "declared_wis": [work]})
    for index, head in enumerate(("c" * 40, "d" * 40, None)):
        g.ok(g.run(reviewer, "record_pull_request_review",
                   {"candidate_id": candidate, "score": 70, "reason": "Findings"}, work, f"review-{index}"))
        if head:
            candidate = submit(head, f"submit-{index + 2}")["candidate_id"]
    fixed = submit("e" * 40, "submit-fixed")["candidate_id"]

    other = rig.client(rig.backend(rig.b))
    revoke = {"grant_id": grant, "reason": "Withdrawn"}

    def race(stage):
        if stage == "before_push":
            rig.ledger.fault = lambda _: None
            g.ok(g.run(gate.GATE, "revoke_merge_authority", revoke, carrier, "revoke", other))
            rig.context = replace(gate.AUTHOR, allowed_intents=g.rig.context.allowed_intents)

    confirm = {"candidate_id": fixed, "reason": "Fixed the third round findings"}
    rig.ledger.fault = race
    raced = g.run(gate.AUTHOR, "confirm_pull_request_merge", confirm, work, "confirm")
    assert raced["error"]["code"] == "ledger_conflict" and raced["error"]["retryable"] is True
    retried = g.run(gate.AUTHOR, "confirm_pull_request_merge", confirm, work, "confirm")
    assert retried["error"]["code"] == "source_invalid", retried


def test_one_approval_registers_one_merge_authority_across_the_ledger(tmp_path):
    g = GateRig(tmp_path)
    rig = g.rig
    first, second, third = g.started("first"), g.started("second"), g.started("third")
    other = rig.client(rig.backend(rig.b))

    def race(stage):
        if stage == "before_push":
            rig.ledger.fault = lambda _: None
            g.ok(g.grant(second, "grant-elsewhere", other))

    rig.ledger.fault = race
    raced = g.grant(first, "grant-first")
    assert raced["error"]["code"] == "ledger_conflict" and raced["error"]["retryable"] is True
    assert g.grant(first, "grant-first")["error"]["code"] == "domain_conflict"
    assert g.grant(third, "grant-third")["error"]["code"] == "domain_conflict"
