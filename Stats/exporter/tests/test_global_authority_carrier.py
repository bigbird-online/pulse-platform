from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from pulse_stats.global_authority import (
    GlobalAuthorityCarrierError,
    load_global_authority_evidence,
    load_workflow_profiles,
)


NOW = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)
SURFACES = [
    "surface_v1_local_macos",
    "surface_v1_cloud_linux_a",
    "surface_v1_cloud_linux_b",
    "surface_v1_chatgpt_github",
]


def _registry() -> dict[str, object]:
    return {
        "schema_version": "pulse_scl_surface_registry/v3",
        "surfaces": [
            {"surface_id": surface_id, "workspace": f"workspace-{index}"}
            for index, surface_id in enumerate(SURFACES)
        ],
    }


def _carrier() -> dict[str, object]:
    revision = "sha256:" + "a" * 64
    digest = "sha256:" + "b" * 64
    return {
        "schema_version": "pulse_stats_global_authority_evidence/v1",
        "authority_revision": revision,
        "required_endpoints": list(SURFACES),
        "materializations": [
            {
                "endpoint_id": surface_id,
                "authority_revision": revision,
                "digest": digest,
            }
            for surface_id in SURFACES
        ],
        "observed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
    }


class GlobalAuthorityCarrierTests(unittest.TestCase):
    def _write(self, root: pathlib.Path, name: str, value: object) -> pathlib.Path:
        path = root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_execution_enrollment_does_not_change_four_end_consumer_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory = _registry()
            inventory["surfaces"].insert(3, {"surface_id": "surface_v1_muse_linux"})
            registry = self._write(root, "surfaces.json", inventory)
            original = registry.read_bytes()
            carrier = self._write(root, "carrier.json", _carrier())
            self.assertEqual(load_global_authority_evidence(carrier, registry)["required_endpoints"], SURFACES)
            for mutation in ("missing", "replace", "reorder", "duplicate", "bad_extra"):
                with self.subTest(mutation=mutation):
                    rows = json.loads(original)["surfaces"]
                    if mutation == "missing":
                        rows.pop()
                    elif mutation == "replace":
                        rows[-1] = {"surface_id": "surface_v1_other"}
                    elif mutation == "reorder":
                        rows.reverse()
                    elif mutation == "duplicate":
                        rows.append({"surface_id": "surface_v1_muse_linux"})
                    else:
                        rows.append({"surface_id": {"bad": True}})
                    self._write(root, "surfaces.json", {"schema_version": inventory["schema_version"], "surfaces": rows})
                    with self.assertRaises(GlobalAuthorityCarrierError):
                        load_global_authority_evidence(carrier, registry)
            self.assertEqual(json.loads(carrier.read_text())["required_endpoints"], SURFACES)

    def test_versioned_aggregate_must_cover_the_registry_inventory_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = self._write(root, "surfaces.json", _registry())
            carrier = self._write(root, "global-authority.json", _carrier())

            loaded = load_global_authority_evidence(carrier, registry)

        self.assertEqual(loaded["required_endpoints"], SURFACES)
        self.assertEqual(
            [row["endpoint_id"] for row in loaded["materializations"]],
            SURFACES,
        )

    def test_carrier_cannot_omit_a_required_surface(self) -> None:
        value = _carrier()
        value["required_endpoints"] = SURFACES[:-1]
        value["materializations"] = value["materializations"][:-1]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = self._write(root, "surfaces.json", _registry())
            carrier = self._write(root, "global-authority.json", value)

            with self.assertRaisesRegex(
                GlobalAuthorityCarrierError, "required endpoint inventory"
            ):
                load_global_authority_evidence(carrier, registry)

    def test_duplicate_materialization_cannot_overwrite_a_surface_receipt(self) -> None:
        value = _carrier()
        value["materializations"].append(dict(value["materializations"][0]))
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = self._write(root, "surfaces.json", _registry())
            carrier = self._write(root, "global-authority.json", value)

            with self.assertRaisesRegex(
                GlobalAuthorityCarrierError, "materialization inventory"
            ):
                load_global_authority_evidence(carrier, registry)

    def test_missing_or_malformed_carrier_is_unavailable_not_empty_global_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = self._write(root, "surfaces.json", _registry())
            missing = root / "missing.json"
            malformed = root / "malformed.json"
            malformed.write_text("[]", encoding="utf-8")

            for carrier in (missing, malformed):
                with self.subTest(carrier=carrier.name):
                    with self.assertRaises(GlobalAuthorityCarrierError):
                        load_global_authority_evidence(carrier, registry)


    def test_workflow_profile_requires_read_model_digest_bound_to_all_surfaces(self) -> None:
        facts = [{
            "kind": "work_item",
            "subject_id": "wi_20260911_261",
            "state": "in_progress",
            "work_item_id": "wi_20260911_261",
            "owner_ids": ["pulse"],
            "created_at": "2026-09-11T00:00:00+00:00",
            "updated_at": "2026-09-21T00:00:00+00:00",
            "terminal": False,
            "workflow_profile": "standard",
            "workflow_profile_sources": ["surface_v1_local_macos"],
        }]
        digest = "sha256:" + hashlib.sha256(
            json.dumps(
                facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        carrier_value = _carrier()
        for row in carrier_value["materializations"]:
            row["digest"] = digest
        read_model_value = {
            "schema_version": "pulse_stats_canonical_read_model/v2",
            "authority_revision": carrier_value["authority_revision"],
            "observed_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
            "facts": facts,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = self._write(root, "surfaces.json", _registry())
            carrier = self._write(root, "global-authority.json", carrier_value)
            read_model = self._write(root, "read-model.json", read_model_value)

            self.assertEqual(
                load_workflow_profiles(
                    carrier,
                    read_model,
                    registry,
                    now=NOW,
                    expected_authority_revision=carrier_value["authority_revision"],
                ),
                {"wi_20260911_261": "standard"},
            )
            with self.assertRaisesRegex(GlobalAuthorityCarrierError, "not current"):
                load_workflow_profiles(
                    carrier,
                    read_model,
                    registry,
                    now=NOW,
                    expected_authority_revision="sha256:" + "c" * 64,
                )
            with self.assertRaisesRegex(GlobalAuthorityCarrierError, "not fresh"):
                load_workflow_profiles(
                    carrier,
                    read_model,
                    registry,
                    now=NOW + timedelta(minutes=16),
                    expected_authority_revision=carrier_value["authority_revision"],
                )

            read_model_value["facts"][0]["workflow_profile"] = "lightweight"
            self._write(root, "read-model.json", read_model_value)
            with self.assertRaisesRegex(
                GlobalAuthorityCarrierError, "digest is unproven"
            ):
                load_workflow_profiles(
                    carrier,
                    read_model,
                    registry,
                    now=NOW,
                    expected_authority_revision=carrier_value["authority_revision"],
                )


if __name__ == "__main__":
    unittest.main()
