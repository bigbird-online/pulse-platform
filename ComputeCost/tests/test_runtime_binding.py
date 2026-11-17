from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from pulse_compute_cost.runtime_binding import (
    RUNTIME_USAGE_BINDING_ENV,
    RUNTIME_USAGE_BINDING_FILE_ENV,
    RuntimeUsageBinding,
    RuntimeUsageBindingError,
    build_runtime_usage_binding,
    load_runtime_usage_binding,
    write_runtime_usage_binding,
)


class RuntimeUsageBindingTests(unittest.TestCase):
    def binding(self) -> RuntimeUsageBinding:
        return build_runtime_usage_binding(
            work_item_id="wi_20260822_730",
            agent_id="pulse",
            phase="work_item",
            epoch=12,
            runtime="pi",
            procurement_source="chatgpt_subscription",
            authority_ref="session_v1_" + "a" * 64,
            execution_surface="surface_v1_local_macos",
            issued_at="2026-08-25T01:00:00Z",
        )

    def test_build_validate_and_replay_are_byte_stable(self) -> None:
        first = self.binding()
        second = build_runtime_usage_binding(
            work_item_id=first.work_item_id,
            agent_id=first.agent_id,
            phase=first.phase,
            epoch=first.epoch,
            runtime=first.runtime,
            procurement_source=first.procurement_source,
            authority_ref=first.authority_ref,
            execution_surface=first.execution_surface,
            issued_at=first.issued_at,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.schema, "pulse_runtime_usage_binding/v3")
        self.assertEqual(first.execution_surface, "surface_v1_local_macos")
        self.assertEqual(RuntimeUsageBinding.from_mapping(first.to_dict()), first)

    def test_cross_language_utc_precision_is_preserved_and_loadable(self) -> None:
        for issued_at in (
            "2026-09-01T11:03:35.428Z",  # JavaScript Date.toISOString()
            "2026-09-01T11:03:35.428000Z",  # Python datetime.isoformat()
        ):
            binding = build_runtime_usage_binding(
                work_item_id="wi_20260822_730",
                agent_id="pulse",
                phase="work_item",
                epoch=12,
                runtime="pi",
                procurement_source="chatgpt_subscription",
                authority_ref="session_v1_" + "a" * 64,
                issued_at=issued_at,
            )
            self.assertEqual(binding.issued_at, issued_at)
            self.assertEqual(
                load_runtime_usage_binding(
                    {RUNTIME_USAGE_BINDING_ENV: json.dumps(binding.to_dict())}
                ),
                binding,
            )

    def test_tamper_unknown_fields_and_private_identity_fail_closed(self) -> None:
        value = self.binding().to_dict()
        value["epoch"] = 13
        with self.assertRaisesRegex(RuntimeUsageBindingError, "digest"):
            RuntimeUsageBinding.from_mapping(value)

        value = self.binding().to_dict()
        value["thread_id"] = "private-thread"
        with self.assertRaisesRegex(RuntimeUsageBindingError, "fields"):
            RuntimeUsageBinding.from_mapping(value)

        value = self.binding().to_dict()
        value.pop("agent_id")
        with self.assertRaisesRegex(RuntimeUsageBindingError, "fields"):
            RuntimeUsageBinding.from_mapping(value)

        value = self.binding().to_dict()
        value["authority_ref"] = "session_v1_" + "b" * 64
        with self.assertRaisesRegex(RuntimeUsageBindingError, "digest"):
            RuntimeUsageBinding.from_mapping(value)

        for issued_at in (
            "2026-08-25T09:00:00+08:00",
            "2026-08-25T01:00:00.1234567Z",
            "2026-02-30T01:00:00Z",
        ):
            with self.assertRaisesRegex(RuntimeUsageBindingError, "canonical UTC"):
                build_runtime_usage_binding(
                    work_item_id="wi_20260822_730",
                    agent_id="pulse",
                    phase="work_item",
                    epoch=12,
                    runtime="pi",
                    procurement_source="chatgpt_subscription",
                    issued_at=issued_at,
                )

    def test_host_must_inject_exactly_one_source(self) -> None:
        inline = json.dumps(self.binding().to_dict())
        with self.assertRaisesRegex(RuntimeUsageBindingError, "exactly one"):
            load_runtime_usage_binding({})
        with self.assertRaisesRegex(RuntimeUsageBindingError, "exactly one"):
            load_runtime_usage_binding(
                {
                    RUNTIME_USAGE_BINDING_ENV: inline,
                    RUNTIME_USAGE_BINDING_FILE_ENV: "/tmp/also-present",
                }
            )
        self.assertEqual(
            load_runtime_usage_binding({RUNTIME_USAGE_BINDING_ENV: inline}),
            self.binding(),
        )

    def test_atomic_file_publish_is_private_and_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding.json"
            write_runtime_usage_binding(path, self.binding())
            self.assertEqual(
                load_runtime_usage_binding({RUNTIME_USAGE_BINDING_FILE_ENV: str(path)}),
                self.binding(),
            )
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            serialized = path.read_text(encoding="utf-8")
            for forbidden in ("session_id", "thread_id", "message_id", "cwd"):
                self.assertNotIn(forbidden, serialized)

    def test_relative_or_symlink_binding_file_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeUsageBindingError, "absolute regular"):
            load_runtime_usage_binding({RUNTIME_USAGE_BINDING_FILE_ENV: "relative.json"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "binding.json"
            link = root / "link.json"
            write_runtime_usage_binding(target, self.binding())
            link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeUsageBindingError, "absolute regular"):
                load_runtime_usage_binding({RUNTIME_USAGE_BINDING_FILE_ENV: str(link)})


if __name__ == "__main__":
    unittest.main()
