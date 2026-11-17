"""An installed stateless tool is not an activated accounting source."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load("mcp_install", ROOT / "Stats/request_purpose.py")
deploy = load("mcp_deploy", ROOT / "Stats/deploy.py")


class McpInstallationTests(unittest.TestCase):
    def setUp(self):
        self.rule = "使用 declare_session_wi：当你明确本 session 所属 WI 时调用一次。"
        probe = patch.object(install, "mcp_instructions", return_value=self.rule)
        self.instruction_probe = probe.start()
        self.addCleanup(probe.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.revision = "a" * 40
        self.runtime = self.home / ".local/share/pulse/stats/releases" / self.revision
        self.python = self.home / ".local/share/coevolution/python-env/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("fixture")
        paths = [
            "ComputeCost/runtime/codex-mcp-request-purpose.py",
            "ComputeCost/pulse_compute_cost/mcp_request_purpose.py",
        ]
        inventory = {}
        for name in paths:
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
            inventory[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.runtime / ".stats-files.json").write_text(json.dumps(inventory))
        (self.runtime / ".stats-source-revision").write_text(self.revision)
        self.selector = self.home / ".local/share/pulse/stats/current"
        self.selector.symlink_to(self.runtime)
        self.config = self.home / ".codex/config.toml"
        self.config.parent.mkdir()
        self.original = b'# retain comments\nmodel = "existing"\n[mcp_servers.other]\ncommand = "other-command"\n'
        self.config.write_bytes(self.original)
        self.hooks = self.home / ".codex/hooks.json"
        self.hooks.write_text('{"hooks":{}}')

    def apply(self, expected=None):
        return install.install_mcp(
            self.home,
            self.runtime,
            self.python,
            self.revision,
            expected_config_sha256=expected
            or hashlib.sha256(self.config.read_bytes()).hexdigest(),
        )

    def test_append_preserves_original_bytes_hooks_and_other_settings(self):
        result = self.apply()
        after = self.config.read_bytes()
        self.assertIn(self.original, after)
        self.assertTrue(result["changed"])
        self.assertEqual(self.hooks.read_text(), '{"hooks":{}}')
        self.assertFalse(result["source_registry_changed"])
        self.assertFalse(result["native_trust_changed"])
        self.assertEqual(result["status"], "configured_pending_native_invocation")
        values = tomllib.loads(after.decode())
        values["mcp_servers"].pop("pulse-request-purpose")
        self.assertEqual(values.pop("developer_instructions"), self.rule)
        self.assertTrue(result["runtime_instruction_configured"])
        self.assertEqual(values, tomllib.loads(self.original.decode()))
        self.assertFalse(self.apply()["changed"])
        self.assertEqual(self.config.read_bytes(), after)

    def test_cas_failure_does_not_change_configuration(self):
        with self.assertRaisesRegex(ValueError, "changed after review"):
            self.apply("b" * 64)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_independently_owned_slot_is_not_overwritten(self):
        self.config.write_bytes(
            self.original + b'[mcp_servers.pulse-request-purpose]\ncommand="owned"\n'
        )
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "already owned"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), before)

    def test_modified_staged_source_is_rejected_before_config_write(self):
        (
            self.runtime / "ComputeCost/pulse_compute_cost/mcp_request_purpose.py"
        ).write_text("changed")
        with self.assertRaisesRegex(ValueError, "integrity differs"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_unpinned_worktree_cannot_be_installed_as_release(self):
        with self.assertRaisesRegex(ValueError, "exact staged"):
            install.install_mcp(
                self.home,
                ROOT,
                self.python,
                self.revision,
                expected_config_sha256="b" * 64,
            )

    def test_wrong_release_marker_rejected(self):
        (self.runtime / ".stats-source-revision").write_text("b" * 40)
        with self.assertRaisesRegex(ValueError, "marker mismatch"):
            self.apply()

    def test_source_cutover_cannot_be_removed_moved_or_duplicated(self):
        before = {
            "sources": [
                {
                    "id": "native",
                    "kind": "codex",
                    "session_wi_mcp_since": "2026-09-29T00:00:00Z",
                }
            ]
        }
        deploy.require_retained_purpose_cutover(before, deepcopy(before))
        for after in (
            {"sources": []},
            {"sources": [{"id": "native", "kind": "codex"}]},
            {"sources": before["sources"] * 2},
            {
                "sources": [
                    {
                        **before["sources"][0],
                        "session_wi_mcp_since": "2026-09-30T00:00:00Z",
                    }
                ]
            },
        ):
            with self.subTest(after=after), self.assertRaises(ValueError):
                deploy.require_retained_purpose_cutover(before, after)
        deploy.require_retained_purpose_cutover(
            {"sources": [{"kind": "codex"}]}, {"sources": []}
        )

    def test_old_reader_cannot_ignore_enabled_mcp_cutover(self):
        registry = {
            "sources": [
                {"kind": "codex", "session_wi_mcp_since": "2026-09-29T00:00:00Z"}
            ]
        }
        with (
            patch.object(deploy, "run", side_effect=RuntimeError("module absent")),
            self.assertRaisesRegex(ValueError, "native MCP purpose protocol"),
        ):
            deploy.verify_reader_features(self.runtime, registry)
        with patch.object(deploy, "run", return_value=b'"codex_session_wi/v1"'):
            deploy.verify_reader_features(self.runtime, registry)

    def test_legacy_installer_cannot_revive_hook_after_mcp_cutover(self):
        registry = {
            "schema": "computecost_source_registry/v1",
            "sources": [
                {
                    "kind": "codex",
                    "node": "ownermac",
                    "session_wi_mcp_since": "2026-09-29T00:00:00Z",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "retired script Hook"):
            install.plan(
                registry,
                {},
                home=self.home,
                runtime=self.runtime,
                python=self.python,
                site="ownermac",
            )

    def test_slot_names_the_selector_so_release_retirement_cannot_break_it(self):
        self.apply()
        slot = tomllib.loads(self.config.read_text())["mcp_servers"]["pulse-request-purpose"]
        self.assertEqual(
            slot["args"],
            [str(self.selector / "ComputeCost/runtime/codex-mcp-request-purpose.py")],
        )
        self.assertNotIn(self.revision.encode(), self.config.read_bytes())

    def test_unselected_release_is_not_installed(self):
        import shutil

        other = self.runtime.parent / ("d" * 40)
        shutil.copytree(self.runtime, other)
        (other / ".stats-source-revision").write_text("d" * 40)
        with self.assertRaisesRegex(ValueError, "selected runtime"):
            install.install_mcp(
                self.home,
                other,
                self.python,
                "d" * 40,
                expected_config_sha256=hashlib.sha256(self.original).hexdigest(),
            )
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_pinned_legacy_slot_moves_to_selector_only_when_reviewed(self):
        retired = "c" * 40
        legacy = (
            b'\n[mcp_servers.pulse-request-purpose]\ncommand = '
            + json.dumps(str(self.python)).encode()
            + b'\nargs = '
            + json.dumps([str(self.runtime.parent / retired / "ComputeCost/runtime/codex-mcp-request-purpose.py")]).encode()
            + b'\n'
        )
        self.config.write_bytes(self.original + legacy)
        previous = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "already owned"):
            self.apply()
        result = install.install_mcp(
            self.home,
            self.runtime,
            self.python,
            self.revision,
            expected_config_sha256=hashlib.sha256(previous).hexdigest(),
            expected_tool_revision=retired,
        )
        self.assertTrue(result["changed"])
        after = self.config.read_bytes()
        self.assertEqual(after.count(b"[mcp_servers.pulse-request-purpose]"), 1)
        self.assertNotIn(retired.encode(), after)
        self.assertIn(str(self.selector).encode(), after)

    def test_unrelated_native_instruction_is_never_overwritten(self):
        self.config.write_bytes(
            b'developer_instructions = "existing user rule"\n' + self.original
        )
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "existing native instructions"):
            self.apply()
        self.assertEqual(self.config.read_bytes(), before)

    def test_missing_rule_is_added_without_duplicate_tool(self):
        self.apply()
        self.config.write_bytes(self.config.read_bytes().split(b"\n", 1)[1])
        self.assertTrue(self.apply()["changed"])
        self.assertEqual(
            self.config.read_bytes().count(b"[mcp_servers.pulse-request-purpose]"), 1
        )
        self.assertEqual(
            tomllib.loads(self.config.read_text())["developer_instructions"], self.rule
        )


class McpInstructionProbeTests(unittest.TestCase):
    def test_reads_the_actual_server_rule_without_business_call(self):
        import sys
        from pulse_compute_cost.mcp_request_purpose import DESCRIPTION, TOOL

        actual = install.mcp_instructions(
            sys.executable, ROOT / "ComputeCost/runtime/codex-mcp-request-purpose.py"
        )
        self.assertEqual(actual, f"使用 {TOOL}：{DESCRIPTION}")


class SessionEnrollmentTests(unittest.TestCase):
    def test_single_source_only_and_repeat_is_idempotent(self):
        original = {
            "ledger_root": "/same",
            "sources": [
                {
                    "id": "native",
                    "kind": "codex",
                    "request_purpose_directory": "/retain",
                },
                {"id": "peer", "kind": "receipts"},
            ],
        }
        planned = deploy.session_wi_source_registry(
            original, "native", "2026-01-01T00:00:00Z"
        )
        self.assertNotIn("session_wi_mcp_since", original["sources"][0])
        self.assertEqual(planned["sources"][1], original["sources"][1])
        self.assertEqual(planned["ledger_root"], original["ledger_root"])
        self.assertEqual(
            deploy.session_wi_source_registry(
                planned, "native", "2026-01-01T00:00:00Z"
            ),
            planned,
        )
        with self.assertRaises(ValueError):
            deploy.session_wi_source_registry(planned, "native", "2026-01-02T00:00:00Z")
        with self.assertRaises(ValueError):
            deploy.session_wi_source_registry(original, "peer", "2026-01-01T00:00:00Z")

    def test_cc_source_enrollment_and_reader_gate(self):
        original = {"sources": [{"id": "cc", "kind": "cc", "path": "/projects"}]}
        planned = deploy.session_wi_source_registry(original, "cc", "2026-01-01T00:00:00Z")
        self.assertEqual(planned["sources"][0]["session_wi_mcp_since"], "2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "Codex-only"):
            deploy.session_wi_source_registry(
                original, "cc", "2026-01-01T00:00:00Z", calendar_root="/x", calendar_from="2026-01"
            )
        with (
            patch.object(deploy, "run", return_value=b'"codex_session_wi/v1"'),
            self.assertRaisesRegex(ValueError, "CC MCP purpose protocol"),
        ):
            deploy.verify_reader_features(Path("/runtime"), planned)
        with patch.object(deploy, "run", return_value=b'"cc_session_wi/v1"'):
            deploy.verify_reader_features(Path("/runtime"), planned)

    def test_pi_source_enrollment_and_reader_gate(self):
        # A Pi node whose Relay extension already offers declare_session_wi
        # enrolls the same immutable boundary as Codex and CC.
        original = {"sources": [{"id": "pi", "kind": "pi", "layout": "pi_session_root.v1"}]}
        planned = deploy.session_wi_source_registry(original, "pi", "2026-01-01T00:00:00Z")
        self.assertEqual(planned["sources"][0]["session_wi_mcp_since"], "2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "immutable"):
            deploy.session_wi_source_registry(planned, "pi", "2026-01-02T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "Codex-only"):
            deploy.session_wi_source_registry(
                original, "pi", "2026-01-01T00:00:00Z", calendar_root="/x", calendar_from="2026-01"
            )
        with (
            patch.object(deploy, "run", return_value=b'"pi_other/v1"'),
            self.assertRaisesRegex(ValueError, "Pi purpose protocol"),
        ):
            deploy.verify_reader_features(Path("/runtime"), planned)
        with patch.object(deploy, "run", return_value=b'"pi_request_wi/v1"'):
            deploy.verify_reader_features(Path("/runtime"), planned)
        # The installed reader really names this protocol.
        from pulse_compute_cost.pi_request_purpose import SCHEMA
        self.assertEqual(SCHEMA, "pi_request_wi/v1")


class TestWorkspaceAdmissionTests(unittest.TestCase):
    def test_missing_workspace_fails_before_starting_model(self):
        agent = load("session_wi_agent_test", ROOT / "ComputeCost/uat/session_wi_agent.py")
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent.subprocess, "run", side_effect=AssertionError("model started")):
            with self.assertRaisesRegex(ValueError, "registered test workspace"):
                agent.run(Path("/not-invoked"), ROOT, Path(tmp))
