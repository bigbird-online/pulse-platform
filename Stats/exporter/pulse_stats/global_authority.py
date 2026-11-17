"""Strict, read-only loading for the SCL global-authority aggregate artifact."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping


SCHEMA = "pulse_stats_global_authority_evidence/v1"
SURFACE_REGISTRY_SCHEMA = "pulse_scl_surface_registry/v3"
# Keep the v1 carrier's publication scope distinct from execution enrollment.
PUBLICATION_ENDPOINTS = (
    "surface_v1_local_macos",
    "surface_v1_cloud_linux_a",
    "surface_v1_cloud_linux_b",
    "surface_v1_chatgpt_github",
)
_SURFACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}$")
MAX_CARRIER_BYTES = 256 * 1024
MAX_READ_MODEL_BYTES = 4 * 1024 * 1024
READ_MODEL_SCHEMA = "pulse_stats_canonical_read_model/v2"
WORKFLOW_PROFILES = frozenset({"lightweight", "standard"})
_READ_MODEL_FIELDS = {"schema_version", "authority_revision", "observed_at", "expires_at", "facts"}
_ROOT_FIELDS = {
    "schema_version",
    "authority_revision",
    "required_endpoints",
    "materializations",
    "observed_at",
    "expires_at",
}
_MATERIALIZATION_FIELDS = {"endpoint_id", "authority_revision", "digest"}


class GlobalAuthorityCarrierError(ValueError):
    """The configured aggregate artifact cannot establish global authority."""


def _load_object(path: Path, *, label: str, maximum_bytes: int) -> dict[str, object]:
    try:
        size = path.stat().st_size
        if size > maximum_bytes:
            raise GlobalAuthorityCarrierError(f"{label} exceeds the size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except GlobalAuthorityCarrierError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GlobalAuthorityCarrierError(f"{label} is unavailable") from exc
    if not isinstance(value, dict):
        raise GlobalAuthorityCarrierError(f"{label} must be a JSON object")
    return value


def _required_surface_ids(registry_path: Path) -> list[str]:
    registry = _load_object(
        registry_path,
        label="surface registry",
        maximum_bytes=MAX_CARRIER_BYTES,
    )
    if registry.get("schema_version") != SURFACE_REGISTRY_SCHEMA:
        raise GlobalAuthorityCarrierError("surface registry schema is unsupported")
    raw_surfaces = registry.get("surfaces")
    if not isinstance(raw_surfaces, list):
        raise GlobalAuthorityCarrierError("surface registry inventory is unavailable")
    surface_ids = [
        row.get("surface_id") if isinstance(row, Mapping) else None
        for row in raw_surfaces
    ]
    if (
        any(not isinstance(item, str) or _SURFACE_ID_RE.fullmatch(item) is None
            for item in surface_ids)
        or len(set(surface_ids)) != len(surface_ids)
    ):
        raise GlobalAuthorityCarrierError("surface registry inventory is invalid")
    required = [item for item in surface_ids if item in PUBLICATION_ENDPOINTS]
    if required != list(PUBLICATION_ENDPOINTS):
        raise GlobalAuthorityCarrierError("surface registry publication endpoints are invalid")
    return required


def load_global_authority_evidence(
    carrier_path: Path,
    surface_registry_path: Path,
) -> dict[str, object]:
    """Load one SCL-produced aggregate without letting it choose its consumers.

    The registry must enroll all four v1 publication endpoints. The artifact
    only reports materialization evidence for those endpoints; a missing,
    duplicate, reordered, or extra endpoint invalidates the whole carrier.
    Semantic proof validation (revision, digest equality, and freshness) stays
    in :func:`producer.project_global_authority` so it can name the degraded
    proof rather than silently turning bad evidence into an empty ledger.
    """
    required_surface_ids = _required_surface_ids(surface_registry_path)
    carrier = _load_object(
        carrier_path,
        label="global authority carrier",
        maximum_bytes=MAX_CARRIER_BYTES,
    )
    if set(carrier) != _ROOT_FIELDS or carrier.get("schema_version") != SCHEMA:
        raise GlobalAuthorityCarrierError("global authority carrier schema is unsupported")

    required_endpoints = carrier.get("required_endpoints")
    if required_endpoints != required_surface_ids:
        raise GlobalAuthorityCarrierError(
            "global authority carrier required endpoint inventory conflicts with registry"
        )

    materializations = carrier.get("materializations")
    if not isinstance(materializations, list):
        raise GlobalAuthorityCarrierError(
            "global authority carrier materialization inventory is unavailable"
        )
    materialization_ids: list[object] = []
    for row in materializations:
        if not isinstance(row, dict) or set(row) != _MATERIALIZATION_FIELDS:
            raise GlobalAuthorityCarrierError(
                "global authority carrier materialization inventory is invalid"
            )
        materialization_ids.append(row.get("endpoint_id"))
    if materialization_ids != required_surface_ids:
        raise GlobalAuthorityCarrierError(
            "global authority carrier materialization inventory conflicts with registry"
        )

    return carrier

def _timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise GlobalAuthorityCarrierError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GlobalAuthorityCarrierError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GlobalAuthorityCarrierError(f"{label} is invalid")
    return parsed


def load_workflow_profiles(
    carrier_path: Path,
    read_model_path: Path,
    surface_registry_path: Path,
    *,
    now: datetime,
    expected_authority_revision: str | None,
) -> dict[str, str]:
    """Return four-surface-proven effective workflow profiles; never infer transitions."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise GlobalAuthorityCarrierError("workflow profile read time is invalid")
    carrier = load_global_authority_evidence(carrier_path, surface_registry_path)
    revision = carrier.get("authority_revision")
    if (
        not isinstance(expected_authority_revision, str)
        or revision != expected_authority_revision
    ):
        raise GlobalAuthorityCarrierError("workflow profile authority revision is not current")

    carrier_observed = _timestamp(carrier.get("observed_at"), label="carrier observed_at")
    carrier_expires = _timestamp(carrier.get("expires_at"), label="carrier expires_at")
    if not carrier_observed <= now < carrier_expires:
        raise GlobalAuthorityCarrierError("workflow profile carrier is not fresh")

    read_model = _load_object(
        read_model_path,
        label="canonical read model",
        maximum_bytes=MAX_READ_MODEL_BYTES,
    )
    if (
        set(read_model) != _READ_MODEL_FIELDS
        or read_model.get("schema_version") != READ_MODEL_SCHEMA
        or read_model.get("authority_revision") != revision
    ):
        raise GlobalAuthorityCarrierError("canonical read model is incompatible")
    read_observed = _timestamp(read_model.get("observed_at"), label="read model observed_at")
    read_expires = _timestamp(read_model.get("expires_at"), label="read model expires_at")
    if not read_observed <= now < read_expires:
        raise GlobalAuthorityCarrierError("canonical read model is not fresh")

    facts = read_model.get("facts")
    if not isinstance(facts, list) or any(not isinstance(row, Mapping) for row in facts):
        raise GlobalAuthorityCarrierError("canonical read model facts are unavailable")
    digest = "sha256:" + hashlib.sha256(
        json.dumps(
            facts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    materializations = carrier.get("materializations")
    if (
        not isinstance(materializations, list)
        or not materializations
        or any(
            not isinstance(row, Mapping)
            or row.get("authority_revision") != revision
            for row in materializations
        )
        or {row.get("digest") for row in materializations} != {digest}
    ):
        raise GlobalAuthorityCarrierError("canonical read model digest is unproven")

    result: dict[str, str] = {}
    for fact in facts:
        if fact.get("kind") != "work_item":
            continue
        profile = fact.get("workflow_profile")
        if profile is None:
            continue
        work_item_id = fact.get("work_item_id")
        sources = fact.get("workflow_profile_sources")
        if (
            profile not in WORKFLOW_PROFILES
            or not isinstance(work_item_id, str)
            or not work_item_id
            or not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) or not source for source in sources)
            or sources != sorted(set(sources))
            or work_item_id in result
        ):
            raise GlobalAuthorityCarrierError(
                "canonical work-item workflow profile is invalid"
            )
        result[work_item_id] = str(profile)
    return result
