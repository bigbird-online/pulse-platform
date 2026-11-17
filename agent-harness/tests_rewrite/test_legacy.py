"""The original source catalog stays readable without granting execution."""
import json

from harness_rewrite import legacy

WI = "wi_20261008_550"


def record():
    return {"work_item_id": WI, "title": "Original title", "owner": "pulse", "status": "draft",
            "acceptance_criteria": ["A1", "A2"], "artifact_refs": [], "main_only": True,
            "cross_tenant": False, "created_at": "2026-10-08T06:08:04Z",
            "updated_at": "2026-10-08T06:08:04Z", "source": "original request"}


def test_catalog_distinguishes_source_presence_from_native_absence(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "schema": "harness.legacy_catalog/v1",
        "source": {"repository": "fixture", "revision": "c" * 40, "snapshot_only": True},
        "items": [{"work_item_id": WI, "record": record(), "conversion_reasons": []}],
    }))
    missing = {"status": "rejected", "error": {"code": "domain_conflict", "reason": "Work item does not exist"}}
    observed = legacy.catalog_observation({"legacy_catalog": str(path)}, missing, wi=WI)
    assert observed["error"]["code"] == "legacy_not_imported"
    assert observed["legacy_source"]["items"][0]["record"]["acceptance_criteria"] == ["A1", "A2"]
    assert observed["legacy_source"]["execution_authority"] is False
    path.write_text("{}")
    assert legacy.catalog_observation({"legacy_catalog": str(path)}, missing, wi=WI)["legacy_source"]["state"] == "unavailable"
