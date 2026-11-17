"""Each intent guide's example envelope is accepted by the kernel as written."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json

from harness_rewrite import Command, HostContext, Kernel, Query, SourceInput, cli
from harness_rewrite.domain import INTENTS
from harness_rewrite.guide import GUIDES, annotate, read, shape
from harness_rewrite.model import ArtifactInput, RepositoryBinding, Rejected, digest
from tests_rewrite.test_pull_requests import HEAD, OPUS, REPO, opened

COVERED = {
    "create_lightweight", "create_draft", "start_work", "adopt_plan", "revise_plan", "record_work",
    "checkpoint", "complete_lightweight", "submit_pull_request", "offer_collaboration",
    "respond_collaboration", "create_child", "review_child", "allocate_workspaces", "grant_access",
}
OWNER = HostContext(
    "team", "producer", "private-producer-session", "local", 0, frozenset({"*"}), INTENTS,
    repositories=(RepositoryBinding("repo-main", "/fixture/src", "/fixture/src/.git", "/fixture/managed"),),
    merge_repositories=frozenset({REPO}), session_compute=OPUS,
    pull_request_observation=opened(HEAD),
)
HELPER = replace(OWNER, actor="helper", session="private-helper-session")


class Rig:
    def __init__(self, tmp_path):
        self.contexts: dict[str, HostContext] = {}
        self.kernel = Kernel(tmp_path / "journal.sqlite", self.contexts.__getitem__,
                             clock=lambda: "2026-11-15T00:00:00Z")
        self.count = 0

    def generation(self, wi):
        if wi is None:
            return 0
        self.contexts["read"] = OWNER
        return self.kernel.inspect(Query(wi), "read")["work_item"]["generation"]

    def run(self, intent, values, actor=OWNER, envelope=None):
        """Execute the guide's example as the CLI would: placeholders, files, owner, source."""
        self.count += 1
        values = {**values, "<HUMAN_REF>": hashlib.sha256(intent.encode()).hexdigest()}
        text = json.dumps(envelope or GUIDES[intent]["example"])
        for key, value in values.items():
            text = text.replace(key, value)
        env = json.loads(text)
        assert "<" not in text, text
        artifacts = {name: ArtifactInput(row["logical"], row["version"], b"result", row.get("access", "team"))
                     for name, row in env.get("files", {}).items()}
        data = cli.substitute(env["input"], {k: a.reference() for k, a in artifacts.items()})
        if intent in {"create_draft", "create_lightweight"}:
            data.setdefault("owner", actor.actor)
        wi = env.get("work_item_id")
        ref = env.get("human_input_ref")
        source = (SourceInput(ref, "native_human", b"approved", intent,
                              digest({"intent": intent, "work_item_id": wi, "input": data}))
                  if ref else SourceInput(f"agent-{self.count}", "native_agent", b"agent"))
        handle = f"h{self.count}"
        self.contexts[handle] = replace(actor, generation=self.generation(wi or data.get("parent")),
                                        source=source, artifacts=tuple(artifacts.values()))
        result = self.kernel.execute(Command(f"r{self.count}", intent, data, wi), handle)
        assert result["status"] == "local_committed", (intent, result["error"])
        return result["result"]["original_request_result"]


def test_guides_cover_the_named_intents_and_match_validation():
    assert COVERED <= set(GUIDES) <= INTENTS
    for intent, guide in GUIDES.items():
        assert {"purpose", "work_item_id", "human_input_ref", "files", "required", "optional",
                "example", "rejections"} <= set(guide), intent
        example = guide["example"]
        assert example["intent"] == intent
        required, optional = shape(intent)
        keys = set(example["input"]) | ({"owner"} if "owner" in guide.get("host_filled", {}) else set())
        assert required <= keys <= required | optional, intent
        assert ("human_input_ref" in example) == guide["human_input_ref"].startswith("required"), intent
    index = read(None)["result"]["items"]
    assert [item["intent"] for item in index] == sorted(GUIDES)
    assert read("adopt_plan")["input"].startswith("input {plan_text, graph, resources?}")
    try:
        read("no_such_intent")
        raise AssertionError("unknown intent has no guide")
    except Rejected as exc:
        assert exc.code == "invalid_input" and "adopt_plan" in exc.reason


