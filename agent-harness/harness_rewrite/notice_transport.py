"""Installed notice routing and durable single-attempt Relay receipt transport."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

from .model import Json, NotificationRoute, digest, encoded
from .notifications import validate_routes

CONTRACT = "relay-notice-single-attempt/v1"
MAX_RESPONSE = 32768
ROUTE_FIELDS = {
    "route_id",
    "channel",
    "agent_id",
    "recipient",
    "mode",
    "timezone",
    "audience",
    "max_items",
    "max_bytes",
}


def configured_routes(policy: Json) -> tuple[NotificationRoute, ...]:
    transport = policy.get("notification_transport")
    rows = policy.get("notification_routes")
    if "notification_transport" not in policy and "notification_routes" not in policy:
        return ()
    if (
        not isinstance(transport, dict)
        or transport.keys() != {"endpoint", "receipts_root", "token_env"}
        or transport["token_env"] != "RELAY_INTERNAL_TOKEN"
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= 128
    ):
        raise ValueError("Notification transport and routes must be explicit")
    endpoint = transport["endpoint"]
    if not isinstance(endpoint, str):
        raise ValueError("Expected installed loopback notice endpoint")
    parts = urlsplit(endpoint)
    if (
        endpoint != f"http://127.0.0.1:{parts.port}/internal/notice"
        or parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.username is not None
        or parts.password is not None
        or parts.port is None
        or not 1 <= parts.port <= 65535
        or parts.netloc != f"127.0.0.1:{parts.port}"
        or parts.path != "/internal/notice"
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Expected installed loopback notice endpoint")
    root = transport["receipts_root"]
    if (
        not isinstance(root, str)
        or not root
        or len(root) > 4096
        or "\0" in root
        or not Path(root).is_absolute()
    ):
        raise ValueError("Expected an absolute receipt root")
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.keys() != ROUTE_FIELDS:
            raise ValueError("Invalid installed notice route shape")
        if any(
            not isinstance(row[k], str)
            or not row[k]
            or len(row[k]) > 256
            or "\0" in row[k]
            for k in ROUTE_FIELDS - {"max_items", "max_bytes"}
        ):
            raise ValueError("Invalid installed notice route value")
        if not isinstance(row["agent_id"], str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", row["agent_id"]
        ):
            raise ValueError("Invalid installed notice agent")
        recipient = row["recipient"]
        if not isinstance(recipient, str) or len(recipient) > 128:
            raise ValueError("Invalid installed notice recipient")
        if row["channel"] == "telegram":
            if (
                not re.fullmatch(r"-?[1-9][0-9]*", recipient)
                or abs(int(recipient)) > 2**53 - 1
            ):
                raise ValueError("Invalid Telegram recipient")
        elif row["channel"] != "feishu" or not re.fullmatch(
            r"ou_[A-Za-z0-9_-]+", recipient
        ):
            raise ValueError("Invalid Feishu recipient")
        result.append(
            NotificationRoute(
                **{
                    k: row[k]
                    for k in (
                        "route_id",
                        "channel",
                        "mode",
                        "timezone",
                        "audience",
                        "max_items",
                        "max_bytes",
                    )
                },
                recipient_digest=digest([row["channel"], row["agent_id"], recipient]),
                transport_digest=digest([transport, row]),
            )
        )
    validate_routes(tuple(result))
    return tuple(result)


def _read(path: Path) -> Json:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESPONSE:
            raise ValueError("Invalid receipt record")
        data = stream.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError("Receipt record exceeds bound")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Invalid receipt record")
    return dict(result)


def _sync(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class NoticeDelivery:
    """One local durable send claim; unknown is never absence or an HTTP retry."""

    def __init__(self, policy: Json, wi: str):
        self.policy, self.wi = policy, wi
        self.routes = configured_routes(policy)

    def request(self, effect: Json, index: int) -> tuple[Path, str, Json]:
        snapshot = effect["snapshot"]
        routes = [r for r in self.routes if r.route_id == effect["target"]]
        if len(routes) != 1 or asdict(routes[0]) != snapshot["route"]:
            raise ValueError("Original notice route binding changed")
        route = routes[0]
        if (
            snapshot["route_digest"] != digest(asdict(route))
            or snapshot["text_sha256"]
            != hashlib.sha256(snapshot["text"].encode()).hexdigest()
            or not snapshot["text"].strip()
            or len(snapshot["text"].encode()) > route.max_bytes
        ):
            raise ValueError("Original notice content binding changed")
        transport = self.policy["notification_transport"]
        root = Path(transport["receipts_root"])
        if root.resolve() != root or not root.is_dir():
            raise ValueError("Receipt root unavailable or redirected")
        row = next(
            r
            for r in self.policy["notification_routes"]
            if r["route_id"] == route.route_id
        )
        key = digest(
            [
                CONTRACT,
                self.policy["namespace"],
                self.wi,
                effect["effect_id"],
                index,
                snapshot,
            ]
        )
        payload = {
            "channel": route.channel,
            "agentId": row["agent_id"],
            "to": row["recipient"],
            "text": snapshot["text"],
            "source": "harness-operational",
            "notice": {
                "contract": CONTRACT,
                "requestKey": key,
                "routeDigest": snapshot["route_digest"],
                "textSha256": snapshot["text_sha256"],
            },
        }
        if route.audience == "owner_attention":
            attention = snapshot.get("human_attention")
            if (
                not isinstance(attention, dict)
                or attention.keys()
                != {"requiresHuman", "humanDecisionKind", "authorityFingerprint"}
                or attention["requiresHuman"] is not True
                or attention["humanDecisionKind"] != "owner_decision"
                or not isinstance(attention["authorityFingerprint"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", attention["authorityFingerprint"])
            ):
                raise ValueError("Original human attention receipt unavailable")
            payload.update(source="harness-escalation", **attention)
        return root, key, payload

    def classify(self, record: Json, payload: Json, effect: Json) -> tuple[str, Json]:
        body = record.get("body")
        if (
            not isinstance(body, dict)
            or body.get("notice") != payload["notice"]
            or type(body.get("sendAttempted")) is not bool
        ):
            return "unknown", {"reason": "notice_response_binding_unavailable"}
        if body["sendAttempted"] is False:
            if (
                record.get("status") in {400, 404, 409}
                and body.get("ok") is False
                and body.get("error")
                in {
                    "agent_not_found",
                    "human_attention_receipt_required",
                }
            ):
                return "absence", {
                    "reason": "relay_rejected_before_send",
                    "request_key": payload["notice"]["requestKey"],
                }
            return "unknown", {"reason": "notice_send_not_proven"}
        receipts = body.get("receipts")
        if (
            record.get("status") != 200
            or body.get("ok") is not True
            or not isinstance(receipts, list)
            or len(receipts) != 1
            or not isinstance(receipts[0], dict)
        ):
            return "unknown", {"reason": "channel_result_unknown"}
        receipt = receipts[0]
        message = receipt.get("externalMessageId")
        if (
            receipt.get("channel") != payload["channel"]
            or receipt.get("to") != payload["to"]
            or receipt.get("status") != "sent"
            or type(receipt.get("attempts")) is not int
            or receipt["attempts"] != 1
            or not isinstance(message, str)
            or not message.strip()
            or len(message) > 512
            or any(ord(c) < 32 for c in message)
        ):
            return "unknown", {"reason": "channel_receipt_mismatch"}
        snapshot = effect["snapshot"]
        return "success", {
            "channel": payload["channel"],
            "recipient_digest": snapshot["route"]["recipient_digest"],
            "route_digest": snapshot["route_digest"],
            "text_sha256": snapshot["text_sha256"],
            "external_message_id": message,
            "request_key": payload["notice"]["requestKey"],
            "transport_contract": CONTRACT,
        }

    def _observe(self, effect: Json, index: int) -> tuple[str, Json]:
        try:
            root, key, payload = self.request(effect, index)
            if _read(root / (key + ".request.json")) != payload:
                raise ValueError("Original send claim differs")
            record = _read(root / (key + ".response.json"))
            if record.get("request_sha256") != digest(payload):
                raise ValueError("Original response differs")
            return self.classify(record, payload, effect)
        except (OSError, ValueError, KeyError, TypeError):
            return "unknown", {"reason": "original_notice_receipt_unavailable"}

    def readback(self, effect: Json) -> tuple[str, Json]:
        # A missing local marker cannot prove that no external send occurred.
        return self._observe(effect, len(effect["attempts"]) - 1)

    def apply(self, effect: Json, content: bytes) -> tuple[str, Json]:
        if content != encoded(effect["snapshot"]):
            return "failure", {"reason": "notice_fixed_input_mismatch"}
        index = len(effect["attempts"])
        try:
            root, key, payload = self.request(effect, index)
            marker = root / (key + ".request.json")
            fd = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            return self._observe(effect, index)
        except (OSError, ValueError, KeyError, TypeError):
            return "unknown", {"reason": "notice_transport_unavailable"}
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(payload))
            stream.flush()
            os.fsync(stream.fileno())
        _sync(root)
        # No redirects, proxy rewrite or retry: endpoint is the admitted loopback.
        parts = urlsplit(self.policy["notification_transport"]["endpoint"])
        connection = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=15)
        try:
            headers = {"Content-Type": "application/json"}
            token = os.environ.get("RELAY_INTERNAL_TOKEN", "").strip()
            if token:
                headers["x-relay-token"] = token
            connection.request("POST", "/internal/notice", encoded(payload), headers)
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                return "unknown", {"reason": "notice_response_exceeds_bound"}
            body = json.loads(data)
            # Persist only contract fields; never retain arbitrary error prose.
            safe = {
                k: body[k]
                for k in ("notice", "sendAttempted", "ok", "receipts", "error")
                if k in body
            }
            if safe.get("error") not in {
                "agent_not_found",
                "human_attention_receipt_required",
            }:
                safe.pop("error", None)
            if isinstance(safe.get("receipts"), list):
                safe["receipts"] = [
                    {
                        k: r[k]
                        for k in (
                            "channel",
                            "to",
                            "status",
                            "attempts",
                            "externalMessageId",
                        )
                        if k in r
                    }
                    for r in safe["receipts"]
                    if isinstance(r, dict)
                ]
            record = {
                "request_sha256": digest(payload),
                "status": response.status,
                "body": safe,
            }
            outcome = self.classify(record, payload, effect)
            with tempfile.NamedTemporaryFile(
                dir=root, prefix=".notice-", delete=True
            ) as stream:
                stream.write(encoded(record))
                stream.flush()
                os.fsync(stream.fileno())
                os.link(stream.name, root / (key + ".response.json"))
                _sync(root)
            return outcome
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            return "unknown", {"reason": "notice_result_unavailable"}
        finally:
            connection.close()
