from dataclasses import replace
import json
import shutil

from harness_rewrite import Command
from harness_rewrite.views import Views
from tests_rewrite.test_review import produce, candidate_id
from tests_rewrite.test_workflow import (
    Rig,
    BASE,
    error,
    original,
    graph,
    node,
    workflow,
)


def test_goal_rework_preserves_original_candidate_and_requires_new_plan(tmp_path):
    rig = Rig(tmp_path)
    context = replace(
        BASE, allowed_intents=BASE.allowed_intents | {"amend_definition", "rename_work"}
    )
    rig.contexts["owner"] = context
    old = produce(rig)
    old_graph = workflow(rig.state())["graph"]
    data = {
        "previous_revision": 1,
        "why": "Reassessed outcome",
        "what": "Revised goal",
        "acceptance": ["A changed observable result"],
        "reason": "Human changed acceptance",
    }
    assert error(rig.send("amend_definition", data)) == "source_invalid"
    original(rig.approve("amend_definition", data, actor=context))
    assert candidate_id(rig) is None
    assert error(rig.claim("work")) == "authority_denied"
    state = rig.state()["result"]["current_observation"]
    assert state["candidates"]["items"][0]["id"] == old
    assert state["candidates"]["items"][0]["content"]["definition_revision"] == 1
    assert state["definition_revision"] == 2
    revised = graph(rig, [node("work", outputs=["result"])])
    revised.update(graph_id=old_graph["graph_id"], revision=2, definition_revision=2)
    original(
        rig.approve(
            "revise_plan",
            {
                "graph": revised,
                "plan_text": "Revised plan",
                "previous_revision": 1,
                "reuse_results": [],
                "reason": "New target",
            },
            actor=context,
        )
    )
    original(rig.claim("work", revision=2))
    original(
        rig.approve(
            "rename_work",
            {"title": "Clearer display title", "reason": "Naming only"},
            actor=context,
        )
    )
    assert workflow(rig.state())["node_stages"]["work"] == "running"


def test_all_disposable_views_can_disappear_or_lie_without_changing_facts(tmp_path):
    rig = Rig(tmp_path)
    rig.adopt(graph(rig))
    root = tmp_path / "views"
    views = Views(rig.kernel, root)
    first = views.read(rig.wi, "owner")
    revision = first["work_item"]["local_revision"]
    (root / (rig.wi + ".json")).write_text(
        json.dumps({"revision": 999999, "state": {"completed": True}})
    )
    assert views.read(rig.wi, "owner")["work_item"]["local_revision"] == revision
    assert json.loads((root / (rig.wi + ".json")).read_text())["revision"] == revision
    shutil.rmtree(root)
    assert views.read(rig.wi, "owner")["result"] == first["result"]
    assert (root / (rig.wi + ".json")).is_file()
    assert rig.state()["work_item"]["local_revision"] == revision


def test_view_redirection_never_hides_readable_journal(tmp_path):
    rig = Rig(tmp_path)
    root = tmp_path / "views"
    root.mkdir()
    asset = tmp_path / "user.txt"
    asset.write_text("user asset")
    (root / (rig.wi + ".json")).symlink_to(asset)
    response = Views(rig.kernel, root).read(rig.wi, "owner")
    assert response["status"] == "read_only"
    assert response["view_diagnostic"] == "view_unavailable"
    assert asset.read_text() == "user asset"


