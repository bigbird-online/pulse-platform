"""Grok weekly quota: fixtures only, no real Cursor/Grok Bot file or network call."""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from pulse_stats import grok_quota  # noqa: E402
from pulse_stats.live import LiveSnapshotService  # noqa: E402
from test_live import FakeRunner, LiveFixture, ack_result, systemd_result  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("stats_cost_refresh_grok", ROOT / "Scripts/pulse-stats-cost-refresh.py")
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)
PERIOD_START = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
NEXT_RESET = PERIOD_START + timedelta(days=7)


def jwt(sub: str | None) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    payload = {"exp": 1} if sub is None else {"sub": sub, "exp": 1}
    return part({"alg": "none"}) + "." + part(payload) + ".signature"


TOKEN = jwt("auth0|fixture-user")
OTHER_TOKEN = jwt("auth0|other-user")


def response(**changes) -> dict:
    body = {
        "usagePercent": 87.38812,
        "currentPeriodStart": PERIOD_START.isoformat().replace("+00:00", "Z"),
        "nextResetTimestampUtc": NEXT_RESET.isoformat().replace("+00:00", "Z"),
        "hasNonZeroIncludedLimit": True,
        "hasAvailableUsage": True,
        "grokPlanLabel": "Grok Bot Plan",
        "isUnified": True,
        "billingBrand": "SAND_BILLING_BRAND_GROK",
    }
    for key, value in changes.items():
        if value is ...:
            body.pop(key, None)
        else:
            body[key] = value
    return body


class Account:
    """A fixture Cursor state database and Grok Bot account file."""

    def __init__(self, root: pathlib.Path, token: str = TOKEN, active_token: str | None = None):
        self.root = root
        self.state_db = root / "state.vscdb"
        self.accounts = root / "sand-secrets.json"
        connection = sqlite3.connect(self.state_db)
        connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        connection.commit()
        connection.close()
        self.set_token(token)
        self.set_active(active_token or token)

    def set_token(self, token: str) -> None:
        connection = sqlite3.connect(self.state_db)
        connection.execute("INSERT INTO ItemTable VALUES (?, ?)", (grok_quota.TOKEN_KEY, token))
        connection.commit()
        connection.close()

    def set_active(self, token: str) -> None:
        inner = {"active": grok_quota.fingerprint(token), "accounts": {"x": {"label": "fixture"}}}
        self.accounts.write_text(json.dumps({"cursor-accounts": json.dumps(inner), "other": 1}))

    def transport(self, status=200, body=None, content_type="application/json", during=None, calls=None):
        def send(url, headers, data, timeout):
            if calls is not None:
                calls.append((url, dict(headers), data, timeout))
            if during is not None:
                during()
            payload = json.dumps(response() if body is None else body).encode() if not isinstance(body, bytes) else body
            return status, content_type, payload
        return send

    def read(self, **kwargs):
        return grok_quota.read_account_quota(state_db=self.state_db, accounts=self.accounts,
                                             now=lambda: NOW, **kwargs)


class GrokReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = Account(pathlib.Path(self.tmp.name))

    def assertCode(self, code, **kwargs):
        with self.assertRaises(grok_quota.QuotaError) as caught:
            self.account.read(**kwargs)
        self.assertEqual(str(caught.exception), code)
        return caught.exception

    def test_success_publishes_only_the_desensitized_fact(self):
        calls = []
        document = self.account.read(transport=self.account.transport(calls=calls))
        self.assertEqual(document["status"], "available")
        self.assertEqual(document["weekly"], {"used_percent": 87.38812,
            "period_start": int(PERIOD_START.timestamp()), "resets_at": int(NEXT_RESET.timestamp())})
        self.assertEqual(document["plan_label"], "Grok Bot Plan")
        self.assertEqual(document["observed_at"], NOW.isoformat())
        fingerprint = hashlib.sha256(b"auth0|fixture-user").hexdigest()
        self.assertNotEqual(document["account_ref"], fingerprint)
        self.assertEqual(document["account_ref"], grok_quota.account_ref(fingerprint))
        text = json.dumps(document)
        for secret in (TOKEN, "fixture-user", fingerprint):
            self.assertNotIn(secret, text)
        self.assertEqual(grok_quota.validate(document, NOW), document)
        ((url, headers, body, _),) = calls
        self.assertEqual(url, grok_quota.ENDPOINT)
        self.assertEqual(body, b"{}")
        self.assertEqual(headers, {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
                                   "Accept": "application/json", "Connect-Protocol-Version": "1"})

    def test_state_database_is_opened_read_only(self):
        self.account.state_db.chmod(0o400)
        self.assertEqual(self.account.read(transport=self.account.transport())["status"], "available")
        with patch.object(grok_quota.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            grok_quota.read_access_token(self.account.state_db)
        uri = connect.call_args.args[0]
        self.assertTrue(uri.startswith("file:") and uri.endswith("?mode=ro"))
        self.assertTrue(connect.call_args.kwargs["uri"])

    def test_fingerprint_uses_jwt_sub_or_the_token_itself(self):
        self.assertEqual(grok_quota.fingerprint(TOKEN), hashlib.sha256(b"auth0|fixture-user").hexdigest())
        no_sub = jwt(None)
        self.assertEqual(grok_quota.fingerprint(no_sub), hashlib.sha256(no_sub.encode()).hexdigest())
        self.assertEqual(grok_quota.fingerprint("opaque"), hashlib.sha256(b"opaque").hexdigest())

    def test_account_mismatch_refuses_before_any_request(self):
        self.account.set_active(OTHER_TOKEN)
        calls = []
        self.assertCode("account_mismatch", transport=self.account.transport(calls=calls))
        self.assertEqual(calls, [])

    def test_account_switch_during_request_refuses_publication(self):
        for name, switch in (("token", lambda: self.account.set_token(OTHER_TOKEN)),
                             ("active", lambda: self.account.set_active(OTHER_TOKEN))):
            with self.subTest(name):
                self.account.set_token(TOKEN)
                self.account.set_active(TOKEN)
                self.assertCode("account_changed_during_read", transport=self.account.transport(during=switch))

    def test_rejected_login_is_invalid_and_never_refreshed(self):
        before = self.account.state_db.read_bytes(), self.account.accounts.read_bytes()
        for status in (401, 403):
            with self.subTest(status):
                self.assertCode("grok_login_invalid", transport=self.account.transport(status=status, body=b""))
        self.assertEqual(before, (self.account.state_db.read_bytes(), self.account.accounts.read_bytes()))

    def test_http_failures_have_stable_codes(self):
        for status, code in ((429, "quota_rate_limited"), (500, "quota_server_error"),
                             (503, "quota_server_error"), (404, "quota_api_changed"), (302, "quota_api_changed")):
            with self.subTest(status):
                self.assertCode(code, transport=self.account.transport(status=status, body=b""))

        def broken(*args):
            raise OSError("connection reset with private detail")
        self.assertCode("quota_request_failed", transport=broken)
        self.assertCode("quota_api_changed", transport=self.account.transport(body=b"<html>", content_type="text/html"))
        self.assertCode("quota_api_changed", transport=self.account.transport(body=b"{not json"))
        self.assertCode("quota_response_too_large",
                        transport=self.account.transport(body=b" " * (grok_quota.MAX_BYTES + 1)))

    def test_unknown_or_changed_schema_is_unavailable_not_simulated(self):
        # Upsell and billing-link fields the live service adds do not change the reading.
        extra = grok_quota.read_account_quota(state_db=self.account.state_db, accounts=self.account.accounts,
            transport=self.account.transport(body=response(upgradeRecommendations=[], cursorPlanName="Pro",
                                                           unifiedBillingUrl="https://example.invalid")), now=lambda: NOW)
        self.assertEqual(extra["status"], "available")
        for name, body in (
            ("missing percent", response(usagePercent=...)),
            ("percent as text", response(usagePercent="87")),
            ("percent above 100", response(usagePercent=100.5)),
            ("percent NaN", response(usagePercent=float("nan"))),
            ("missing reset", response(nextResetTimestampUtc=...)),
            ("missing start", response(currentPeriodStart=...)),
            ("naive time", response(nextResetTimestampUtc="2026-10-27T00:00:00")),
            ("monthly period", response(nextResetTimestampUtc="2026-11-20T00:00:00Z")),
            ("reset already passed", response(currentPeriodStart="2026-10-10T00:00:00Z",
                                              nextResetTimestampUtc="2026-10-17T00:00:00Z")),
            ("boolean as text", response(isUnified="true")),
            ("not an object", [response()]),
            ("empty plan label", response(grokPlanLabel=" ")),
        ):
            with self.subTest(name):
                self.assertCode("quota_schema_changed", transport=self.account.transport(body=body))
        for brand in (..., "SAND_BILLING_BRAND_CURSOR"):
            with self.subTest(brand=brand):
                self.assertCode("unexpected_billing_brand",
                                transport=self.account.transport(body=response(billingBrand=brand)))

    def test_epoch_period_formats_are_accepted(self):
        start = int(PERIOD_START.timestamp())
        body = response(currentPeriodStart=str(start * 1000), nextResetTimestampUtc=start + 7 * 86400)
        document = self.account.read(transport=self.account.transport(body=body))
        self.assertEqual(document["weekly"]["period_start"], start)

    def test_pooled_or_missing_personal_allowance_is_not_applicable(self):
        for name, body, reason in (
            ("pooled", response(usesPooledEnterpriseAllowance=True), "pooled_enterprise_allowance"),
            ("pooled without percent", response(usesPooledEnterpriseAllowance=True, usagePercent=...),
             "pooled_enterprise_allowance"),
            ("zero included limit", response(hasNonZeroIncludedLimit=False), "no_personal_allowance"),
            ("omitted included limit", response(hasNonZeroIncludedLimit=...), "no_personal_allowance"),
        ):
            with self.subTest(name):
                document = self.account.read(transport=self.account.transport(body=body))
                self.assertEqual((document["status"], document["reason"]), ("not_applicable", reason))
                self.assertNotIn("weekly", document)
                self.assertEqual(grok_quota.validate(document, NOW), document)

    def test_missing_database_token_or_account_file(self):
        calls = []
        send = self.account.transport(calls=calls)
        self.account.accounts.unlink()
        self.assertCode("grok_account_unavailable", transport=send)
        self.account.accounts.write_text(json.dumps({"cursor-accounts": {"active": "not a string"}}))
        self.assertCode("grok_account_unavailable", transport=send)
        self.account.state_db.unlink()
        self.assertCode("cursor_auth_db_unavailable", transport=send)
        self.account.state_db.write_bytes(b"not sqlite")
        self.assertCode("cursor_auth_db_unavailable", transport=send)
        self.account.state_db.unlink()
        Account(self.account.root).set_token(" ")
        self.assertCode("cursor_token_missing", transport=send)
        self.assertEqual(calls, [])


def available(observed=NOW, **weekly) -> dict:
    return {"schema": grok_quota.SCHEMA, "source": grok_quota.SOURCE, "status": "available",
            "observed_at": observed.isoformat(), "account_ref": "a" * 64, "plan_label": "Grok Bot Plan",
            "weekly": {"used_percent": 87.4, "period_start": int(PERIOD_START.timestamp()),
                       "resets_at": int(NEXT_RESET.timestamp()), **weekly}}


class GrokDocumentTests(unittest.TestCase):
    def test_stale_reading_expires_with_its_period(self):
        after_reset = NEXT_RESET + timedelta(seconds=1)
        with self.assertRaisesRegex(grok_quota.QuotaError, "quota_window_elapsed"):
            grok_quota.validate(available(), after_reset)
        with self.assertRaisesRegex(grok_quota.QuotaError, "quota_outside_current_window"):
            grok_quota.validate(available(observed=PERIOD_START - timedelta(hours=2)), NOW)
        with self.assertRaisesRegex(grok_quota.QuotaError, "quota_from_future"):
            grok_quota.validate(available(observed=NOW + timedelta(minutes=5)), NOW)

    def test_published_documents_admit_no_extra_field(self):
        for name, document in (
            ("token", {**available(), "token": TOKEN}),
            ("email", {**grok_quota.failure_document("grok_login_invalid", NOW), "email": "x@y"}),
            ("unknown reason", {**grok_quota.failure_document("grok_login_invalid", NOW), "reason": "free text"}),
            ("raw account", {**available(), "account_ref": "auth0|fixture-user"}),
            ("weekly extra", available(raw=1)),
        ):
            with self.subTest(name), self.assertRaises(grok_quota.QuotaError):
                grok_quota.validate(document, NOW)
        with self.assertRaises(grok_quota.QuotaError):
            grok_quota.failure_document("quota_server_error", NOW)

    def test_load_quota_returns_reason_for_each_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "grok-quota-v1.json"
            self.assertEqual(grok_quota.load_quota(path, NOW), (None, "quota_source_unavailable"))
            for document, reason in (
                (available(), None),
                (grok_quota.failure_document("account_mismatch", NOW), "account_mismatch"),
                ({**available(), "status": "not_applicable", "reason": "pooled_enterprise_allowance"}, None),
            ):
                if document["status"] == "not_applicable":
                    document.pop("weekly")
                    reason = "pooled_enterprise_allowance"
                path.write_text(json.dumps(document))
                path.chmod(0o600)
                self.assertEqual(grok_quota.load_quota(path, NOW), (document, reason))
            path.chmod(0o644)
            self.assertEqual(grok_quota.load_quota(path, NOW), (None, "quota_source_unavailable"))
            path.chmod(0o600)
            path.write_text(json.dumps(available()))
            self.assertEqual(grok_quota.load_quota(path, NEXT_RESET), (None, "quota_window_elapsed"))


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


class GrokPublicationTests(unittest.TestCase):
    """The existing cost-refresh cycle reads and publishes the fact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.state = root / "state"
        self.state.mkdir(mode=0o700)
        self.account = Account(root)
        self.destination = {"host": "fixture", "python": "/python", "script": "/script"}
        self.config = {"destination": self.destination,
                       "grok_quota": {"state_db": str(self.account.state_db), "accounts": str(self.account.accounts)}}
        patcher = patch.object(refresh, "STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The fixture period is fixed, so the refresh reads and checks it at NOW.
        for module in (refresh, grok_quota):
            clock = patch.object(module, "datetime", FrozenDatetime)
            clock.start()
            self.addCleanup(clock.stop)
        self.target = self.state / "grok-quota-v1.json"

    def run_refresh(self, transport, config=None):
        sent = []
        stderr = io.StringIO()
        with patch.object(refresh, "ssh", side_effect=lambda peer, command, data=None: sent.append((command, data)) or b""), \
             redirect_stderr(stderr):
            status = refresh.refresh_grok_quota(self.config if config is None else config, transport=transport)
        return status, sent, stderr.getvalue()

    def published(self):
        return json.loads(self.target.read_bytes())

    def test_unconfigured_source_reads_nothing(self):
        def never(*args):
            raise AssertionError("no read without configuration")
        self.assertEqual(self.run_refresh(never, {"destination": self.destination})[0], "source_not_configured")
        self.assertFalse(self.target.exists())

    def test_success_is_written_privately_and_forwarded(self):
        status, sent, log = self.run_refresh(self.account.transport())
        self.assertEqual(status, "published")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sent, [(["/python", "/script", "receive-grok-quota"], self.target.read_bytes())])
        self.assertNotIn(TOKEN, self.target.read_text() + log)
        self.assertEqual(self.published()["status"], "available")

    def test_definitive_failure_replaces_the_reading_with_its_reason(self):
        self.run_refresh(self.account.transport())
        for code, transport in (
            ("grok_login_invalid", self.account.transport(status=401, body=b"")),
            ("quota_schema_changed", self.account.transport(body=response(usagePercent="87"))),
        ):
            with self.subTest(code):
                status, sent, log = self.run_refresh(transport)
                self.assertEqual(status, "unavailable")
                self.assertEqual(self.published(), {"schema": grok_quota.SCHEMA, "source": grok_quota.SOURCE,
                    "status": "unavailable", "observed_at": self.published()["observed_at"], "reason": code})
                expected = {"schema": "stats_refresh_progress/v1", "stage": "grok_quota_read_failed",
                            "error_code": code}
                self.assertEqual(json.loads(log.splitlines()[-1]), expected)
                self.assertNotIn(TOKEN, log)
        self.account.set_active(OTHER_TOKEN)
        self.assertEqual(self.run_refresh(self.account.transport())[0], "unavailable")
        self.assertEqual(self.published()["reason"], "account_mismatch")

    def test_transient_failure_keeps_the_previous_reading(self):
        self.run_refresh(self.account.transport())
        before = self.target.read_bytes()

        def offline(*args):
            raise OSError("private network detail")
        for transport in (offline, self.account.transport(status=503, body=b"")):
            status, sent, log = self.run_refresh(transport)
            self.assertEqual((status, sent), ("read_or_publish_failed", []))
            self.assertNotIn("private", log)
            self.assertEqual(self.target.read_bytes(), before)

    def test_pooled_allowance_is_published_as_not_applicable(self):
        status, _, _ = self.run_refresh(self.account.transport(body=response(usesPooledEnterpriseAllowance=True)))
        self.assertEqual(status, "not_applicable")
        self.assertEqual(self.published()["reason"], "pooled_enterprise_allowance")

    def test_receiver_refuses_an_older_publication(self):
        newer = available(observed=NOW)
        older = available(observed=NOW - timedelta(minutes=5))
        for document in (newer, older):
            document["weekly"]["resets_at"] = int(NOW.timestamp()) + 86400
            document["weekly"]["period_start"] = document["weekly"]["resets_at"] - 7 * 86400
        refresh.receive_grok_quota(json.dumps(newer).encode())
        with self.assertRaisesRegex(ValueError, "older quota publication refused"):
            refresh.receive_grok_quota(json.dumps(older).encode())
        self.assertEqual(self.published(), newer)

    def test_manual_mode_runs_one_read(self):
        config = pathlib.Path(self.tmp.name) / "manual.json"
        config.write_text(json.dumps({"grok_quota": self.config["grok_quota"]}))
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["refresh", "grok-quota", "--config", str(config)]), \
             patch.object(grok_quota, "urllib_transport", self.account.transport()), \
             patch("sys.stdout", stdout):
            self.assertEqual(refresh.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"grok_quota": "published"})
        self.assertEqual(self.published()["status"], "available")


class GrokQuotaMetricTests(unittest.TestCase):
    """The snapshot reads the published fact; never the account files or network."""

    @staticmethod
    def render(directory, document=None):
        fixture = LiveFixture(pathlib.Path(directory))
        path = fixture.root / "grok-quota-v1.json"
        if document is not None:
            path.write_text(json.dumps(document))
            path.chmod(0o600)
        fixture.paths = replace(fixture.paths, grok_quota=path)
        runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
        rendered = fixture.service(runner, now=NOW).render_json()
        return {item["id"]: item for item in json.loads(rendered)["metrics"]}

    def test_available_reading_is_published_as_percent_with_reset_and_label(self):
        with tempfile.TemporaryDirectory() as directory:
            observed = NOW - timedelta(minutes=10)
            metrics = self.render(directory, available(observed=observed))
            percent = metrics["relay.grok.used_percent"]
            self.assertEqual((percent["status"], percent["value"], percent["unit"]), ("available", 87.4, "percent"))
            self.assertEqual(percent["observed_at"], observed.isoformat())
            self.assertEqual(percent["scope"], "Grok account weekly pool; not per-bot cost")
            self.assertEqual(metrics["relay.grok.resets_at"]["value"], int(NEXT_RESET.timestamp()))
            self.assertEqual(metrics["relay.grok.period_start"]["value"], int(PERIOD_START.timestamp()))
            self.assertEqual(metrics["relay.grok.plan_label"]["value"], "Grok Bot Plan")

    def test_unavailable_states_are_unknown_with_reason_never_zero(self):
        pooled = {k: v for k, v in available().items() if k != "weekly"}
        pooled.update(status="not_applicable", reason="pooled_enterprise_allowance")
        for name, document, reason, has_time in (
            ("missing", None, "quota_source_unavailable", False),
            ("stale", available(observed=PERIOD_START - timedelta(days=1)), "quota_outside_current_window", False),
            ("elapsed", available(resets_at=int(NOW.timestamp()) - 1, period_start=int(NOW.timestamp()) - 86400),
             "quota_window_elapsed", False),
            ("login", grok_quota.failure_document("grok_login_invalid", NOW), "grok_login_invalid", True),
            ("mismatch", grok_quota.failure_document("account_mismatch", NOW), "account_mismatch", True),
            ("pooled", pooled, "pooled_enterprise_allowance", True),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                metrics = self.render(directory, document)
                for metric_id in ("relay.grok.used_percent", "relay.grok.resets_at", "relay.grok.period_start"):
                    self.assertEqual(metrics[metric_id]["status"], "unknown", metric_id)
                    self.assertIsNone(metrics[metric_id]["value"], metric_id)
                    self.assertEqual(metrics[metric_id]["reason"], reason, metric_id)
                    self.assertEqual(metrics[metric_id]["observed_at"] is not None, has_time, metric_id)

    def test_without_a_configured_file_no_grok_metric_is_emitted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiveFixture(pathlib.Path(directory))
            runner = FakeRunner(ack=ack_result(), systemd=systemd_result())
            metrics = json.loads(fixture.service(runner, now=NOW).render_json())["metrics"]
        self.assertFalse([m["id"] for m in metrics if "grok" in m["id"]])

    def test_production_wiring_reads_the_published_grok_file(self):
        self.assertEqual(LiveSnapshotService.default().paths.grok_quota,
                         pathlib.Path.home() / ".local/state/pulse/stats/grok-quota-v1.json")


if __name__ == "__main__":
    unittest.main()
