"""Human-approved session access: recorded by the kernel, read back for Relay."""

import json
from pathlib import Path

import pytest
from harness_rewrite import Command, access, cli
from harness_rewrite.model import Rejected, digest
from tests_rewrite.test_relay_session import rig as rig, process  # noqa: F401
from tests_rewrite.test_native_host import native as native  # noqa: F401


def human(host, event, message):
    """One authenticated Relay ingress that entered this native session."""
    tenant = "telegram:fixture"
    ref = digest(["telegram", event, "human", tenant])
    record = Path(host.policy["human_inputs"]) / (ref + ".json")
    source = {"channel": "telegram", "eventId": event, "humanRef": "human", "message": message}
    record.write_text(json.dumps({"source": source, "poolKey": tenant}))
    record.chmod(0o600)
    with host.native_log().open("a") as stream:
        row = {"role": "user", "content": f"{message} [Host human input reference: {ref}.]"}
        stream.write(json.dumps({"type": "message", "message": row}) + "\n")
    return ref


def execute(kernel, host, request, intent, data, wi, ref=None):
    try:
        host.prepare(intent, data, wi, 1, (), ref)
    except Rejected as exc:
        return {"status": "rejected", "error": {"code": exc.code}}
    return kernel.execute(Command(request, intent, data, wi), host.handle)


