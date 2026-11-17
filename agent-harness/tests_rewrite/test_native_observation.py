"""Native CLI capability observations use current facts without minting grants."""

import hashlib
import json
from pathlib import Path

from harness_rewrite import cli
from harness_rewrite.native_host import NativeHost
from tests_rewrite.test_native_host import native as native


def test_native_query_and_post_effect_view_preserve_current_authority(
    native,
    tmp_path,
    monkeypatch,
    capsys,
):
    policy, proc, session, _ = native
    data = json.loads(policy.read_text())
    for name in ["delivery", "dispatch", "views"]:
        (tmp_path / name).mkdir()
    database = tmp_path / "facts.sqlite"
    data.update(
        database=str(database),
        delivery_root=str(tmp_path / "delivery"),
        dispatch_root=str(tmp_path / "dispatch"),
        views_root=str(tmp_path / "views"),
    )
    policy.write_text(json.dumps(data))
    monkeypatch.setattr(
        cli, "NativeHost", lambda _: NativeHost(policy, proc=proc, pid=101)
    )
    monkeypatch.setattr(cli.sys, "argv", [str(Path(__file__).resolve())])
    command_file = tmp_path / "command.json"

    def run(argv):
        assert cli.main(argv) == 0
        return json.loads(capsys.readouterr().out)

    def send(intent, payload, wi=None):
        command_file.write_text(
            json.dumps({"intent": intent, "work_item_id": wi, "input": payload})
        )
        return run(["execute", "--input", str(command_file)])

    def local_work(response):
        return next(
            c["state"] for c in response["capabilities"] if c["name"] == "local_work"
        )

    created = send(
        "create_lightweight",
        {
            "summary": "Fixture work for tests.",
            "why": "Native boundary fixture",
            "what": "Observe real journal grants",
            "acceptance": ["A stored result"],
            "resources": ["repo"],
        },
    )
    wi = created["work_item"]["id"]
    assert local_work(created["latest_observation"]) == "unavailable"
    started = send("start_work", {}, wi)
    assert started["work_item"]["generation"] == 1
    assert local_work(started["latest_observation"]) == "available"
    checkpoint = send(
        "checkpoint", {"summary": "Native fixture progress", "resources": ["repo"]}, wi
    )
    assert local_work(checkpoint["latest_observation"]) == "available"
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    for action in ["inspect", "resume"]:
        view = run([action, "--wi", wi])
        assert local_work(view) == "available"
        assert view["work_item"] == checkpoint["latest_observation"]["work_item"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    scanned = run(
        ["inspect", "--view", "scan", "--limit", "1", "--age-threshold-seconds", "60"]
    )
    assert scanned["result"]["view"] == "scan"
    assert scanned["result"]["total_count"] == 1
    assert scanned["result"]["items"][0]["id"] == wi
    assert scanned["result"]["items"][0]["watch"]["summary"]["waiting_items"] == 1
    assert all(c["name"] == "local_read" for c in scanned["capabilities"])
    before_views = {
        str(p): p.read_bytes() for p in (tmp_path / "views").rglob("*") if p.is_file()
    }
    for view_name in ["state", "watch"]:
        historical = run(
            ["inspect", "--wi", wi, "--view", view_name, "--revision", "1"]
        )
        assert historical["work_item"]["local_revision"] == 1
        assert historical["result"]["observation_is_historical"] is True
        assert local_work(historical) == "available"
        if view_name == "watch":
            assert historical["result"]["items"]["source_revision"] == 1
    assert before_views == {
        str(p): p.read_bytes() for p in (tmp_path / "views").rglob("*") if p.is_file()
    }
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    # A new native session in the same admitted profile can read this WI but
    # cannot use the previous session's grant merely by observing its generation.
    session.write_text(
        json.dumps({"type": "session", "id": "different-native-session"}) + "\n"
    )
    for action in ["inspect", "resume"]:
        assert local_work(run([action, "--wi", wi])) == "unavailable"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest


def test_native_create_child_runs_at_the_parent_s_generation(native, tmp_path, monkeypatch, capsys):
    policy, proc, _, _ = native
    data = json.loads(policy.read_text())
    for name in ["delivery", "dispatch", "views"]:
        (tmp_path / name).mkdir()
    data.update(database=str(tmp_path / "facts.sqlite"), delivery_root=str(tmp_path / "delivery"),
                dispatch_root=str(tmp_path / "dispatch"), views_root=str(tmp_path / "views"))
    policy.write_text(json.dumps(data))
    monkeypatch.setattr(cli, "NativeHost", lambda _: NativeHost(policy, proc=proc, pid=101))
    monkeypatch.setattr(cli.sys, "argv", [str(Path(__file__).resolve())])
    command_file = tmp_path / "command.json"

    def send(intent, payload, wi=None):
        command_file.write_text(json.dumps({"intent": intent, "work_item_id": wi, "input": payload}))
        cli.main(["execute", "--input", str(command_file)])
        return json.loads(capsys.readouterr().out)

    definition = {"summary": "Fixture work for tests.", "why": "Native boundary fixture",
                  "what": "Observe real journal grants", "acceptance": ["A stored result"],
                  "resources": ["repo"]}
    parent = send("create_lightweight", definition)["work_item"]["id"]
    send("start_work", {}, parent)
    child = send("create_child", {**definition, "parent": parent})
    assert child["status"] == "local_committed", child
    assert child["latest_observation"]["result"]["current_observation"]["parent"] == parent
