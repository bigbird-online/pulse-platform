"""Physical resource intentions share the journal and original effect protocol."""

from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import re
from typing import Any

from . import guide, workflow
from .repository_identity import repository_values
from .model import (
    Decision,
    HostContext,
    Json,
    Rejected,
    fields,
    human_decision,
    identity,
)

INTENTS = frozenset(
    {"allocate_workspaces", "replace_workspace", "revise_workspace", "retire_resources"}
)
EFFECT_KINDS = frozenset({"workspace_allocate", "workspace_revise", "workspace_retire"})
# The effect service reclaims a terminal WI's workspaces: the human decision that
# ended the WI covers its cleanup, and physical retirement still refuses dirty
# trees and unpublished commits.
WORKER_INTENTS = frozenset({"retire_terminal_resources"})
TERMINAL_RECLAIM_REASON = "Terminal WI: automatic workspace reclaim"


def event(kind: str, **payload: Any) -> Json:
    return {"kind": kind, "payload": payload}


def sparse_paths(value: Any) -> list[str]:
    if value == []:
        return []
    paths = workflow.resource_paths(value)
    if any(not re.fullmatch(r"[A-Za-z0-9_./-]+", path) for path in paths):
        raise Rejected("invalid_input", "Sparse paths must be literal relative paths")
    return paths


def successful(state: Json, effect_id: str) -> bool:
    return bool(state["effects"][effect_id]["status"] == "observed_success")


def ready(state: Json, resource: Json) -> bool:
    return bool(resource.get("existing_allocation") and resource["effect_id"] is None) or successful(state, resource["effect_id"])


def retired(state: Json, resource: Json) -> bool:
    return bool(resource.get("retirement") and successful(state, resource["retirement"]))


def effect(operation: str, kind: str, resource: Json, generation: int) -> Json:
    return event(
        "EffectRequested",
        effect_id=operation,
        effect_kind=kind,
        candidate_id=None,
        node_id=None,
        required=False,
        generation=generation,
        target=resource["target"],
        snapshot=repository_values(resource),
    )


def same_repository(admitted: Json | None, original: Json) -> bool:
    """A moved managed root keeps earlier workspaces where they were allocated."""
    if admitted is None:
        return False
    return {k: v for k, v in admitted.items() if k != "managed_root"} == {
        k: v for k, v in original.items() if k != "managed_root"
    }


# A read-back failure is final: nothing acts on it again unless its owner asks
# retry_effect, so, like success or cancellation, it no longer holds a terminal
# WI's workspaces. Requested, started, unknown and proven-absent effects may
# still act (absence may begin a new round) and keep them.
SETTLED_STATUSES = frozenset({"observed_success", "cancelled", "observed_failure"})


def unsettled(state: Json) -> bool:
    """Whether an original effect may still act, so retirement must wait."""
    return any(
        e["status"] not in SETTLED_STATUSES
        and e["effect_kind"] != "workspace_retire"
        for e in state["effects"].values()
    )


def inherited_elsewhere(state: Json, resource_id: str, context: HostContext) -> bool:
    inherited = state.get("migration")
    return bool(
        inherited
        and resource_id in inherited.get("inherited_resources", [])
        and (
            context.effect_binding is None
            or context.effect_binding != inherited["effect_binding"]
        )
    )


def retirement(state: Json, ids: list[str], reason: str, operation: str) -> Decision:
    resources = state.get("workspaces", {})
    events = []
    effects = []
    for index, resource_id in enumerate(ids):
        resource = resources[resource_id]
        if resource.get("retirement"):
            effects.append(resource["retirement"])
            continue
        effect_id = f"{operation}.{index}"
        from .publication import safe_retirement

        resource = safe_retirement(state, resource)
        events += [
            effect(effect_id, "workspace_retire", resource, state["generation"]),
            event(
                "WorkspaceRetirementRequested",
                resource_id=resource_id,
                effect_id=effect_id,
                reason=reason,
            ),
        ]
        effects.append(effect_id)
    return Decision(events, {"effect_ids": effects})


def reclaim_terminal(state: Json, data: Json, context: HostContext, operation: str) -> Decision:
    """Worker request: retire every ready, unretired resource of a terminal WI."""
    from . import delivery

    fields(data, set())
    if state["status"] not in {"completed", "terminated"}:
        raise Rejected("domain_conflict", "Only a terminal WI's workspaces are reclaimed")
    if unsettled(state) or delivery.public_obligations(state)["unsettled"]:
        return Decision([], {"effect_ids": [], "deferred": "unsettled_effects"})
    ids = [
        resource_id
        for resource_id, resource in state.get("workspaces", {}).items()
        if not resource.get("retirement")
        and ready(state, resource)
        and not inherited_elsewhere(state, resource_id, context)
    ]
    return retirement(state, ids, TERMINAL_RECLAIM_REASON, operation)


