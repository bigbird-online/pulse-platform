#!/usr/bin/env python3
"""Real bundled-Codex behavior test; no GUI control or production enrollment.

User messages never command an MCP call. The candidate supplies the same short rule
through MCP metadata, optionally also the native startup instruction channel. The test inspects its own JSON event stream,
not any existing personal conversation. A failed expectation is not retried.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

PROMPTS = [
    "还没确定这段会话所属的 WI。先聊写作：怎样让文章更简洁？请用一句话回答。",
    "现在明确：本 session 属于 wi_20260825_300。请把‘我们需要在未来推进实施有关改善’改写成一句直白的话。",
    "继续，把刚才那句话再缩短一点。",
    "再举一个主动语态的例子。",
]


def run(
    codex: Path,
    source: Path,
    report: Path,
    *,
    runtime_instruction=False,
    workspace=None,
):
    report.mkdir(mode=0o700, parents=True, exist_ok=True)
    if workspace is None or not Path(workspace).is_dir():
        raise ValueError(
            "explicit registered test workspace required before model invocation"
        )
    workspace = Path(workspace).resolve()
    registry = json.loads(
        (Path.home() / ".local/state/pulse/compute-cost/sources.json").read_text()
    )
    actors = {
        rule["agent"]
        for source in registry.get("sources", [])
        if source.get("kind") == "codex" and source.get("actor_attribution") is True
        for rule in source.get("workspace_actors", [])
        if Path(rule["workspace_root"]).resolve() == workspace
        and rule["runtime"] == "codex"
    }
    if actors != {"pulse"}:
        raise ValueError("test workspace must have one registered Pulse executor")
    home = Path.home()
    protected = [
        home / ".codex/config.toml",
        home / ".codex/hooks.json",
        home / ".local/state/pulse/compute-cost/sources.json",
    ]
    before = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in protected
        if p.exists()
    }
    config = tomllib.loads((home / ".codex/config.toml").read_text())
    model = config.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("existing model setting required")
    python = home / ".local/share/coevolution/python-env/bin/python"
    script = source / "ComputeCost/runtime/codex-mcp-request-purpose.py"
    proxy = report / "mcp-wire-proxy.py"
    wire = report / "mcp-wire.jsonl"
    proxy.write_text(
        "import json, subprocess, sys, threading\nfrom pathlib import Path\nwire=Path(sys.argv[2])\nchild=subprocess.Popen([sys.executable,sys.argv[1]],stdin=subprocess.PIPE,stdout=subprocess.PIPE)\nlock=threading.Lock()\ndef record(value):\n    with lock, wire.open('a') as out: out.write(json.dumps(value,ensure_ascii=False)+'\\n')\ndef incoming():\n    for raw in sys.stdin.buffer:\n        value=json.loads(raw)\n        record({'direction':'in','method':value.get('method'),'id':value.get('id')})\n        child.stdin.write(raw); child.stdin.flush()\n    child.stdin.close()\nthreading.Thread(target=incoming,daemon=True).start()\nfor raw in child.stdout:\n    value=json.loads(raw); record({'direction':'out','id':value.get('id'),'result':value.get('result'),'error':value.get('error')})\n    sys.stdout.buffer.write(raw); sys.stdout.buffer.flush()\nsys.exit(child.wait())\n"
    )
    common = [
        str(codex),
        "exec",
        "--json",
        "--color",
        "never",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-m",
        model,
        "-c",
        "model_reasoning_effort="
        + json.dumps(config.get("model_reasoning_effort", "medium")),
        "-c",
        "mcp_servers.pulse-request-purpose.required=true",
        "-c",
        "features.hooks=false",
        "-c",
        "features.shell_tool=false",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "mcp_servers.pulse-request-purpose.command=" + json.dumps(str(python)),
        "-c",
        "mcp_servers.pulse-request-purpose.args="
        + json.dumps([str(proxy), str(script), str(wire)]),
    ]
    instruction = None
    if runtime_instruction:
        sys.path.insert(0, str(source / "ComputeCost"))
        from pulse_compute_cost.mcp_request_purpose import handle

        instruction = handle({"method": "initialize"}, clock=lambda: None)[
            "instructions"
        ]
        common += ["-c", "developer_instructions=" + json.dumps(instruction)]
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SCL_", "HARNESS_", "BROKER_", "PULSE_"))
        and k not in {"GH_TOKEN", "GITHUB_TOKEN", "PYTHONPATH", "PYTHONHOME"}
    }
    thread = None
    rounds = []
    result = {
        "schema": "session_wi_agent_test/v1",
        "gui_test": False,
        "instruction_delivery": "runtime_startup"
        if runtime_instruction
        else "mcp_metadata_only",
        "runtime_instruction": instruction,
        "model": model,
        "reasoning_effort": config.get("model_reasoning_effort", "medium"),
        "runtime_version": subprocess.check_output(
            [str(codex), "--version"], text=True
        ).strip(),
        "expected_call_counts": [0, 1, 0, 0],
        "prompts": PROMPTS,
        "rounds": rounds,
        "pass": False,
    }
    try:
        for i, prompt in enumerate(PROMPTS):
            argv = common + ([prompt] if thread is None else ["resume", thread, prompt])
            completed = subprocess.run(
                argv,
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
            )
            (report / f"round-{i + 1}.jsonl").write_text(completed.stdout)
            (report / f"round-{i + 1}.stderr").write_text(completed.stderr)
            events = [
                json.loads(x)
                for x in completed.stdout.splitlines()
                if x.startswith("{")
            ]
            seen = [e["thread_id"] for e in events if e.get("type") == "thread.started"]
            if thread is None and seen:
                thread = seen[0]
            if seen and any(x != thread for x in seen):
                raise ValueError("resumed a different session")
            calls = [
                e["item"]
                for e in events
                if e.get("type") == "item.completed"
                and e.get("item", {}).get("type") == "mcp_tool_call"
            ]
            purposes = [
                c
                for c in calls
                if c.get("server") == "pulse-request-purpose"
                and c.get("tool") == "declare_session_wi"
            ]
            forbidden = [
                e
                for e in events
                if e.get("type") == "item.completed"
                and e.get("item", {}).get("type")
                in {"command_execution", "file_change"}
            ]
            rounds.append(
                {
                    "number": i + 1,
                    "returncode": completed.returncode,
                    "calls": purposes,
                    "other_mcp_calls": len(calls) - len(purposes),
                    "forbidden_effects": len(forbidden),
                }
            )
            (report / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
            if completed.returncode or thread is None:
                raise ValueError("native round failed")
            if (
                len(purposes) != [0, 1, 0, 0][i]
                or len(calls) != len(purposes)
                or forbidden
            ):
                raise ValueError("unexpected native tool behavior")
            for c in purposes:
                returned = c.get("result") or {}
                value = returned.get("structured_content") or {}
                blocks = returned.get("content") or []
                text_value = (
                    json.loads(blocks[0]["text"])
                    if len(blocks) == 1 and blocks[0].get("type") == "text"
                    else None
                )
                expected_scope = (
                    "ccf1_"
                    + hashlib.sha256(
                        json.dumps(
                            ["native_session", ["codex", thread]], separators=(",", ":")
                        ).encode()
                    ).hexdigest()
                )
                if (
                    c.get("status") != "completed"
                    or c.get("arguments") != {"wi": "wi_20260825_300"}
                    or c.get("error") is not None
                    or text_value != value
                    or value.get("native_scope_ref") != expected_scope
                    or value.get("wi") != "wi_20260825_300"
                ):
                    raise ValueError("native session registration failed")
        result["pass"] = True
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    finally:
        observations = (
            [json.loads(line) for line in wire.read_text().splitlines()]
            if wire.exists()
            else []
        )
        result["advertised_tools"] = sorted(
            {
                tool["name"]
                for row in observations
                for tool in (row.get("result") or {}).get("tools", [])
            }
        )
        result["server_instructions"] = sorted(
            {
                row["result"]["instructions"]
                for row in observations
                if isinstance(row.get("result"), dict)
                and "instructions" in row["result"]
            }
        )
        result["pass"] &= result["advertised_tools"] == ["declare_session_wi"]
        result["protected_inputs_unchanged"] = all(
            hashlib.sha256(Path(p).read_bytes()).hexdigest() == v
            for p, v in before.items()
        )
        result["pass"] &= result["protected_inputs_unchanged"]
        result["session_ref_sha256"] = (
            hashlib.sha256(thread.encode()).hexdigest() if thread else None
        )
        (report / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"rounds", "prompts"}},
            ensure_ascii=False,
        )
    )
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--codex", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument(
        "--runtime-instruction",
        action="store_true",
        help="Deliver the unchanged MCP rule through native developer_instructions",
    )
    p.add_argument("--workspace", type=Path, required=True)
    a = p.parse_args()
    raise SystemExit(
        run(
            a.codex,
            a.source,
            a.report,
            runtime_instruction=a.runtime_instruction,
            workspace=a.workspace,
        )
    )