def test_every_example_envelope_executes_in_a_test_kernel(tmp_path):
    rig = Rig(tmp_path)
    light = rig.run("create_lightweight", {})["work_item_id"]
    v = {"<WI>": light}
    rig.run("start_work", v)
    rig.run("checkpoint", v)
    rig.run("record_work", {**v, "<ABSOLUTE_FILE>": "/fixture/result.md"})
    rig.run("grant_access", {**v, "<ABSOLUTE_DIR>": "fixture/notes"})
    rig.run("allocate_workspaces", {**v, "<REPO_ID>": "repo-main", "<BASE_COMMIT>": "c" * 40})
    rig.run("submit_pull_request", {**v, "<OWNER/REPO>": REPO, "<HEAD_SHA>": HEAD})
    draft = rig.run("create_draft", {})["work_item_id"]
    contract = rig.run("offer_collaboration", {**v, "<AGENT>": "helper", "<TARGET_WI>": draft})["contract_id"]
    rig.run("respond_collaboration", {"<OFFERING_WI>": light, "<CONTRACT_ID>": contract}, actor=HELPER)
    child = rig.run("create_child", {"<PARENT_WI>": light})["work_item_id"]
    rig.run("record_work", {"<WI>": child, "<ABSOLUTE_FILE>": "/fixture/child.md"})
    rig.run("review_child", {"<CHILD_WI>": child})
    # Standard: the confirmed plan, start at its revision, then a confirmed revision.
    rig.run("adopt_plan", {"<WI>": draft})
    rig.run("start_work", {}, envelope={"intent": "start_work", "work_item_id": draft,
                                        "input": {"graph_revision": 1}})
    rig.run("revise_plan", {"<WI>": draft})
    # Lightweight closing on a WI with no open child.
    closing = rig.run("create_lightweight", {})["work_item_id"]
    rig.run("start_work", {"<WI>": closing})
    record = rig.run("record_work", {"<WI>": closing, "<ABSOLUTE_FILE>": "/fixture/done.md"})["record_id"]
    rig.run("complete_lightweight", {"<WI>": closing, "<RECORD_ID>": record})


def test_shape_refusals_carry_the_input_guide_and_others_do_not():
    from harness_rewrite.kernel import Kernel as K

    shaped = annotate(K._error(Rejected("invalid_input", "Required business input is missing: graph")), "adopt_plan")
    guide = shaped["error"]["input_guide"]
    assert guide["intent"] == "adopt_plan" and "view=guide intent=adopt_plan" in guide["read"]
    assert guide["input"].startswith("input {plan_text, graph")
    assert "human_input_ref: required" in guide["input"]
    unknown = annotate(K._error(Rejected("invalid_input", "Intent is not implemented")), "adopt")
    assert "view=guide" in unknown["error"]["input_guide"]["read"]
    denied = annotate(K._error(Rejected("authority_denied", "No grant")), "adopt_plan")
    assert "input_guide" not in denied["error"]


def test_cli_reads_a_guide_without_policy_or_ledger(monkeypatch, capsys):
    monkeypatch.setattr(cli, "NativeHost", lambda _: (_ for _ in ()).throw(AssertionError("no host")))
    assert cli.main(["inspect", "--view", "guide", "--intent", "record_work"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["intent"] == "record_work" and out["guide"]["files"].startswith("required")
    assert cli.main(["inspect", "--view", "guide"]) == 0
    assert len(json.loads(capsys.readouterr().out)["result"]["items"]) == len(GUIDES)
    assert cli.main(["inspect", "--intent", "record_work"]) == 2
    assert "--intent requires" in json.loads(capsys.readouterr().out)["error"]["reason"]
    assert cli.main(["inspect", "--view", "guide", "--wi", "wi_x"]) == 2
    assert "Guide view accepts only" in json.loads(capsys.readouterr().out)["error"]["reason"]
