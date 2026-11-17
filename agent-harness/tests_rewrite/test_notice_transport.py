"""Real loopback HTTP, synthetic channel receipts; no production channel calls."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from harness_rewrite import policy, Query
from harness_rewrite.model import Rejected, encoded
from harness_rewrite.native_host import NativeHost
from harness_rewrite.notice_transport import NoticeDelivery, configured_routes
from tests_rewrite.test_native_host import native as native
from tests_rewrite.test_policy import configured as configured
from tests_rewrite.test_notifications import Notices, original


@pytest.fixture
def relay():
    requests = []
    mode = {"value": "success"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body, self.headers.get("x-relay-token")))
            current = mode["value"]
            if current == "lost":
                self.close_connection = True
                return
            response = {
                "notice": body["notice"],
                "sendAttempted": True,
                "ok": True,
                "receipts": [
                    {
                        "channel": body["channel"],
                        "to": body["to"],
                        "attempts": 1,
                        "status": "sent",
                        "externalMessageId": "original-channel-message-1",
                    }
                ],
            }
            status = 200
            if current == "refuse":
                status = 404
                response = {
                    "notice": body["notice"],
                    "sendAttempted": False,
                    "ok": False,
                    "error": "agent_not_found",
                }
            elif current == "old":
                status, response = 404, {"ok": False, "error": "not_found"}
            elif current == "redirect":
                status = 307
            elif current == "wrong_binding":
                response["notice"] = {**body["notice"], "requestKey": "0" * 64}
            elif current == "wrong_to":
                response["receipts"][0]["to"] = "99999"
            elif current == "no_id":
                response["receipts"][0].pop("externalMessageId")
            elif current == "retried":
                response["receipts"][0]["attempts"] = 2
            elif current == "channel_failure":
                response["ok"] = False
                response["receipts"][0].update(status="failed", error="private-url")
            data = encoded(response) if current != "oversize" else b"x" * 40000
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Location", "/internal/notice")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/internal/notice", requests, mode
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def settings(root, endpoint):
    receipts = root / "notice-receipts"
    receipts.mkdir(exist_ok=True)
    return {
        "namespace": "fixture",
        "notification_transport": {
            "endpoint": endpoint,
            "receipts_root": str(receipts),
            "token_env": "RELAY_INTERNAL_TOKEN",
        },
        "notification_routes": [
            {
                "route_id": "installed-route",
                "channel": "telegram",
                "agent_id": "pulse",
                "recipient": "12345",
                "mode": "immediate",
                "timezone": "Asia/Taipei",
                "audience": "operational",
                "max_items": 16,
                "max_bytes": 4096,
            }
        ],
    }


def setup_notice(tmp_path, endpoint):
    p = settings(tmp_path, endpoint)
    n = Notices(tmp_path, configured_routes(p)[0])
    original(n.record())
    return p, n


def worker_effect(n):
    return n.kernel.inspect(Query(n.wi), n.worker(()))["effects"][0]


def test_real_http_delivery_and_recreated_adapter_readback(
    tmp_path, relay, monkeypatch
):
    endpoint, requests, _ = relay
    monkeypatch.setenv("RELAY_INTERNAL_TOKEN", "fixture-managed-token")
    p, n = setup_notice(tmp_path, endpoint)
    n.drain(NoticeDelivery(p, n.wi))
    current = worker_effect(n)
    assert current["status"] == "observed_success"
    assert len(requests) == 1
    assert requests[0][0] == "/internal/notice"
    assert requests[0][2] == "fixture-managed-token"
    state, receipt = NoticeDelivery(p, n.wi).readback(current)
    assert state == "success"
    assert receipt["external_message_id"] == "original-channel-message-1"
    assert "fixture-managed-token" not in json.dumps(receipt)
    assert n.drain(NoticeDelivery(p, n.wi)) == []
    assert len(requests) == 1


def test_concurrent_duplicate_apply_has_one_exclusive_send(tmp_path, relay):
    endpoint, requests, _ = relay
    p, n = setup_notice(tmp_path, endpoint)
    effect = worker_effect(n)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _: NoticeDelivery(p, n.wi).apply(
                    effect, encoded(effect["snapshot"])
                ),
                range(2),
            )
        )
    assert len(requests) == 1
    assert any(state == "success" for state, _ in outcomes)
    assert all(state in {"unknown", "success"} for state, _ in outcomes)


@pytest.mark.parametrize("stage", ["after_send", "before_observation"])
def test_crash_after_send_recovers_original_receipt_without_send(
    tmp_path, relay, stage
):
    endpoint, requests, _ = relay
    p, n = setup_notice(tmp_path, endpoint)

    def crash(at):
        if at == stage:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        n.drain(NoticeDelivery(p, n.wi), fault=crash)
    assert len(requests) == 1
    n.drain(NoticeDelivery(p, n.wi))
    assert n.state()["effects"][0]["status"] == "observed_success"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "mode",
    [
        "lost",
        "old",
        "redirect",
        "wrong_binding",
        "wrong_to",
        "no_id",
        "retried",
        "channel_failure",
        "oversize",
    ],
)
def test_ambiguous_or_incompatible_transport_never_resends(tmp_path, relay, mode):
    endpoint, requests, behavior = relay
    behavior["value"] = mode
    p, n = setup_notice(tmp_path, endpoint)
    n.drain(NoticeDelivery(p, n.wi))
    n.drain(NoticeDelivery(p, n.wi))
    effect = worker_effect(n)
    assert effect["status"] == "unknown"
    assert len(effect["attempts"]) == 1
    assert len(requests) == 1
    original(n.resolve(effect["snapshot"]["notice_ids"][0]))
    n.drain(NoticeDelivery(p, n.wi))
    assert len(requests) == 1
    assert n.state()["effects"][0]["status"] == "unknown"


def test_only_original_presend_rejection_allows_a_new_attempt(tmp_path, relay):
    endpoint, requests, behavior = relay
    behavior["value"] = "refuse"
    p, n = setup_notice(tmp_path, endpoint)
    n.drain(NoticeDelivery(p, n.wi))
    assert n.state()["effects"][0]["status"] == "observed_absence"
    behavior["value"] = "success"
    n.drain(NoticeDelivery(p, n.wi))
    assert n.state()["effects"][0]["status"] == "observed_success"
    assert len(requests) == 2
    assert (
        requests[0][1]["notice"]["requestKey"] != requests[1][1]["notice"]["requestKey"]
    )
    assert (
        len(
            list(
                Path(p["notification_transport"]["receipts_root"]).glob(
                    "*.request.json"
                )
            )
        )
        == 2
    )


def test_lost_local_evidence_is_not_proof_of_absence(tmp_path, relay):
    endpoint, requests, _ = relay
    p, n = setup_notice(tmp_path, endpoint)

    def crash(stage):
        if stage == "after_send":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        n.drain(NoticeDelivery(p, n.wi), fault=crash)
    for f in Path(p["notification_transport"]["receipts_root"]).glob("*.json"):
        f.unlink()
    n.drain(NoticeDelivery(p, n.wi))
    assert n.state()["effects"][0]["status"] == "unknown"
    assert len(requests) == 1


def test_installed_route_change_cannot_retarget_captured_notice(tmp_path, relay):
    endpoint, requests, _ = relay
    p, n = setup_notice(tmp_path, endpoint)
    p["notification_routes"][0]["recipient"] = "54321"
    n.drain(NoticeDelivery(p, n.wi))
    assert n.state()["effects"][0]["status"] == "unknown"
    assert requests == []


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p["notification_transport"].update(
            endpoint="http://example.com/internal/notice"
        ),
        lambda p: p["notification_transport"].update(
            endpoint="http://127.0.0.1:80/internal/push"
        ),
        lambda p: p["notification_transport"].update(
            endpoint="http://user:secret@127.0.0.1:80/internal/notice"
        ),
        lambda p: p["notification_transport"].update(
            endpoint="http://127.0.0.1:80/internal/notice?to=other"
        ),
        lambda p: p["notification_transport"].update(token_env="OTHER_SECRET"),
        lambda p: p["notification_routes"][0].update(recipient="NaN"),
        lambda p: p["notification_routes"][0].update(max_items=True),
        lambda p: p["notification_routes"][0].update(channel="both"),
        lambda p: p.update(notification_routes=[]),
        lambda p: p.pop("notification_transport"),
    ],
)
def test_policy_rejects_widening_or_ambiguous_routes(tmp_path, edit):
    p = settings(tmp_path, "http://127.0.0.1:8181/internal/notice")
    edit(p)
    with pytest.raises((ValueError, Rejected)):
        configured_routes(p)


def test_native_producer_and_worker_adopt_route_audit_is_readonly(native, tmp_path):
    path, proc, _, _ = native
    p = json.loads(path.read_text())
    p.update(settings(tmp_path, "http://127.0.0.1:8181/internal/notice"))
    path.write_text(json.dumps(p))
    host = NativeHost(path, proc=proc, pid=101)
    assert len(host.base.notification_routes) == 1
    assert "12345" not in json.dumps(asdict(host.base.notification_routes[0]))
    assert "notification_send" in host.authenticate(host.worker(())).effect_kinds
    p["execution"] = {"mode": "audit", "batch_budget": 1}
    path.write_text(json.dumps(p))
    audited = NativeHost(path, proc=proc, pid=101)
    assert audited.base.notification_routes == ()
    assert not audited.authenticate(audited.worker(())).effect_kinds


def test_preflight_review_and_compatible_route_change(configured):
    root, p, path = configured
    old = deepcopy(p)
    config = settings(root, "http://127.0.0.1:8181/internal/notice")
    p.update(config)
    policy.shape(p)
    policy.preflight(p, root)
    public = policy.describe(p)
    assert public["counts"]["notification_routes"] == 1
    assert "12345" not in json.dumps(public)
    policy.compatible(old, p)
    changed = deepcopy(p)
    changed["notification_transport"]["endpoint"] = (
        "http://127.0.0.1:8182/internal/notice"
    )
    with pytest.raises(ValueError):
        policy.compatible(p, changed)
    p["notification_transport"]["receipts_root"] = str(root.parent / "outside")
    with pytest.raises(ValueError):
        policy.preflight(p, root)


def test_native_cli_records_then_resumes_real_notice_transport(
    native, tmp_path, relay, monkeypatch, capsys
):
    from harness_rewrite import cli

    path, proc, _, _ = native
    p = json.loads(path.read_text())
    p.update(settings(tmp_path, relay[0]))
    for name in ("views", "delivery", "dispatch"):
        (tmp_path / name).mkdir()
    p.update(
        database=str(tmp_path / "facts.sqlite"),
        views_root=str(tmp_path / "views"),
        delivery_root=str(tmp_path / "delivery"),
        dispatch_root=str(tmp_path / "dispatch"),
        execution={"mode": "manual", "batch_budget": 1},
    )
    path.write_text(json.dumps(p))
    monkeypatch.setattr(
        cli, "NativeHost", lambda _: NativeHost(path, proc=proc, pid=101)
    )
    monkeypatch.setattr(cli.sys, "argv", [str(Path(__file__).resolve())])
    command = tmp_path / "command.json"

    def run(args):
        exit_code = cli.main(args)
        result = json.loads(capsys.readouterr().out)
        assert exit_code == 0, result
        return result

    def send(intent, data, wi=None):
        command.write_text(
            json.dumps({"intent": intent, "input": data, "work_item_id": wi})
        )
        return run(["execute", "--input", str(command)])

    wi = send(
        "create_lightweight",
        {
            "summary": "Fixture work for tests.",
            "why": "Original channel receipt fixture",
            "what": "Deliver an operational notice",
            "acceptance": ["Actual receipt, no retry"],
            "resources": ["repo"],
        },
    )["work_item"]["id"]
    send("start_work", {}, wi)
    send(
        "record_notice",
        {
            "route_id": "installed-route",
            "resources": ["repo"],
            "text": "Native CLI synthetic notice",
            "purpose": "operational",
            "attention": None,
        },
        wi,
    )
    assert relay[1] == []
    resumed = run(["resume", "--wi", wi])
    assert resumed["effects"][0]["status"] == "observed_success"
    assert "snapshot" not in resumed["effects"][0]
    assert len(relay[1]) == 1
    run(["resume", "--wi", wi])
    assert len(relay[1]) == 1
    query = run(["inspect", "--wi", wi, "--view", "notices"])
    assert query["result"]["items"]["items"][0]["delivery_status"] == "observed_success"

    # The installed CLI must preserve filters, not just parse their flags.
    for status, count in [
        ("open", 1),
        ("failed", 0),
        ("unresolved_send", 0),
        ("resolved", 0),
        ("backlog", 0),
    ]:
        filtered = run(
            [
                "inspect",
                "--wi",
                wi,
                "--view",
                "notices",
                "--status",
                status,
                "--after",
                "0",
                "--limit",
                "1",
            ]
        )
        assert len(filtered["result"]["items"]["items"]) == count, status
        assert filtered["result"]["items"]["next_offset"] is None
    assert len(relay[1]) == 1


def test_explicit_human_attention_keeps_original_gate_on_wire(tmp_path, relay):
    from dataclasses import replace
    from harness_rewrite import SourceInput
    from harness_rewrite.model import digest

    p = settings(tmp_path, relay[0])
    p["notification_routes"][0]["audience"] = "owner_attention"
    n = Notices(tmp_path, configured_routes(p)[0])
    data = {
        "route_id": "installed-route",
        "resources": ["repo/src"],
        "text": "Synthetic decision",
        "purpose": "explicit_decision",
        "attention": {"reason": "Choose this synthetic case"},
    }
    n.host.contexts["owner"] = replace(
        n.context,
        source=SourceInput(
            "human-source",
            "native_human",
            b"Request this exact decision",
            "record_notice",
            digest({"intent": "record_notice", "work_item_id": n.wi, "input": data}),
        ),
    )
    original(n.send("record_notice", data))
    n.drain(NoticeDelivery(p, n.wi))
    assert len(relay[1]) == 1
    payload = relay[1][0][1]
    assert payload["source"] == "harness-escalation"
    assert payload["requiresHuman"] is True
    assert payload["humanDecisionKind"] == "owner_decision"
    assert len(payload["authorityFingerprint"]) == 64


def test_receipt_corruption_and_symlink_do_not_recover_as_absence(tmp_path, relay):
    p, n = setup_notice(tmp_path, relay[0])
    n.drain(NoticeDelivery(p, n.wi))
    effect = worker_effect(n)
    path = next(
        Path(p["notification_transport"]["receipts_root"]).glob("*.response.json")
    )
    retained = path.read_bytes()
    path.write_bytes(b"{")
    assert NoticeDelivery(p, n.wi).readback(effect)[0] == "unknown"
    original_file = tmp_path / "original-response.json"
    original_file.write_bytes(retained)
    path.unlink()
    path.symlink_to(original_file)
    assert NoticeDelivery(p, n.wi).readback(effect)[0] == "unknown"
    assert len(relay[1]) == 1


def test_unknown_optional_notice_does_not_monopolize_manual_one_budget(tmp_path, relay):
    from harness_rewrite.effects import Effects

    endpoint, requests, behavior = relay
    behavior["value"] = "channel_failure"
    p, n = setup_notice(tmp_path, endpoint)
    driver = Effects(n.kernel, NoticeDelivery(p, n.wi), n.worker)
    assert len(driver.drain(n.wi, budget=1)) == 1
    old = worker_effect(n)
    assert old["status"] == "unknown"
    assert len(requests) == 1

    # This is a separately authorized intention, not a retry of the unknown.
    original(n.record(text="A distinct independent follow-up"))
    behavior["value"] = "success"
    assert len(driver.drain(n.wi, budget=1)) == 1
    effects = n.state()["effects"]
    assert [e["status"] for e in effects] == ["unknown", "observed_success"]
    assert [len(e["attempts"]) for e in effects] == [1, 1]
    assert len(requests) == 2
    assert requests[0][1]["notice"]["requestKey"] != requests[1][1]["notice"]["requestKey"]
    assert requests[1][1]["text"] == "A distinct independent follow-up"

    # Idle drain still reads the retained unknown; neither message is resent.
    driver.drain(n.wi, budget=1)
    assert len(requests) == 2
    assert [e["status"] for e in n.state()["effects"]] == ["unknown", "observed_success"]