def grants(host, wi, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", [str(Path(host.policy["human_inputs"]).parent / "harness.pyz")])
    assert cli.main(["access", "--wi", wi], _host=host) == 0
    return json.loads(capsys.readouterr().out)["grants"]


def test_access_needs_the_latest_human_approval_and_ends_with_revocation(rig, monkeypatch, capsys):
    _, _, kernel, original, wi, _ = rig
    data = {"paths": ["/Users/fixture/.codex/worktrees"], "mode": "write", "reason": "clean legacy"}
    missing = execute(kernel, original, "no-human", "grant_access", data, wi)
    assert missing["error"]["code"] == "human_decision_required"
    earlier = human(original, "earlier", "yes, open it")
    human(original, "later", "something else")
    assert execute(kernel, original, "stale-human", "grant_access", data, wi, earlier)["status"] == (
        "rejected"
    )
    approve = human(original, "approve", "同意开放该目录")
    granted = execute(kernel, original, "grant", "grant_access", data, wi, approve)
    assert granted["status"] == "local_committed", granted
    rows = grants(original, wi, monkeypatch, capsys)
    assert [(r["paths"], r["mode"]) for r in rows] == [(data["paths"], "write")]
    revoke = human(original, "revoke", "撤销")
    reason = {"grant_id": rows[0]["grant_id"], "reason": "done"}
    assert execute(kernel, original, "revoke", "revoke_access", reason, wi, revoke)["status"] == (
        "local_committed"
    )
    assert grants(original, wi, monkeypatch, capsys) == []


@pytest.mark.parametrize("paths", [["relative/path"], ["/a/../b"], ["/a", "/a"], []])
def test_access_paths_are_normalized_absolute_and_bounded(rig, paths):
    _, _, kernel, original, wi, _ = rig
    ref = human(original, "bad-" + str(len(paths)), "同意")
    data = {"paths": paths, "mode": "read", "reason": "fixture"}
    assert execute(kernel, original, "bad", "grant_access", data, wi, ref)["status"] == "rejected"


def test_a_finished_work_item_grants_no_access():
    grant = {"grant_id": "g", "principal": {"session": "s"}, "paths": ["/x"], "mode": "read",
             "reason": "r", "active": True}
    assert access.active({"status": "active", "access": {"g": grant}})
    assert access.active({"status": "completed", "access": {"g": grant}}) == []
    assert access.active({"status": "terminated", "access": {"g": grant}}) == []


def test_relay_reads_the_grants_of_its_native_child_by_pid(rig, monkeypatch, capsys):
    from harness_rewrite.relay_channel import access_host

    _, _, kernel, original, wi, proc = rig
    data = {"paths": ["/Users/fixture/shared"], "mode": "read", "reason": "inventory"}
    approve = human(original, "relay-approve", "同意")
    assert execute(kernel, original, "grant", "grant_access", data, wi, approve)["status"] == (
        "local_committed"
    )
    entry = proc.parent / "harness.pyz"
    host = access_host(entry, {"pid": 101}, proc=proc, caller=55)
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    assert cli.main(["access"], _host=host) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["work_item_id"] == wi
    assert [r["paths"] for r in response["grants"]] == [data["paths"]]
    # Its WI has no allocated workspace, so none opens (test_workspaces reads one back).
    assert response["workspaces"] == []
    # An Agent's own tool process is the Relay's grandchild and cannot ask for access.
    process(proc, 104, 101, [proc.parent / "node"])
    with pytest.raises(Rejected):
        access_host(entry, {"pid": 101}, proc=proc, caller=104)


CHILD = {
    "summary": "Child fixture work for tests.",
    "why": "fixture",
    "what": "one child work",
    "acceptance": ["checked"],
    "resources": ["repo/child"],
}


def test_a_parent_stands_for_its_children_in_the_access_readback(rig, monkeypatch, capsys):
    from harness_rewrite.relay_channel import access_host

    _, _, kernel, original, wi, proc = rig
    created = execute(kernel, original, "child", "create_child", {**CHILD, "parent": wi}, None)
    assert created["status"] == "local_committed", created
    child = created["work_item"]["id"]
    on_parent = {"paths": ["/Users/fixture/parent"], "mode": "read", "reason": "parent"}
    on_child = {"paths": ["/Users/fixture/child"], "mode": "write", "reason": "child"}
    for request, data, target in (("p", on_parent, wi), ("c", on_child, child)):
        approve = human(original, "approve-" + request, "同意")
        granted = execute(kernel, original, request, "grant_access", data, target, approve)
        assert granted["status"] == "local_committed", granted
    entry = proc.parent / "harness.pyz"
    monkeypatch.setattr(cli.sys, "argv", [str(entry)])
    assert cli.main(["access"], _host=access_host(entry, {"pid": 101}, proc=proc, caller=55)) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["work_item_id"] == wi
    assert [r["paths"] for r in response["grants"]] == [on_parent["paths"], on_child["paths"]]
    assert cli.bound_work_item(kernel, original.handle) == wi
    assert cli.held_work_item(kernel, original.handle) == wi


def test_two_unrelated_bindings_still_conflict():
    one = {"id": "wi_a", "parent": None}
    family = {"items": [one, {"id": "wi_b", "parent": "wi_a"}], "next_offset": None}
    assert cli.binding_family(family, "conflict") == ["wi_a", "wi_b"]
    assert cli.binding_family({"items": [], "next_offset": None}, "conflict") == []
    for result in (
        {"items": [one, {"id": "wi_c", "parent": None}], "next_offset": None},
        {"items": [one, {"id": "wi_d", "parent": "wi_other"}], "next_offset": None},
        {"items": [{"id": "wi_d", "parent": "wi_other"}], "next_offset": None},
        {"items": [one], "next_offset": 1},
    ):
        with pytest.raises(Rejected) as raised:
            cli.binding_family(result, "conflict")
        assert raised.value.code == "domain_conflict"


def test_a_merge_authority_follows_the_latest_human_word_and_one_approval_grants_once(rig):
    _, _, kernel, original, wi, _ = rig
    data = {"reason": "大鸟: 第三轮后修完直接合并"}
    missing = execute(kernel, original, "merge-no-human", "grant_merge_authority", data, wi)
    assert missing["error"]["code"] == "human_decision_required"
    approve = human(original, "merge-approve", "同意长期授权")
    granted = execute(kernel, original, "merge-grant", "grant_merge_authority", data, wi, approve)
    assert granted["status"] == "local_committed", granted
    grant = granted["result"]["original_request_result"]["grant_id"]
    # The Agent gives it up by itself; the approval is still the latest input,
    # yet one approval registers one authority.
    revoke = {"grant_id": grant, "reason": "Agent gives it up"}
    assert execute(kernel, original, "merge-drop", "revoke_merge_authority", revoke, wi)["status"] == (
        "local_committed")
    again = execute(kernel, original, "merge-again", "grant_merge_authority", data, wi, approve)
    assert again["error"]["code"] == "domain_conflict", again
    # A fresh approval grants anew; a later human revocation outlives the old approval.
    renewed = human(original, "merge-renew", "再次同意")
    second = execute(kernel, original, "merge-renewed", "grant_merge_authority", data, wi, renewed)
    assert second["status"] == "local_committed", second
    withdraw = human(original, "merge-withdraw", "撤销长期合并授权")
    revoke = {"grant_id": second["result"]["original_request_result"]["grant_id"], "reason": "撤销"}
    assert execute(kernel, original, "merge-revoke", "revoke_merge_authority", revoke, wi,
                   withdraw)["status"] == "local_committed"
    for request, ref in (("merge-replay-first", approve), ("merge-replay-second", renewed)):
        replayed = execute(kernel, original, request, "grant_merge_authority", data, wi, ref)
        assert replayed["error"]["code"] == "source_invalid", replayed
    # An approval never used, overtaken by a later message, is no longer the human's word.
    unused = human(original, "merge-unused", "同意")
    human(original, "merge-overtaken", "先别授权")
    stale = execute(kernel, original, "merge-stale", "grant_merge_authority", data, wi, unused)
    assert stale["error"]["code"] == "source_invalid", stale
