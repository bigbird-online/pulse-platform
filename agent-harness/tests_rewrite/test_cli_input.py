"""The execute command names an unreadable or malformed --input as such."""

from __future__ import annotations

import json

from tests_rewrite.test_native_host import native as native  # noqa: F401


def test_execute_input_must_be_a_readable_json_file(native, tmp_path, monkeypatch, capsys):
    from harness_rewrite import cli
    from harness_rewrite.native_host import NativeHost

    file, proc, _, _ = native
    data = json.loads(file.read_text())
    for name in ["delivery", "dispatch", "views"]:
        (tmp_path / name).mkdir()
    data.update(
        database=str(tmp_path / "facts.sqlite"),
        delivery_root=str(tmp_path / "delivery"),
        dispatch_root=str(tmp_path / "dispatch"),
        views_root=str(tmp_path / "views"),
    )
    file.write_text(json.dumps(data))
    monkeypatch.setattr(cli, "NativeHost", lambda _: NativeHost(file, proc=proc, pid=101))
    monkeypatch.setattr(cli.sys, "argv", [__file__])

    def run(value):
        assert cli.main(["execute", "--input", value]) == 2
        return json.loads(capsys.readouterr().out)["error"]

    # A caller passing the JSON text itself instead of a file path.
    inline = run('{"intent":"offer_collaboration"}')
    assert inline["code"] == "invalid_input"
    assert "readable JSON file" in inline["reason"]
    malformed = tmp_path / "command.json"
    malformed.write_text("{")
    assert run(str(malformed)) == {
        "code": "invalid_input", "reason": "--input is not valid JSON", "retryable": False,
    }
    # The two shapes WI 498 tried: a bare path, and a row without logical/version.
    for row in ["/abs/report.html", {"path": "/abs/report.html"}]:
        command = tmp_path / "files.json"
        command.write_text(json.dumps({
            "intent": "record_work", "work_item_id": "wi_20261028_498",
            "input": {"summary": "s", "resources": ["repo"], "artifacts": [{"file": "slides"}]},
            "files": {"slides": row},
        }))
        error = run(str(command))
        assert error["code"] == "invalid_input"
        assert "files.slides must be {path, logical, version[, access]}" in error["reason"]

    # Vigil 2026-11-09/10: files passed as a list of paths crashed the core with no
    # output, so the caller saw only "unknown" and read back absent forever.
    command = tmp_path / "list.json"
    command.write_text(json.dumps({
        "intent": "respond_collaboration", "work_item_id": "wi_20261015_422",
        "input": {"contract_id": "op_x", "decision": "complete", "evidence": [{"file": "probe.md"}]},
        "files": ["/abs/probe.md"],
    }))
    error = run(str(command))
    assert error["code"] == "invalid_input"
    assert error["reason"].startswith("files must be an object {name: {path, logical, version[, access]}}")
    assert error["reason"].endswith("not list")
    # A shape refusal names the intent's input and where its full guide is read.
    assert error["input_guide"]["intent"] == "respond_collaboration"
    assert "view=guide intent=respond_collaboration" in error["input_guide"]["read"]
    command.write_text(json.dumps({"intent": "checkpoint", "input": ["summary"]}))
    assert run(str(command))["reason"] == "input must be an object, not list"
