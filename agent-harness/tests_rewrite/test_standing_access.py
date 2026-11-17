"""Standing read grants of an Agent: the family rule admits, Relay reads them back."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from harness_rewrite import Command, Query, access, cli
from harness_rewrite.relay_channel import standing_access
from tests_rewrite.test_relay_session import rig as rig  # noqa: F401
from tests_rewrite.test_native_host import native as native  # noqa: F401
from tests_rewrite.test_session_access import execute, human


@pytest.fixture
def family(rig, tmp_path):
    """The native session is avatar vigil-33; vigil is its base, steward another Agent."""
    _, _, kernel, host, _, proc = rig
    root = tmp_path / "Coevolution"
    base, avatar, other = (
        root / "PARA/vigil-space",
        root / "Bigbird/Oikos/tenants/33/vigil-33-space",
        root / "PARA/steward-space",
    )
    for path in (base, avatar, other):
        path.mkdir(parents=True)
    host.profile.update(actor="vigil-33", workspace_roots=[str(avatar)])
    for actor, workspace in (("vigil", base), ("steward", other)):
        sessions = tmp_path / (actor + "-sessions")
        sessions.mkdir()
        host.policy["profiles"].append({"role": "producer", "actor": actor, "session_root": str(sessions),
                                        "artifact_roots": [str(workspace)], "workspace_roots": [str(workspace)]})
    (tmp_path / "host-policy.json").write_text(json.dumps(host.policy))
    host.bind_context(None)
    payload = {"summary": "Family fixture.", "why": "fixture", "what": "read family",
               "acceptance": ["read"], "owner": "vigil-33", "resources": ["repo"]}
    host.prepare("create_lightweight", payload, None, 0, (), None)
    wi = kernel.execute(Command("family-create", "create_lightweight", payload), host.handle)["work_item"]["id"]
    host.current = replace(host.base, generation=1)
    started = kernel.execute(Command("family-start", "start_work", {}, wi), host.handle)
    assert started["status"] == "local_committed", started
    return kernel, host, wi, proc, {"base": str(base), "avatar": str(avatar), "other": str(other)}


def relay_read(family):
    _, host, _, proc, _ = family
    return standing_access(Path(host.policy["human_inputs"]).parent / "harness.pyz", {},
                           proc=proc, caller=55)


def test_the_family_rule_admits_a_standing_read_of_the_base_workspace(family, monkeypatch, capsys):
    kernel, host, wi, _, roots = family
    data = {"paths": [roots["base"]], "mode": "read", "reason": "read my base", "standing": True}
    granted = execute(kernel, host, "family-grant", "grant_access", data, wi)
    assert granted["status"] == "local_committed", granted
    assert granted["result"]["original_request_result"]["admission"] == "family_rule"
    read = relay_read(family)
    assert read["coverage"] == "complete"
    assert [(r["actor"], r["paths"], r["mode"], r["admission"], r["work_item_id"])
            for r in read["grants"]] == [("vigil-33", [roots["base"]], "read", "family_rule", wi)]
    # The session readback carries the same standing grant beside session grants.
    monkeypatch.setattr(cli.sys, "argv", [str(Path(host.policy["human_inputs"]).parent / "harness.pyz")])
    assert cli.main(["access", "--wi", wi], _host=host) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["grants"] == [] and [r["paths"] for r in response["standing"]] == [[roots["base"]]]
    # It is readable back on its carrier WI as a standing grant.
    view = kernel.inspect(Query(wi), host.handle)
    assert [r["admission"] for r in view["result"]["current_observation"]["access_grants"]] == ["family_rule"]


@pytest.mark.parametrize("case", ["other_family", "inside_base", "write"])
def test_a_request_outside_the_family_rule_is_not_auto_admitted(family, case):
    kernel, host, wi, _, roots = family
    paths = {"other_family": [roots["other"]], "inside_base": [roots["base"] + "/memory"],
             "write": [roots["base"]]}[case]
    data = {"paths": paths, "mode": "write" if case == "write" else "read", "reason": "r", "standing": True}
    refused = execute(kernel, host, "family-" + case, "grant_access", data, wi)
    assert refused["status"] == "rejected"
    assert refused["error"]["code"] == ("invalid_input" if case == "write" else "source_invalid")
    assert relay_read(family)["grants"] == []


def test_a_non_family_standing_read_needs_the_human(family):
    kernel, host, wi, _, roots = family
    data = {"paths": [roots["other"]], "mode": "read", "reason": "shared ledger", "standing": True}
    approve = human(host, "family-approve", "同意常驻只读")
    granted = execute(kernel, host, "family-human", "grant_access", data, wi, approve)
    assert granted["status"] == "local_committed", granted
    assert [r["admission"] for r in relay_read(family)["grants"]] == ["human"]


def test_the_agent_revokes_its_standing_grant_without_approval(family):
    kernel, host, wi, _, roots = family
    data = {"paths": [roots["base"]], "mode": "read", "reason": "read my base", "standing": True}
    grant_id = execute(kernel, host, "family-grant", "grant_access", data, wi)["result"]["original_request_result"]["grant_id"]
    revoked = execute(kernel, host, "family-revoke", "revoke_access", {"grant_id": grant_id, "reason": "done"}, wi)
    assert revoked["status"] == "local_committed", revoked
    assert relay_read(family)["grants"] == []


def test_only_the_relay_reads_standing_access(family):
    from harness_rewrite.model import Rejected
    from tests_rewrite.test_relay_session import process

    _, host, _, proc, _ = family
    process(proc, 104, 101, [proc.parent / "node"])
    with pytest.raises(Rejected):
        standing_access(Path(host.policy["human_inputs"]).parent / "harness.pyz", {},
                        proc=proc, caller=104)


LAYOUT = {
    "vigil": ["/c/PARA/vigil-space"],
    "vigil-33": ["/c/Oikos/tenants/33/vigil-33-space"],
    "vigil-xb": ["/c/Oikos/tenants/xb/vigil-xb-space"],
    "vigil-lab": ["/c/PARA/vigil-lab-space"],
    "steward": ["/c/PARA/steward-space"],
}


def test_family_is_base_and_avatar_in_the_tenant_layout():
    assert access.family("vigil", LAYOUT) == {LAYOUT["vigil-33"][0], LAYOUT["vigil-xb"][0]}
    assert access.family("vigil-33", LAYOUT) == {LAYOUT["vigil"][0]}
    # A hyphenated name outside tenants/<member>/ is no avatar; siblings are not family.
    assert access.family("vigil-lab", LAYOUT) == set()
    assert LAYOUT["vigil-xb"][0] not in access.family("vigil-33", LAYOUT)
    assert access.family("steward", LAYOUT) == set()


def test_a_standing_grant_outlives_its_carrier_but_not_the_layout():
    grant = {"grant_id": "g", "principal": {"actor": "vigil-33", "session": "s"}, "paths": LAYOUT["vigil"],
             "mode": "read", "reason": "r", "active": True, "scope": "agent", "admission": "family_rule"}
    state = {"status": "completed", "access": {"g": grant}}
    assert access.standing(state, "vigil-33", LAYOUT) == [grant]
    assert access.active(state) == []
    moved = {**LAYOUT, "vigil-33": ["/c/elsewhere/vigil-33-space"]}
    assert access.standing(state, "vigil-33", moved) == []
    assert access.standing({**state, "access": {"g": {**grant, "admission": "human"}}}, "vigil-33", moved)