def decide(
    state: Json, intent: str, data: Json, context: HostContext, operation: str, wi: str
) -> Decision:
    from .domain import authorized, denial

    resources = state.get("workspaces", {})
    if intent == "retire_resources":
        fields(data, {"resource_ids", "reason"})
        workflow.nonempty(data["reason"])
        human_decision(context, intent, data, wi)
        ids = data["resource_ids"]
        if (
            state["status"] not in {"completed", "terminated"}
            or context.actor != state["definition"]["owner"]
        ):
            raise Rejected(
                "authority_denied",
                "Terminal owner and exact inventory approval required",
            )
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > 64
            or any(not isinstance(i, str) or i not in resources for i in ids)
            or len(ids) != len(set(ids))
        ):
            raise Rejected("invalid_input", "Retirement requires known unique resource IDs")
        if unsettled(state):
            raise Rejected("domain_conflict", "Unsettled original effects must be preserved")
        if any(inherited_elsewhere(state, key, context) for key in ids):
            raise Rejected(
                "authority_denied",
                "Original physical resource bindings are not available at this destination",
            )
        return retirement(state, ids, data["reason"], operation)
    rows: list[Json]
    predecessor: Json | None = None
    replacement_source: Json | None = None
    if intent == "replace_workspace":
        fields(data, {"resource_id", "snapshot_id", "resources", "sparse", "purpose"})
        identity(data["resource_id"])
        if data["snapshot_id"] is not None:
            identity(data["snapshot_id"])
        predecessor = resources.get(data["resource_id"])
        if predecessor is None or predecessor.get("superseded_by"):
            raise Rejected("domain_conflict", "Current original workspace required")
        if predecessor.get("retirement") or not ready(state, predecessor):
            raise Rejected("domain_conflict", "Original workspace intention is unsettled")
        if not authorized(state, context, intent, predecessor["resources"]):
            raise Rejected("authority_denied", "Original workspace scope is not owned: "
                           + denial(state, context, intent, predecessor["resources"], wi=wi))
        if any(
            a["status"] == "running"
            and any(
                workflow.overlaps(x, y) for x in predecessor["resources"] for y in a["resources"]
            )
            for a in state["attempts"].values()
        ):
            raise Rejected("domain_conflict", "A task still uses the original workspace")
        if data["snapshot_id"] is not None:
            replacement_source = state.get("sources", {}).get(data["snapshot_id"])
            if (
                replacement_source is None
                or replacement_source["resource_id"] != predecessor["resource_id"]
            ):
                raise Rejected(
                    "source_invalid",
                    "Replacement source must be an original captured snapshot",
                )
        rows = [
            {
                "repo_id": predecessor["repo_id"],
                "base_commit": replacement_source["commit"]
                if replacement_source
                else predecessor["base_commit"],
                "resources": data["resources"],
                "sparse": data["sparse"],
                "purpose": data["purpose"],
            }
        ]
    elif intent == "allocate_workspaces":
        fields(data, *guide.shape("allocate_workspaces"))
        if not isinstance(data["workspaces"], list) or not 1 <= len(data["workspaces"]) <= 32:
            raise Rejected("invalid_input", "Expected a bounded workspace request")
        rows = data["workspaces"]
    else:
        fields(data, {"resource_id", "resources", "sparse", "purpose"})
        rows = [data]
    events = []
    selected: list[Json] = []
    bindings = {binding.repo_id: asdict(binding) for binding in context.repositories}
    for index, row in enumerate(rows):
        allocating = intent in {"allocate_workspaces", "replace_workspace"}
        fields(
            row,
            {"repo_id", "base_commit", "resources", "sparse", "purpose"}
            if allocating
            else {"resource_id", "resources", "sparse", "purpose"},
        )
        scope = workflow.resource_paths(row["resources"])
        sparse = sparse_paths(row["sparse"])
        workflow.nonempty(row["purpose"])
        if not authorized(state, context, intent, scope):
            raise Rejected("authority_denied", "Current resource execution scope required: "
                           + denial(state, context, intent, scope, wi=wi))
        if any(
            a["status"] == "running"
            and any(workflow.overlaps(x, y) for x in scope for y in a["resources"])
            for a in state["attempts"].values()
        ):
            raise Rejected("domain_conflict", "Active task occupies the requested resources")
        effect_id = f"{operation}.{index}"
        if allocating:
            identity(row["repo_id"])
            if row["repo_id"] not in bindings:
                raise Rejected("authority_denied", "Repository is not admitted by this Host")
            if not isinstance(row["base_commit"], str) or not re.fullmatch(
                r"[0-9a-f]{40}|[0-9a-f]{64}", row["base_commit"]
            ):
                raise Rejected("invalid_input", "Workspace requires an exact commit identity")
            resource_id = "workspace." + effect_id
            resource = {
                "resource_id": resource_id,
                "repo_id": row["repo_id"],
                "base_commit": row["base_commit"],
                "binding": bindings[row["repo_id"]],
                "target": wi + "/" + resource_id,
                "branch": "harness/" + wi + "/" + effect_id,
                "allocation": effect_id,
                "retirement": None,
            }
            if predecessor is not None:
                retained_original = False
                if predecessor.get("existing_allocation"):
                    from .existing_execution import retained_binding

                    # The journal admits the old physical allocation. A new
                    # allocation uses today's Host root for that same repository;
                    # it neither rewrites nor retires the original workspace.
                    retained_original = repository_values(
                        retained_binding(predecessor, resource["binding"])
                    ) == repository_values(predecessor["binding"])
                migration = state.get("migration")
                destination_replacement = bool(
                    migration
                    and migration["phase"] == "active"
                    and context.fact_store == migration["destination"]
                    and predecessor["resource_id"] in migration.get("inherited_resources", [])
                )
                if (
                    not same_repository(resource["binding"], repository_values(predecessor["binding"]))
                    and not destination_replacement
                    and not retained_original
                ):
                    raise Rejected(
                        "authority_denied",
                        "Replacement requires the admitted original repository",
                    )
                resource["replaces"] = predecessor["resource_id"]
                resource["replacement_source"] = replacement_source
        else:
            resource_id = row["resource_id"]
            if not isinstance(resource_id, str) or resource_id not in resources:
                raise Rejected("domain_conflict", "Unknown workspace")
            resource = repository_values(resources[resource_id])
            admitted = bindings.get(resource["repo_id"])
            if resource.get("existing_allocation") and admitted is not None:
                from .existing_execution import retained_binding
                admitted = retained_binding(resource, admitted)
            if not same_repository(admitted, resource["binding"]):
                raise Rejected(
                    "authority_denied",
                    "Original physical repository binding is no longer admitted",
                )
            if (
                resource.get("superseded_by")
                or resource.get("retirement")
                or not ready(state, resource)
            ):
                raise Rejected("domain_conflict", "Original workspace operation is not settled")
            if not authorized(state, context, intent, resource["resources"]):
                raise Rejected("authority_denied", "Previous resource scope is no longer owned: "
                               + denial(state, context, intent, resource["resources"], wi=wi))
            if any(
                a["status"] == "running"
                and any(
                    workflow.overlaps(x, y) for x in resource["resources"] for y in a["resources"]
                )
                for a in state["attempts"].values()
            ):
                raise Rejected(
                    "domain_conflict",
                    "A running task still occupies the previous scope",
                )
        for other in [*resources.values(), *selected]:
            if (
                other["resource_id"] != resource_id
                and (predecessor is None or other["resource_id"] != predecessor["resource_id"])
                and not other.get("superseded_by")
                and not retired(state, other)
                and other["repo_id"] == resource["repo_id"]
                and any(workflow.overlaps(x, y) for x in scope for y in other["resources"])
            ):
                raise Rejected(
                    "domain_conflict",
                    "Another live workspace owns this repository scope",
                )
        resource.update(resources=scope, sparse=sparse, purpose=row["purpose"], effect_id=effect_id)
        selected.append(resource)
        events += [
            event("WorkspaceRequested", resource=resource),
            effect(
                effect_id,
                "workspace_allocate" if allocating else "workspace_revise",
                resource,
                state["generation"],
            ),
        ]
        if predecessor is not None:
            events.append(
                event(
                    "WorkspaceReplaced",
                    resource_id=predecessor["resource_id"],
                    successor_id=resource_id,
                )
            )
    return Decision(
        events,
        {
            "resource_ids": [r["resource_id"] for r in selected],
            "effect_ids": [r["effect_id"] for r in selected],
        },
    )


