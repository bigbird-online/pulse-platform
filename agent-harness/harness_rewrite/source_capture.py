"""Capture owned Git object bytes at the native boundary, before freezing a result."""

from dataclasses import asdict
import base64
import hashlib
import os
from pathlib import Path

from .git_workspaces import GitWorkspaces
from .model import ArtifactInput, HostContext, Json, Query, Rejected
from .kernel import Kernel
from .publication import commit


def artifact_roots(
    kernel: Kernel, wi: str, handle: object, intent: str, paths: tuple[str, ...]
) -> tuple[Path, ...]:
    """Derive file capture scope from the same journal that grants execution.

    The Host's static inbox roots remain available. A continued allocation adds
    only its exact physical workspace while this native principal can act on it.
    No policy edit, caller path grant, or new physical ownership marker is needed.
    """
    from .domain import authorized
    from .existing_execution import retained_binding
    from .workspaces import ready, retired

    context = kernel._host(handle)
    if context.read_only_reason:
        return ()
    candidates = tuple(Path(path).resolve(strict=True) for path in paths)
    roots = []
    bindings = {r.repo_id: asdict(r) for r in context.repositories}
    with kernel._connection(write=False, work_item_id=wi) as connection:
        state, _, _, _ = kernel._replay(connection, context.namespace, wi)
        if state is None:
            return ()
        for resource in state.get("workspaces", {}).values():
            if (
                not ready(state, resource) or retired(state, resource)
                or resource.get("retirement") or resource.get("superseded_by")
                or not authorized(
                    state, context, intent, resource["resources"], same_agent_records=True
                )
                or resource["repo_id"] not in bindings
            ):
                continue
            bound = {
                **resource,
                "binding": retained_binding(resource, bindings[resource["repo_id"]]),
            }
            expected = Path(bound["binding"]["managed_root"]) / bound["target"]
            if not any(expected == path or expected in path.parents for path in candidates):
                continue
            git = GitWorkspaces()
            source, root, target = git.layout(bound)
            git.owned(source, root, target, bound)
            roots.append(target)
    return tuple(roots)


def retained(
    kernel: Kernel, wi: str, handle: object, resource_id: str, revision: str
) -> tuple[Json, ArtifactInput] | None:
    """Recover an already captured object, never assert a new live HEAD reading.

    This permits exact command retries after a workspace changes or retires.
    execute still checks original request semantics and current authority for
    new requests; this read cannot reacquire a producer grant.
    """
    response = kernel.inspect(Query(wi), handle)
    if response["status"] == "rejected":
        raise Rejected(response["error"]["code"], response["error"]["reason"])
    snapshots = response["result"]["current_observation"]["source_publication"][
        "snapshots"
    ]
    snapshot = next(
        (
            s
            for s in snapshots
            if s["resource_id"] == resource_id and s["commit"] == revision
        ),
        None,
    )
    if snapshot is None:
        return None
    ref = snapshot["artifact"]
    fixed = kernel.inspect(Query(wi, view="artifact", artifact_ref=ref), handle)
    if fixed["status"] == "rejected":
        raise Rejected(fixed["error"]["code"], fixed["error"]["reason"])
    metadata = {k: v for k, v in snapshot.items() if k != "snapshot_id"}
    artifact = ArtifactInput(
        ref["object"],
        ref["version"],
        base64.b64decode(fixed["result"]["artifact"]["base64"], validate=True),
        ref["access"],
    )
    return metadata, artifact


def capture(
    resource: Json, context: HostContext, revision: str
) -> tuple[Json, ArtifactInput]:
    commit(revision)
    bindings = [
        asdict(r) for r in context.repositories if r.repo_id == resource["repo_id"]
    ]
    if len(bindings) != 1:
        raise Rejected("authority_denied", "Native repository binding unavailable")
    from .existing_execution import retained_binding
    resource = {**resource, "binding": retained_binding(resource, bindings[0])}
    git = GitWorkspaces()
    source, root, target = git.layout(resource)
    with git.locked(root):
        git.owned(source, root, target, resource)
        git.clean(target, include_ignored=False)
        if git.git(target, "rev-parse", "HEAD").decode().strip() != revision:
            raise Rejected(
                "source_invalid",
                "Requested source is not the actual clean workspace HEAD",
            )
        git.git(
            target, "merge-base", "--is-ancestor", resource["base_commit"], revision
        )
        # Freeze only history not already available from the admitted remote.
        # The prerequisite is immutable source evidence, never a moving ref.
        prerequisite = None
        if resource["binding"]["remote_url"] is not None:
            refs = git.git(target, "for-each-ref", "--format=%(objectname)",
                           "refs/remotes/origin/main").decode().splitlines()
            if len(refs) == 1:
                prerequisite = git.git(target, "merge-base", revision, refs[0]).decode().strip()
                if prerequisite == revision:
                    parents = git.git(target, "rev-list", "--parents", "-n", "1", revision).decode().split()
                    prerequisite = parents[1] if len(parents) > 1 else None
        _, directory = git.markers(root, resource)
        suffix = "." + prerequisite if prerequisite else ""
        bundle = directory / (resource["resource_id"] + "." + revision + suffix + ".bundle")
        if bundle.is_symlink():
            raise OSError("Captured source redirected")
        if not bundle.exists():
            # Git writes a complete bundle through its lockfile protocol. HEAD is
            # reread below; a concurrent producer cannot claim another revision.
            exclusions = ["^" + prerequisite] if prerequisite else []
            git.git(target, "bundle", "create", str(bundle), "HEAD", *exclusions)
            with bundle.open("rb") as stream:
                os.fsync(stream.fileno())
        if bundle.stat().st_size > 64 * 1024 * 1024:
            raise Rejected(
                "source_invalid", "Source bundle exceeds the bounded capture limit"
            )
        if (
            git.git(target, "bundle", "list-heads", str(bundle)).decode().strip()
            != revision + " HEAD"
        ):
            raise Rejected("source_invalid", "Captured Git bundle has another HEAD")
        git.git(target, "bundle", "verify", str(bundle))
        git.clean(target, include_ignored=False)
        if git.git(target, "rev-parse", "HEAD").decode().strip() != revision:
            raise Rejected("source_invalid", "Workspace moved during capture")
        artifact = ArtifactInput(
            "repo/source/" + resource["repo_id"], revision, bundle.read_bytes()
        )
        metadata = {
            "resource_id": resource["resource_id"],
            "repo_id": resource["repo_id"],
            "base_commit": resource["base_commit"],
            "commit": revision,
            "tree": git.git(target, "rev-parse", revision + "^{tree}").decode().strip(),
            "artifact": artifact.reference(),
        }
        if prerequisite:
            metadata["bundle_prerequisite"] = prerequisite
        git.fixed(
            Path(str(bundle) + ".json"),
            {**metadata, "bundle_sha256": hashlib.sha256(artifact.content).hexdigest()},
        )
        return metadata, artifact