def test_added_requirement_keeps_original_definition_and_requires_new_plan(tmp_path):
    rig = Rig(tmp_path)
    intents = {"add_requirement", "amend_definition"}
    context = replace(BASE, allowed_intents=BASE.allowed_intents | intents)
    rig.contexts["owner"] = context
    rig.contexts["helper"] = replace(
        rig.contexts["helper"], allowed_intents=BASE.allowed_intents | intents
    )
    original_definition = rig.state()["result"]["current_observation"]["definition"]
    data = {
        "previous_revision": 1,
        "what": "Also keep a boundless work environment",
        "acceptance": ["The environment draft is preserved", "Its letter stays unsent"],
        "material_refs": ["github:bigbird-online/PULSE#1188"],
        "reason": "Owner adopted the ChatGPT draft into this WorkItem",
    }
    assert error(rig.send("add_requirement", data)) == "source_invalid"
    assert error(rig.approve("add_requirement", data, actor=rig.contexts["helper"])) == (
        "authority_denied"
    )
    assert error(rig.approve("add_requirement", {**data, "previous_revision": 0}, actor=context)) == (
        "domain_conflict"
    )
    assert error(rig.approve("add_requirement", {**data, "acceptance": []}, actor=context)) == (
        "invalid_input"
    )
    original(rig.approve("add_requirement", data, actor=context))
    state = rig.state()["result"]["current_observation"]
    definition = state["definition"]
    assert state["definition_revision"] == 2
    for key in ("why", "what", "owner", "resources"):
        assert definition[key] == original_definition[key]
    assert definition["acceptance"] == [*original_definition["acceptance"], *data["acceptance"]]
    assert definition["additions"] == [
        {
            "what": data["what"],
            "material_refs": data["material_refs"],
            "reason": data["reason"],
            "acceptance_from": 1,
        }
    ]

    # Executing work stops on the stale plan until the holder revises it.
    rig.adopt({**graph(rig), "definition_revision": 2})
    second = {**data, "previous_revision": 2, "what": "One more", "acceptance": ["Third"]}
    original(rig.approve("add_requirement", second, actor=context))
    assert rig.state()["result"]["current_observation"]["definition"]["additions"][1][
        "acceptance_from"
    ] == 3
    assert error(rig.claim("work")) == "authority_denied"

    # A whole rewrite supersedes the additions; history keeps them.
    rewrite = {
        "previous_revision": 3,
        "why": "Rewritten",
        "what": "Rewritten goal",
        "acceptance": ["Only this"],
        "reason": "Owner rewrote the goal",
    }
    original(rig.approve("amend_definition", rewrite, actor=context))
    assert "additions" not in rig.state()["result"]["current_observation"]["definition"]


def test_summary_is_required_at_creation_and_can_be_written_later(tmp_path):
    from harness_rewrite.domain import SUMMARY_GUIDE
    from tests_rewrite.test_workflow import DEFINITION

    rig = Rig(tmp_path)
    context = replace(BASE, allowed_intents=BASE.allowed_intents | {"rename_work"})
    rig.contexts["owner"] = context
    def view():
        return rig.state()["result"]["current_observation"]
    assert view()["summary"] == DEFINITION["summary"]
    original(rig.approve("rename_work", {"summary": "给人看的一句话简介。", "reason": "写简介"}, actor=context))
    assert view()["summary"] == "给人看的一句话简介。" and view()["title"]
    assert error(rig.approve("rename_work", {"reason": "nothing"}, actor=context)) == "invalid_input"
    for bad in ("", "两行\n简介", "长" * 121):
        assert error(rig.approve("rename_work", {"summary": bad, "reason": "r"}, actor=context)) == "invalid_input"
    missing = {k: v for k, v in DEFINITION.items() if k != "summary"}
    result = rig.kernel.execute(Command("no-summary", "create_lightweight", missing), "create")
    assert result["status"] == "rejected" and result["error"]["reason"] == SUMMARY_GUIDE


def test_title_defaults_to_the_summary_not_the_full_what(tmp_path):
    from tests_rewrite.test_workflow import DEFINITION

    rig = Rig(tmp_path)
    context = replace(BASE, allowed_intents=BASE.allowed_intents | {"rename_work"})
    rig.contexts["owner"] = context
    def view():
        return rig.state()["result"]["current_observation"]
    assert view()["title"] == DEFINITION["summary"] != DEFINITION["what"]
    original(rig.approve("rename_work", {"summary": "改过的简介。", "reason": "改简介"}, actor=context))
    assert view()["title"] == "改过的简介。"
    original(rig.approve("rename_work", {"title": "显式标题", "reason": "定标题"}, actor=context))
    assert view()["title"] == "显式标题"


def test_creation_rejection_names_the_fields_to_fix(tmp_path):
    # An Agent can only correct an input whose wrong fields it is told.
    from tests_rewrite.test_workflow import DEFINITION

    rig = Rig(tmp_path)
    missing = {k: v for k, v in DEFINITION.items() if k not in {"why", "resources"}}
    result = rig.kernel.execute(Command("no-why", "create_lightweight", missing), "create")
    assert result["error"]["reason"] == "Required business input is missing: resources, why"
    result = rig.kernel.execute(Command("titled", "create_lightweight", {**DEFINITION, "title": "x"}), "create")
    assert result["error"]["reason"] == "Unsupported business input field: title"