def obsolete(state: Json, effect: Json) -> bool:
    """The intention no longer names the current resource action or generation.

    It can never send or resume again; an execution-generation change (for a
    child WI, its parent's) or a replaced/ended resource makes it so.
    """
    resource = state.get("workspaces", {}).get(effect["snapshot"]["resource_id"])
    retiring = effect["effect_kind"] == "workspace_retire"
    return (
        resource is None
        or state["status"] not in ({"completed", "terminated"} if retiring else {"active"})
        or (resource.get("retirement") if retiring else resource["effect_id"])
        != effect["effect_id"]
        or effect["generation"] != state["generation"]
    )


def settles_obsolete(state: Json, effect: Json) -> bool:
    """An obsolete allocation or revision proven not to have acted is cancelled.

    A retirement is never cancelled: its resource still names it, and only
    physical readback settles it.
    """
    return effect["effect_kind"] in {"workspace_allocate", "workspace_revise"} and obsolete(
        state, effect
    )


def can_begin(state: Json, effect: Json) -> None:
    if obsolete(state, effect):
        raise Rejected(
            "domain_conflict",
            "Obsolete resource intention cannot start an external action",
        )


def withdrawal(state: Json, effect: Json) -> list[Json]:
    """Undo the request of a cancelled allocation or revision that never acted.

    A revision restores the resource's last confirmed record; an allocation
    removes its never-created resource and frees a replaced predecessor.
    """
    if effect["effect_kind"] not in {"workspace_allocate", "workspace_revise"}:
        return []
    resource_id = effect["snapshot"]["resource_id"]
    resource = state.get("workspaces", {}).get(resource_id)
    if resource is None or resource["effect_id"] != effect["effect_id"]:
        return []
    if effect["effect_kind"] == "workspace_revise" and resource_id not in state.get(
        "workspace_priors", {}
    ):
        return []
    return [event("WorkspaceRequestWithdrawn", resource_id=resource_id,
                  effect_id=effect["effect_id"])]


