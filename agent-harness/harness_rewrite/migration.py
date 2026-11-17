"""Reducer for the retired sealed history transfer between fact stores.

Cross-store migration is retired with the old track: every migration intent is
rejected. Historical Migration* and MigratedEffectsAdopted events still replay.
"""

from __future__ import annotations

from . import workflow
from .model import Decision, HostContext, Json, Rejected

INTENTS = frozenset(
    {
        "seal_migration",
        "prepare_migration",
        "activate_migration",
        "adopt_migrated_effects",
    }
)


def blocked(state: Json, context: HostContext, intent: str) -> None:
    item = state.get("migration")
    if item is None:
        return
    if item["phase"] in {"sealed", "prepared"} and intent not in {
        "prepare_migration",
        "activate_migration",
    }:
        raise Rejected(
            "migration_sealed", "Sealed history cannot regain source-side authority"
        )
    if item["phase"] == "active" and context.fact_store != item["destination"]:
        raise Rejected(
            "authority_location_changed",
            "Migrated work belongs to its exact destination store",
        )


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str
) -> Decision:
    raise Rejected(
        "track_retired",
        "Cross-store migration is retired with the old track; history remains readable",
    )


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "MigrationSealed":
        workflow.invalidate_attempts(state)
        for grant in state["grants"].values():
            grant["active"] = False
        state.update(status="migration_sealed", holder=None, transfer=None)
        state["migration"] = {**data, "phase": "sealed", "adopted_effects": []}
    elif kind == "MigrationPrepared":
        state["migration"].update(
            phase="prepared", package_digest=data["package_digest"]
        )
        state["status"] = "migration_prepared"
    elif kind == "MigrationActivated":
        item = state["migration"]
        item["phase"] = "active"
        state.update(
            status=item["prior_status"],
            holder=item["successor"],
            generation=data["generation"],
        )
        state["definition"] = {
            **state["definition"],
            "owner": item["successor"]["actor"],
        }
    elif kind == "MigratedEffectsAdopted":
        for key in data["effect_ids"]:
            state["effects"][key]["generation"] = data["generation"]
            if key not in state["migration"]["adopted_effects"]:
                state["migration"]["adopted_effects"].append(key)
    else:
        return False
    state.setdefault("migration_history", []).append({"kind": kind, **data})
    return True


def public(state: Json) -> Json | None:
    item = state.get("migration")
    if item is None:
        return None
    result = {
        key: item[key]
        for key in (
            "migration_id",
            "phase",
            "destination_alias",
            "pending_effects",
            "adopted_effects",
            "inherited_resources",
        )
    }
    result["binding_bound_effects"] = sorted(binding_bound_effects(state))
    return result


def binding_bound_effects(state: Json) -> set[str]:
    item = state.get("migration")
    if item is None:
        return set()
    return set(item["pending_effects"]) | {
        key
        for key, effect in state["effects"].items()
        if effect["effect_kind"] == "workspace_retire"
        and effect.get("snapshot", {}).get("resource_id")
        in item.get("inherited_resources", [])
    }


def effect_admitted(state: Json, effect_id: str, context: HostContext) -> None:
    item = state.get("migration")
    if (
        item is not None
        and effect_id in binding_bound_effects(state)
        and (
            context.effect_binding is None
            or context.effect_binding != item["effect_binding"]
        )
    ):
        raise Rejected(
            "migration_effect_unadopted",
            "The current adapter no longer matches the retained physical binding",
        )
    if (
        item
        and effect_id in item["pending_effects"]
        and effect_id not in item["adopted_effects"]
    ):
        raise Rejected(
            "migration_effect_unadopted",
            "Retained effects require explicit original-binding adoption before observation or send",
        )


def inherited_resources(state: Json) -> list[str]:
    from .workspaces import retired

    item = state.get("migration")
    if item is None or item["phase"] != "active":
        return []
    return [
        key
        for key in item.get("inherited_resources", [])
        if not retired(state, state["workspaces"][key])
        and not state["workspaces"][key].get("superseded_by")
    ]


def observation_blocks_effect(view: Json, effect_id: str) -> bool:
    item = view.get("migration")
    return bool(
        item
        and (
            item["phase"] != "active"
            or effect_id in item["pending_effects"]
            and effect_id not in item["adopted_effects"]
            or effect_id in item["binding_bound_effects"]
            and not item.get("physical_effects_available")
        )
    )
