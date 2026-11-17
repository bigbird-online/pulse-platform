"""Synthetic native admission tests, not channel UAT."""

import hashlib
import json
from pathlib import Path

import pytest
from harness_rewrite import cli, channel
from harness_rewrite.model import Rejected, digest
from harness_rewrite.native_host import NativeHost
from tests_rewrite.test_native_host import native as native


@pytest.fixture
def daily(native, tmp_path, monkeypatch, capsys):
    policy, proc, session, inputs = native
    data = json.loads(policy.read_text())
    for name in ["delivery", "dispatch", "views"]:
        (tmp_path / name).mkdir()
        data[name + "_root"] = str(tmp_path / name)
    database = tmp_path / "facts.sqlite"
    data["database"] = str(database)
    policy.write_text(json.dumps(data))
    monkeypatch.setattr(
        cli, "NativeHost", lambda _: NativeHost(policy, proc=proc, pid=101)
    )
    monkeypatch.setattr(cli.sys, "argv", [str(Path(__file__).resolve())])
    command_file = tmp_path / "command.json"

    def run(args):
        code = cli.main(args)
        return code, json.loads(capsys.readouterr().out)

    def send(intent, payload, wi=None):
        command_file.write_text(
            json.dumps({"intent": intent, "work_item_id": wi, "input": payload})
        )
        code, result = run(["execute", "--input", str(command_file)])
        assert code == 0, result
        return result

    created = send(
        "create_lightweight",
        {
            "summary": "Fixture work for tests.",
            "why": "Native daily fixture",
            "what": "Preserve original channel command",
            "acceptance": ["Stored result"],
            "resources": ["repo"],
        },
    )
    wi = created["work_item"]["id"]
    send("start_work", {}, wi)

    def ingress(text, event="one", tenant="telegram:fixture", persist=True):
        source = {
            "channel": "telegram",
            "eventId": event,
            "humanRef": "human",
            "message": text,
        }
        ref = digest(["telegram", event, "human", tenant])
        (inputs / (ref + ".json")).write_text(
            json.dumps({"source": source, "poolKey": tenant})
        )
        if persist:
            with session.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "message",
                            "message": {
                                "role": "user",
                                "content": text
                                + "\n\n[Host human input reference: "
                                + ref
                                + ".]",
                            },
                        }
                    )
                    + "\n"
                )
        return ref

    return run, ingress, wi, database, session


def test_original_daily_command_commits_once_and_keeps_native_binding(daily):
    run, ingress, wi, database, _ = daily
    ref = ingress("/wi checkpoint " + wi + " 原消息检查点")
    args = ["channel", "--human-input-ref", ref]
    code, first = run(args)
    assert code == 0, first
    assert first["request_id"] == "channel." + ref
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    code, second = run(args)
    assert code == 0, second
    assert first["operation_id"] == second["operation_id"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    for command in ["status", "watch", "continue", ""]:
        ref = ingress("/wi " + command + " " + wi, command or "shorthand")
        code, result = run(["channel", "--human-input-ref", ref])
        assert code == 0, result
        assert result["work_item"]["generation"] == 1
        assert result["channel_command"]["backend"] == "native_core"


def test_missing_binding_arguments_and_wrong_provenance_never_mutate(daily):
    run, ingress, wi, database, _ = daily
    for index, (text, tenant, persist) in enumerate(
        [
            ("/wi reinit " + wi, "telegram:fixture", True),
            ("/wi cancel " + wi + " stop", "telegram:other", True),
            ("/wi cancel " + wi + " stop", "telegram:fixture", False),
            ("/wi rollover " + wi, "telegram:fixture", True),
            ("/wi status " + wi + " injected", "telegram:fixture", True),
        ]
    ):
        ref = ingress(text, str(index), tenant, persist)
        before = database.read_bytes()
        assert run(["channel", "--human-input-ref", ref])[0] == 2
        assert database.read_bytes() == before


def test_superseded_command_is_not_executed_and_explicit_reinit_is_idempotent(daily):
    run, ingress, wi, database, _ = daily
    old = ingress("/wi cancel " + wi + " old")
    ref = ingress("/wi reinit " + wi + " take control", "new")
    before = database.read_bytes()
    assert run(["channel", "--human-input-ref", old])[0] == 2
    assert database.read_bytes() == before
    code, result = run(["channel", "--human-input-ref", ref])
    assert code == 0, result
    assert result["work_item"]["generation"] == 2
    before = database.read_bytes()
    code, result = run(["channel", "--human-input-ref", ref])
    assert code == 0, result
    assert database.read_bytes() == before


def test_explicit_cancel_without_comment_preserves_original_request(daily):
    run, ingress, wi, database, _ = daily
    ref = ingress("/wi cancel " + wi)
    code, result = run(["channel", "--human-input-ref", ref])
    assert code == 0, result
    assert result["request_id"] == "channel." + ref
    assert result["work_item"]["business_progress"] == "terminated"
    assert result['result']['current_observation']['terminal']['termination'] == 'cancelled'
    before = database.read_bytes()
    assert run(["channel", "--human-input-ref", ref])[0] == 0
    assert database.read_bytes() == before


@pytest.mark.parametrize("text", ["/wi", "/wi help", "/wi@PulseBot list"])
def test_help_and_list_accept_real_channel_spelling(text):
    assert channel.parse(text)["action"] in {"help", "list"}


@pytest.mark.parametrize(
    "text", ["hello /wi list", "/wipe", "/wi list extra", "/wi checkpoint"]
)
def test_invalid_daily_vocabulary_is_rejected(text):
    with pytest.raises(Rejected):
        channel.parse(text)


def test_switch_names_exactly_one_work_item():
    from harness_rewrite import channel
    from harness_rewrite.model import Rejected
    import pytest as _pytest

    assert channel.parse("/wi switch wi_20261028_253") == {"action": "switch", "wi": "wi_20261028_253"}
    for text in ("/wi switch", "/wi switch wi_20261028_253 later", "/wi switch now"):
        with _pytest.raises(Rejected):
            channel.parse(text)
    assert "/wi switch WI" in channel.HELP["commands"]