def apply_event(state: Json, kind: str, data: Json) -> bool:
    if kind == "WorkspaceRequested":
        resources = state.setdefault("workspaces", {})
        priors = state.setdefault("workspace_priors", {})
        resource_id = data["resource"]["resource_id"]
        # The confirmed record a revision replaces, restored if it is withdrawn.
        if resource_id in resources:
            priors[resource_id] = resources[resource_id]
        else:
            priors.pop(resource_id, None)
        resources[resource_id] = data["resource"]
    elif kind == "WorkspaceRequestWithdrawn":
        resources = state["workspaces"]
        prior = state.setdefault("workspace_priors", {}).pop(data["resource_id"], None)
        if prior is not None:
            resources[data["resource_id"]] = prior
        else:
            withdrawn = resources.pop(data["resource_id"])
            predecessor = resources.get(withdrawn.get("replaces") or "")
            if predecessor is not None and predecessor.get("superseded_by") == data["resource_id"]:
                del predecessor["superseded_by"]
    elif kind == "WorkspaceRetirementRequested":
        state["workspaces"][data["resource_id"]]["retirement"] = data["effect_id"]
    elif kind == "WorkspaceReplaced":
        state["workspaces"][data["resource_id"]]["superseded_by"] = data["successor_id"]
    else:
        return False
    return True


def public(state: Json) -> list[Json]:
    return [
        {
            **{k: v for k, v in resource.items() if k != "binding"},
            "path": resource["binding"]["managed_root"] + "/" + resource["target"],
            "status": "retired"
            if retired(state, resource)
            else "existing_allocation_observed"
            if resource.get("existing_allocation") and resource["effect_id"] is None and not resource.get("retirement")
            else state["effects"][resource.get("retirement") or resource["effect_id"]]["status"],
        }
        for resource in state.get("workspaces", {}).values()
    ]


BRANCH = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")


def recorded_git_dir(state: Json, resource: Json) -> str | None:
    """The administrative directory the allocation or revision observed, if it recorded one."""
    effect = state["effects"].get(resource["effect_id"] or "")
    for observation in reversed((effect or {}).get("observations", [])):
        if observation["outcome"] == "success":
            value = observation["details"].get("git_dir")
            return value if isinstance(value, str) else None
    return None


