"""Git resource adapter. Original intentions bind paths; caches never prove ownership."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Iterator, cast

from .repository_identity import remote_identity, repository_values
from .model import Json, encoded
from .workspaces import sparse_paths, worktree_admin



# Porcelain status lines kept in an observation; the rest stays in the tree.
PRESERVED_PATHS_SHOWN = 5


class WorkspacePreserved(OSError):
    """The workspace holds results that are not disposable; nothing was changed."""

    def __init__(self, cause: str, paths: list[str]) -> None:
        super().__init__(f"Workspace is preserved: {cause}")
        self.cause = cause
        self.paths = paths

class GitWorkspaces:
    def __init__(
        self, *, fault: Callable[[str], None] | None = None, git_config_file: str | None = None
    ):
        self.git_config_file = git_config_file
        self.fault = fault or (lambda stage: None)

    def git(self, path: Path, *args: str, stdin: bytes | None = None) -> bytes:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        if self.git_config_file:
            environment["GIT_CONFIG_GLOBAL"] = self.git_config_file
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "submodule.recurse=false",
                    "-C",
                    str(path),
                    *args,
                ],
                input=stdin,
                capture_output=True,
                env=environment,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError("Git resource operation timed out") from exc
        if result.returncode:
            raise OSError("Git resource operation refused")
        return result.stdout

    @staticmethod
    def real(path: Path) -> Path:
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise OSError("Physical repository binding redirected or missing")
        return path

    def layout(self, resource: Json) -> tuple[Path, Path, Path]:
        source = self.repository(resource["binding"])
        root = self.real(Path(resource["binding"]["managed_root"]))
        return source, root, self.bound_target(root, resource)

    def repository(self, binding: Json) -> Path:
        """The bound source repository, with its recorded Git and remote identity."""
        source = self.real(Path(binding["source"]))
        common = Path(
            self.git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode()
            .strip()
        )
        if common.resolve() != Path(binding["common_dir"]):
            raise OSError("Repository identity changed")
        if binding["remote_url"] is not None:
            remote = self.git(source, "remote", "get-url", "origin").decode().strip()
            if remote_identity(remote) != remote_identity(binding["remote_url"]):
                raise OSError("Repository remote identity changed")
        return source

    @staticmethod
    def bound_target(root: Path, resource: Json) -> Path:
        relative = Path(resource["target"])
        if relative.is_absolute() or any(x in {"", ".", ".."} for x in relative.parts):
            raise OSError("Invalid bound workspace target")
        target = root / relative
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise OSError("Workspace path redirected")
        return target

    def already_absent(self, resource: Json) -> tuple[str, Json] | None:
        """Retirement of a workspace that no longer exists anywhere.

        Ownership proof exists to authorize removing something. When the bound
        repository (same Git and remote identity) has neither the worktree
        registration nor the branch, and nothing is at the path, nothing is left
        to remove or protect, even if the managed root and its markers are gone.
        """
        binding = resource["binding"]
        source = self.repository(binding)
        root = Path(binding["managed_root"])
        if not root.is_absolute() or root.is_symlink() or (root.exists() and root.resolve() != root):
            return None
        target = self.bound_target(root, resource)
        physical = self.physical(source, target, resource)
        if physical["exists"] or physical["branches"] or physical["registrations"]:
            return None
        return "success", {
            "reason": "physical_resources_already_absent",
            "resource_id": resource["resource_id"],
            "path_exists": False,
            "branch_count": 0,
            "registration_count": 0,
        }

    @contextmanager
    def locked(self, root: Path, *, wait: bool = True) -> Iterator[None]:
        """The root's writer lock; without `wait`, BlockingIOError if a writer holds it."""
        lock = root / ".harness-resource-lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def withdrawn_marker(directory: Path, effect: Json) -> Path:
        return directory / (str(effect["effect_id"]) + ".withdrawn.json")

    @staticmethod
    def fixed(path: Path, data: Json) -> None:
        content = encoded(data)
        if path.is_symlink():
            raise OSError("Original resource intent redirected")
        try:
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except FileExistsError:
            if path.read_bytes() != content:
                raise OSError("Original physical intent differs") from None

    def markers(self, root: Path, resource: Json) -> tuple[Path, Path]:
        directory = root / ".harness-resource-evidence"
        if directory.is_symlink():
            raise OSError("Resource evidence redirected")
        directory.mkdir(mode=0o700, exist_ok=True)
        return (directory / (resource["resource_id"] + ".json"), directory)

    @staticmethod
    def immutable(resource: Json) -> Json:
        return cast(
            Json,
            repository_values(
                {
                    k: resource[k]
                    for k in (
                        "resource_id",
                        "repo_id",
                        "base_commit",
                        "binding",
                        "target",
                        "branch",
                        "allocation",
                    )
                }
            ),
        )

    def resource_marker(self, marker: Path, resource: Json) -> None:
        expected = self.immutable(resource)
        if marker.is_symlink():
            raise OSError("Original resource marker redirected")
        if marker.exists():
            if repository_values(json.loads(marker.read_bytes())) != expected:
                raise OSError("Original resource identity differs")
            return
        self.fixed(marker, expected)

    def registrations(self, source: Path) -> list[Json]:
        output = self.git(source, "worktree", "list", "--porcelain", "-z")
        result: list[Json] = []
        row: Json = {}
        for item in output.decode().split("\0"):
            if not item:
                if row:
                    result.append(row)
                    row = {}
            else:
                key, _, value = item.partition(" ")
                row[key] = value
        if row:
            result.append(row)
        return result

    def published(self, source: Path, resource: Json, head: str) -> bool:
        """The bound remote's default branch, as last fetched, holds head."""
        if resource["binding"]["remote_url"] is None:
            return False
        try:
            default = self.git(
                source, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"
            ).decode().strip()
        except OSError:
            default = "refs/remotes/origin/main"
        if not default.startswith("refs/remotes/origin/"):
            return False
        try:
            self.git(source, "merge-base", "--is-ancestor", head, default)
        except OSError:
            return False
        return True

    def physical(self, source: Path, target: Path, resource: Json) -> Json:
        refs = (
            self.git(
                source,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/heads/" + resource["branch"],
            )
            .decode()
            .splitlines()
        )
        refname = "refs/heads/" + resource["branch"]
        refs = [line.split(" ", 1)[1] for line in refs if line.split(" ", 1)[0] == refname]
        registrations = [
            row
            for row in self.registrations(source)
            if row.get("worktree") == str(target) or row.get("branch") == refname
        ]
        return {
            "exists": target.exists(),
            "branches": refs,
            "registrations": registrations,
        }

    def owned(self, source: Path, root: Path, target: Path, resource: Json) -> Json:
        marker = root / ".harness-resource-evidence" / (resource["resource_id"] + ".json")
        if not resource.get("existing_allocation") and (
            marker.is_symlink()
            or not marker.is_file()
            or repository_values(json.loads(marker.read_bytes())) != self.immutable(resource)
        ):
            raise OSError("No matching original physical allocation")
        physical = self.physical(source, target, resource)
        if (
            not physical["exists"]
            or len(physical["branches"]) != 1
            or len(physical["registrations"]) != 1
        ):
            raise OSError("Physical workspace ownership is incomplete")
        registration = physical["registrations"][0]
        if (
            registration.get("worktree") != str(target)
            or registration.get("branch") != "refs/heads/" + resource["branch"]
            or "locked" in registration
            or "prunable" in registration
        ):
            raise OSError("Workspace registration is occupied or locked")
        actual = (
            self.git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode()
            .strip()
        )
        branch = self.git(target, "symbolic-ref", "HEAD").decode().strip()
        if (
            Path(actual).resolve() != Path(resource["binding"]["common_dir"])
            or branch != registration["branch"]
        ):
            raise OSError("Workspace Git identity differs")
        return physical

    def clean(self, target: Path, *, include_ignored: bool = True) -> None:
        status = self.git(
            target,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            *(["--ignored=matching"] if include_ignored else []),
        )
        if status:
            raise WorkspacePreserved(
                "preserved_changes_or_ignored_files",
                status.decode(errors="replace").splitlines()[:PRESERVED_PATHS_SHOWN],
            )
        # Embedded repositories and submodule checkouts are not disposable results.
        if self.git(target, "ls-files", "--stage").startswith(
            b"160000 "
        ) or b"\n160000 " in self.git(target, "ls-files", "--stage"):
            raise WorkspacePreserved("submodule_checkout", [])

    def configure(self, target: Path, sparse: list[str]) -> None:
        sparse_paths(sparse)
        if sparse:
            self.git(
                target,
                "sparse-checkout",
                "set",
                "--no-cone",
                "--stdin",
                stdin="".join("/" + p + "\n" for p in sparse).encode(),
            )
        else:
            self.git(target, "sparse-checkout", "disable")
            self.git(target, "read-tree", "-mu", "HEAD")

    def sparse_matches(self, target: Path, sparse: list[str]) -> bool:
        if not sparse:
            result = (
                self.git(target, "config", "--get-regexp", "core.sparseCheckout|core.bare")
                .decode()
                .lower()
            )
            return "core.sparsecheckout true" not in result
        actual = self.git(target, "sparse-checkout", "list").decode().splitlines()
        return actual == ["/" + path for path in sparse]

    def apply(self, effect: Json, content: bytes) -> tuple[str, Json]:
        if content != encoded(effect["snapshot"]):
            return "failure", {"reason": "resource_input_mismatch"}
        return self.run(effect, initiate=True)

    def readback(self, effect: Json) -> tuple[str, Json]:
        # Existing durable physical markers allow idempotent continuation of the
        # original operation, under its already journaled execution attempt.
        return self.run(effect, initiate=False)

    def probe(self, effect: Json) -> tuple[str, Json]:
        """Readback of an obsolete intention's original attempt, never resuming it.

        Fenced against the original writer: it runs only while holding the
        root's writer lock, which every attempt holds for its whole action; a
        held lock reads as unknown. Revision writes its fsynced action marker
        before changing anything (before it, it only checks ownership and
        cleanliness); allocation writes its resource marker before `worktree
        add`. Without that marker, under the lock, the attempt has not acted.
        The probe then writes a withdrawal marker, still under the lock, which
        every later attempt checks under the same lock before acting, so the
        absence it reports stays true. Nothing else is created or repaired.
        A retirement is only read; it is never withdrawn.
        """
        resource = effect["snapshot"]
        reason = {"resource_id": resource["resource_id"], "obsolete_intention": True}
        kind = effect["effect_kind"]
        try:
            source, root, target = self.layout(resource)
            with self.locked(root, wait=False):
                directory = root / ".harness-resource-evidence"
                marker = directory / (resource["resource_id"] + ".json")
                action = directory / (effect["effect_id"] + ".action.json")
                withdrawn = self.withdrawn_marker(directory, effect)
                if any(p.is_symlink() for p in (directory, marker, action, withdrawn)):
                    raise OSError("Resource evidence redirected")
                physical = self.physical(source, target, resource)
                trace = physical["exists"] or physical["branches"] or physical["registrations"]
                if kind == "workspace_retire":
                    if not action.exists():
                        return "absence", {**reason, "reason": "no_original_action_marker"}
                    if not trace:
                        return "success", {**reason, "reason": "physical_resources_retired"}
                    return "unknown", {**reason, "reason": "obsolete_retirement_incomplete"}
                if kind == "workspace_allocate" and not marker.exists():
                    if trace:
                        return "unknown", {**reason, "reason": "unowned_workspace_or_branch_preserved"}
                    absent = "no_original_physical_intention"
                elif not action.exists():
                    if kind == "workspace_allocate":
                        # `worktree add` may have run after the resource marker.
                        return "unknown", {**reason, "reason": "obsolete_action_incomplete"}
                    absent = "no_original_action_marker"
                else:
                    self.owned(source, root, target, resource)
                    if self.sparse_matches(target, resource["sparse"]):
                        return "success", {**reason, "reason": "physical_workspace_observed"}
                    return "unknown", {**reason, "reason": "obsolete_action_incomplete"}
                directory.mkdir(mode=0o700, exist_ok=True)
                self.fixed(withdrawn, {"effect_id": effect["effect_id"]})
                return "absence", {**reason, "reason": absent}
        except BlockingIOError:
            return "unknown", {**reason, "reason": "original_writer_may_be_running"}
        except (OSError, ValueError, KeyError):
            return "unknown", {**reason, "reason": "physical_resource_conflict_or_unavailable"}

    def run(self, effect: Json, *, initiate: bool) -> tuple[str, Json]:
        resource = effect["snapshot"]
        try:
            source, root, target = self.layout(resource)
            with self.locked(root):
                return self.reconcile(effect, source, root, target, initiate=initiate)
        except (OSError, ValueError, KeyError) as error:
            if effect["effect_kind"] == "workspace_retire":
                try:
                    absent = self.already_absent(resource)
                except (OSError, ValueError, KeyError):
                    absent = None
                if absent is not None:
                    return absent
            details = {
                "reason": "physical_resource_conflict_or_unavailable",
                "resource_id": resource["resource_id"],
            }
            if isinstance(error, WorkspacePreserved):
                # Name what the owner must clear before a retry; the outcome
                # stays unknown so the effect remains retryable.
                details.update(cause=error.cause, paths=error.paths)
            return "unknown", details

    def reconcile(
        self, effect: Json, source: Path, root: Path, target: Path, *, initiate: bool
    ) -> tuple[str, Json]:
        resource = effect["snapshot"]
        marker, directory = self.markers(root, resource)
        withdrawn = self.withdrawn_marker(directory, effect)
        if withdrawn.is_symlink() or withdrawn.exists():
            # A fenced probe proved this obsolete attempt had not acted; it never may.
            return "absence", {"reason": "withdrawn_intention_fenced"}
        physical = self.physical(source, target, resource)
        kind = effect["effect_kind"]
        action_marker = directory / (effect["effect_id"] + ".action.json")
        if kind == "workspace_allocate":
            if not marker.exists():
                if physical["exists"] or physical["branches"] or physical["registrations"]:
                    return "failure", {"reason": "unowned_workspace_or_branch_preserved"}
                if not initiate:
                    return "absence", {"reason": "no_original_physical_intention"}
                if self.git(source, "cat-file", "-t", resource["base_commit"]).strip() != b"commit":
                    return "failure", {"reason": "base_is_not_commit"}
                self.resource_marker(marker, resource)
            self.resource_marker(marker, resource)
            if (
                not physical["exists"]
                and not physical["branches"]
                and not physical["registrations"]
            ):
                target.parent.mkdir(mode=0o700, exist_ok=True)
                self.real(target.parent)
                self.git(
                    source,
                    "worktree",
                    "add",
                    "--no-checkout",
                    "-b",
                    resource["branch"],
                    str(target),
                    resource["base_commit"],
                )
                self.fault("after_worktree_add")
            self.owned(source, root, target, resource)
            if not action_marker.exists():
                # This allocation has not exposed a ready workspace yet. A
                # changed HEAD or untracked file after a crash is preserved.
                if (
                    self.git(target, "rev-parse", "HEAD").decode().strip()
                    != resource["base_commit"]
                ):
                    return "failure", {"reason": "incomplete_allocation_changed"}
                if self.git(target, "ls-files", "--others", "--exclude-standard") or self.git(
                    target, "ls-files", "--others", "--ignored", "--exclude-standard"
                ):
                    return "failure", {"reason": "unowned_files_in_incomplete_allocation"}
                fresh_index = not self.git(target, "ls-files")
                if not fresh_index:
                    self.clean(target)
                self.configure(target, resource["sparse"])
                if fresh_index:
                    self.git(target, "read-tree", "-mu", "HEAD")
                self.fault("after_sparse_checkout")
                self.fixed(
                    action_marker,
                    {"effect_id": effect["effect_id"], "sparse": resource["sparse"]},
                )
        elif kind == "workspace_revise":
            self.owned(source, root, target, resource)
            if not action_marker.exists() and not initiate:
                return "absence", {"reason": "no_original_revision_intention"}
            self.clean(target)
            self.fault("before_action_marker")
            self.fixed(
                action_marker,
                {"effect_id": effect["effect_id"], "sparse": resource["sparse"]},
            )
            self.configure(target, resource["sparse"])
            self.fault("after_sparse_checkout")
        elif kind == "workspace_retire":
            retire_commit = resource.get("retire_commit", resource["base_commit"])
            if retire_commit != resource["base_commit"]:
                preserved = resource.get("preserved_source")
                if (
                    not preserved
                    or not resource.get("publication_effect")
                    or not resource.get("adoption_effects")
                ):
                    return "failure", {"reason": "published_source_proof_missing"}
                prerequisite = preserved.get("bundle_prerequisite")
                suffix = "." + prerequisite if prerequisite else ""
                bundle = directory / (resource["resource_id"] + "." + retire_commit + suffix + ".bundle")
                if bundle.is_symlink() or not bundle.is_file():
                    return "failure", {"reason": "unique_source_bundle_missing"}
                content = bundle.read_bytes()
                if (
                    len(content) != preserved["artifact"]["size"]
                    or hashlib.sha256(content).hexdigest() != preserved["artifact"]["sha256"]
                ):
                    return "failure", {"reason": "unique_source_bundle_changed"}
                self.git(source, "bundle", "verify", str(bundle))
            elif (
                len(physical["branches"]) == 1
                and physical["branches"][0] != retire_commit
                and self.published(source, resource, physical["branches"][0])
            ):
                # Committed and pushed without the core's publication (for
                # example a lightweight item): the remote already keeps them.
                retire_commit = physical["branches"][0]
            if not marker.exists():
                if not (physical["exists"] or physical["branches"] or physical["registrations"]):
                    return self.already_absent(resource) or (
                        "unknown", {"reason": "original_ownership_proof_missing"})
                return "unknown", {"reason": "original_ownership_proof_missing"}
            self.resource_marker(marker, resource)
            if not action_marker.exists():
                if not initiate:
                    return "absence", {"reason": "no_original_retirement_intention"}
                # A worktree removed outside the core leaves no checkout whose
                # changes need protecting; its branch, if any, is still removed
                # only when it holds nothing beyond the retire commit.
                if physical["exists"] or physical["registrations"]:
                    self.owned(source, root, target, resource)
                    self.clean(target)
                if physical["branches"] and physical["branches"] != [retire_commit]:
                    return "failure", {"reason": "unique_commits_preserved"}
                self.fixed(
                    action_marker,
                    {
                        "effect_id": effect["effect_id"],
                        "base_commit": retire_commit,
                    },
                )
            else:
                self.fixed(
                    action_marker,
                    {
                        "effect_id": effect["effect_id"],
                        "base_commit": retire_commit,
                    },
                )
            if physical["exists"] or physical["registrations"]:
                self.owned(source, root, target, resource)
                self.clean(target)
                if physical["branches"] != [retire_commit]:
                    return "failure", {"reason": "unique_commits_preserved"}
                self.git(source, "worktree", "remove", str(target))
                self.fault("after_worktree_remove")
            physical = self.physical(source, target, resource)
            if physical["exists"] or physical["registrations"]:
                return "unknown", {"reason": "worktree_removal_not_observed"}
            if physical["branches"]:
                if physical["branches"] != [retire_commit]:
                    return "failure", {"reason": "unique_commits_preserved"}
                self.git(
                    source,
                    "update-ref",
                    "-d",
                    "refs/heads/" + resource["branch"],
                    retire_commit,
                )
                self.fault("after_branch_remove")
            observed = self.physical(source, target, resource)
            if observed["exists"] or observed["branches"] or observed["registrations"]:
                return "unknown", {"reason": "retirement_not_observed"}
            if target.parent != root:
                try:
                    target.parent.rmdir()  # The WI's directory, once its last workspace is gone.
                except OSError:
                    pass
            return "success", {
                "reason": "physical_resources_retired",
                "resource_id": resource["resource_id"],
                "path_exists": False,
                "branch_count": 0,
                "registration_count": 0,
            }
        else:
            return "failure", {"reason": "unsupported_resource_effect"}
        if not self.sparse_matches(target, resource["sparse"]):
            return "unknown", {"reason": "sparse_scope_not_observed"}
        self.owned(source, root, target, resource)
        self.clean(target)
        # Git names the administrative directory, adding a number on a name
        # clash; the observation records the one proven to belong to this
        # worktree, which session write access later uses and proves again.
        admin = worktree_admin(
            str(target),
            resource["binding"]["common_dir"],
            resource["branch"],
            self.git(target, "rev-parse", "--absolute-git-dir").decode().strip(),
        )
        return "success", {
            "reason": "physical_workspace_observed",
            "resource_id": resource["resource_id"],
            "head": self.git(target, "rev-parse", "HEAD").decode().strip(),
            "path_exists": True,
            "branch_count": 1,
            "registration_count": 1,
            "sparse": resource["sparse"],
            **({"git_dir": admin} if admin else {}),
        }