def session_write(wi: str, state: Json) -> list[Json]:
    """The workspaces of `wi` that its holding session may write, before physical proof.

    Only a ready workspace of an active WI that is neither superseded nor
    asked to retire, newly allocated or an admitted existing allocation, on a
    branch that is a plain relative ref path. Each candidate carries what
    `writable` proves on the host before Relay opens anything: the worktree,
    the bound common Git directory, the branch and the administrative
    directory the allocation recorded (none for an existing allocation or one
    observed before it was recorded).
    """
    if state["status"] != "active":
        return []
    rows = []
    for resource in state.get("workspaces", {}).values():
        if resource.get("superseded_by") or resource.get("retirement") or not ready(state, resource):
            continue
        branch = resource["branch"]
        if not BRANCH.fullmatch(branch) or any(
            part in {".", ".."} or part.endswith(".lock") for part in branch.split("/")
        ):
            continue
        binding = resource["binding"]
        rows.append(
            {
                "work_item_id": wi,
                "resource_id": resource["resource_id"],
                "path": binding["managed_root"] + "/" + resource["target"],
                "common_dir": binding["common_dir"],
                "branch": branch,
                "git_dir": recorded_git_dir(state, resource),
            }
        )
    return rows


def _pointer(path: Path, prefix: str = "") -> Path | None:
    """The absolute path a Git pointer file names, or None."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        text = path.read_text().strip()
    except (OSError, UnicodeError):
        return None
    if not text.startswith(prefix):
        return None
    target = Path(text[len(prefix):])
    return Path(os.path.realpath(target if target.is_absolute() else path.parent / target))


def worktree_admin(path: str, common_dir: str, branch: str, recorded: str | None) -> str | None:
    """The worktree's own administrative directory, proven on the host, else None.

    Git names it after the worktree's last path component and adds a number on
    a clash, so it is never inferred from the path. The candidate is the
    recorded one or, for a workspace that recorded none, the one the
    worktree's `.git` link names. It must be a real directory directly under
    the common directory's `worktrees/`, name the worktree back (its `gitdir`
    file is the worktree's `.git`), be the one the worktree's `.git` names,
    and have this branch checked out.
    """
    tree = Path(os.path.realpath(path))
    link = _pointer(tree / ".git", "gitdir: ")
    if link is None or (recorded is not None and Path(os.path.realpath(recorded)) != link):
        return None
    worktrees = Path(os.path.realpath(common_dir)) / "worktrees"
    if link.parent != worktrees or link.is_symlink() or not link.is_dir():
        return None
    if _pointer(link / "gitdir") != tree / ".git":
        return None
    try:
        head = (link / "HEAD").read_text().strip()
    except (OSError, UnicodeError):
        return None
    return str(link) if head == "ref: refs/heads/" + branch else None


def writable(candidates: list[Json]) -> list[Json]:
    """The rows Relay opens: each candidate whose administrative directory is proven.

    `write` is the worktree and exactly what `git add` and `git commit` there
    write in the common Git directory: the proven administrative directory,
    the shared object store, and the branch's own ref, ref lock and reflog; no
    ref directory opens, so no other branch does. `deny` stays closed inside
    them: the worktree's `.git` link and the administrative files that
    redirect Git or carry configuration, which unsandboxed Git (the effect
    service's ownership checks) would otherwise follow; and the object
    store's pack and info directories (packs, alternates). A candidate whose
    ownership cannot be proven opens nothing.
    """
    rows = []
    for candidate in candidates:
        admin = worktree_admin(
            candidate["path"], candidate["common_dir"], candidate["branch"], candidate["git_dir"]
        )
        if admin is None:
            continue
        path, common = candidate["path"], candidate["common_dir"]
        ref = common + "/refs/heads/" + candidate["branch"]
        rows.append(
            {
                "work_item_id": candidate["work_item_id"],
                "resource_id": candidate["resource_id"],
                "path": path,
                "write": [
                    path,
                    admin,
                    common + "/objects",
                    ref,
                    ref + ".lock",
                    common + "/logs/refs/heads/" + candidate["branch"],
                ],
                "deny": [
                    path + "/.git",
                    *(admin + "/" + name for name in ("config.worktree", "commondir", "gitdir")),
                    common + "/objects/pack",
                    common + "/objects/info",
                ],
            }
        )
    return rows


def settlement(state: Json) -> Json:
    rows = public(state)
    return {
        "resources": rows,
        "resources_retired": bool(rows) and all(r["status"] == "retired" for r in rows),
        "unretired_resource_count": sum(r["status"] != "retired" for r in rows),
        "inventory": "managed_journal_resources_only",
    }
