"""Exact non-forced Git publication and clean fast-forward consumer adoption."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable

from .git_workspaces import GitWorkspaces
from .repository_identity import remote_identity
from .model import Json, encoded


class GitPublication:
    def __init__(
        self,
        root: Path,
        *,
        fault: Callable[[str], None] | None = None,
        git_config_file: str | None = None,
    ):
        self.root = root
        self.git = GitWorkspaces(git_config_file=git_config_file)
        self.fault = fault or (lambda stage: None)

    def location(self, effect: Json, *, create: bool = False) -> Path:
        self.git.real(self.root)
        path = self.root / str(effect["effect_id"])
        if path.is_symlink():
            raise OSError("Publication evidence redirected")
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        return path

    def remote_head(self, repository: Path, effect: Json) -> str:
        target = effect["snapshot"]["target"]
        rows = (
            self.git.git(
                repository,
                "ls-remote",
                "--refs",
                "--",
                remote_identity(target["remote_url"]),
                target["ref"],
            )
            .decode()
            .splitlines()
        )
        exact = [
            r.split()[0] for r in rows if len(r.split()) == 2 and r.split()[1] == target["ref"]
        ]
        if len(exact) != 1:
            raise OSError("Original source ref unavailable")
        return exact[0]

    def apply(self, effect: Json, content: bytes) -> tuple[str, Json]:
        ref = effect["artifact"]
        if len(content) != ref["size"] or hashlib.sha256(content).hexdigest() != ref["sha256"]:
            return "failure", {"reason": "fixed_source_bundle_mismatch"}
        try:
            root = self.location(effect, create=True)
            with self.git.locked(root):
                self.git.fixed(root / "intention.json", effect["snapshot"])
                bundle = root / "source.bundle"
                if bundle.is_symlink():
                    raise OSError("Source bundle redirected")
                if not bundle.exists():
                    with bundle.open("xb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                if bundle.read_bytes() != content:
                    raise OSError("Source bundle changed")
                transport = root / "transport.git"
                if not transport.exists():
                    self.git.git(root, "init", "--bare", str(transport))
                self.git.real(transport)
                source = effect["snapshot"]["source"]
                if (
                    self.git.git(transport, "bundle", "list-heads", str(bundle)).decode().strip()
                    != source["commit"] + " HEAD"
                ):
                    return "failure", {"reason": "fixed_bundle_head_mismatch"}
                prerequisite = source.get("bundle_prerequisite")
                if prerequisite:
                    # Only the registered destination supplies prerequisite
                    # history. Verify reachability before importing frozen bytes.
                    target = effect["snapshot"]["target"]
                    self.git.git(transport, "fetch", "--no-tags",
                                 remote_identity(target["remote_url"]),
                                 target["ref"] + ":refs/harness/source-base")
                    self.git.git(transport, "merge-base", "--is-ancestor",
                                 prerequisite, "refs/harness/source-base")
                self.git.git(
                    transport,
                    "fetch",
                    "--no-tags",
                    str(bundle),
                    "HEAD:refs/heads/captured",
                )
                if (
                    self.git.git(transport, "rev-parse", source["commit"] + "^{tree}")
                    .decode()
                    .strip()
                    != source["tree"]
                ):
                    return "failure", {"reason": "fixed_bundle_tree_mismatch"}
                return self.run(effect, root, transport, initiate=True)
        except (OSError, ValueError, KeyError):
            return "unknown", {"reason": "source_publication_unavailable"}

    def readback(self, effect: Json) -> tuple[str, Json]:
        try:
            root = self.location(effect)
            if not root.exists():
                return "absence", {"reason": "no_original_publication_intention"}
            with self.git.locked(root):
                intention = root / "intention.json"
                if not intention.exists():
                    return "absence", {"reason": "no_original_publication_intention"}
                if intention.is_symlink() or intention.read_bytes() != encoded(effect["snapshot"]):
                    raise OSError("Publication intention mismatch")
                transport = root / "transport.git"
                if not transport.exists():
                    return "absence", {"reason": "no_original_send_intention"}
                self.git.real(transport)
                return self.run(effect, root, transport, initiate=False)
        except (OSError, ValueError, KeyError):
            return "unknown", {"reason": "source_readback_unavailable"}

    def run(self, effect: Json, root: Path, transport: Path, *, initiate: bool) -> tuple[str, Json]:
        snapshot = effect["snapshot"]
        revision = snapshot["source"]["commit"]
        target = snapshot["target"]
        marker = root / "send.json"
        if marker.is_symlink():
            raise OSError("Original send marker redirected")
        if effect["effect_kind"] == "publish_source":
            head = self.remote_head(transport, effect)
            if head == revision:
                return "success", {
                    "reason": "exact_remote_head_observed",
                    "commit": head,
                    "target_id": target["target_id"],
                }
            if marker.exists():
                # Old base after an ambiguous send may be an intentional rollback.
                # It is not permission to repeat the write.
                return "unknown", {
                    "reason": "original_send_not_observed",
                    "observed_head": head,
                }
            if head != snapshot["expected_head"]:
                return "failure", {
                    "reason": "remote_head_conflict",
                    "observed_head": head,
                }
            if not initiate:
                return "absence", {"reason": "no_original_send_intention"}
            self.git.git(transport, "merge-base", "--is-ancestor", head, revision)
            self.git.fixed(marker, {"commit": revision, "expected_head": head})
            self.fault("before_push")
            self.git.git(
                transport,
                "push",
                "--porcelain",
                "--",
                remote_identity(target["remote_url"]),
                revision + ":" + target["ref"],
            )
            self.fault("after_push")
            if self.remote_head(transport, effect) != revision:
                return "unknown", {"reason": "published_head_not_observed"}
            return "success", {
                "reason": "exact_remote_head_observed",
                "commit": revision,
                "target_id": target["target_id"],
            }
        consumer = snapshot["consumer"]
        binding = consumer["binding"]
        path = self.git.real(Path(binding["path"]))
        common = (
            self.git.git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode()
            .strip()
        )
        if (
            Path(common).resolve() != Path(binding["common_dir"])
            or remote_identity(self.git.git(path, "remote", "get-url", "origin").decode().strip())
            != remote_identity(binding["remote_url"])
            or self.git.git(path, "symbolic-ref", "HEAD").decode().strip()
            != "refs/heads/" + binding["branch"]
        ):
            raise OSError("Consumer identity changed")
        self.git.clean(path)
        head = self.git.git(path, "rev-parse", "HEAD").decode().strip()
        if head == revision:
            if (
                self.git.git(path, "rev-parse", "HEAD^{tree}").decode().strip()
                != snapshot["source"]["tree"]
            ):
                raise OSError("Consumer tree differs")
            return "success", {
                "reason": "exact_consumer_checkout_observed",
                "consumer_id": binding["consumer_id"],
                "commit": head,
                "tree": snapshot["source"]["tree"],
                "clean": True,
            }
        if marker.exists():
            return "unknown", {
                "reason": "original_adoption_not_observed",
                "observed_head": head,
            }
        if head != consumer["expected_head"]:
            return "failure", {
                "reason": "consumer_head_conflict",
                "observed_head": head,
            }
        if not initiate:
            return "absence", {"reason": "no_original_adoption_intention"}
        if self.remote_head(transport, effect) != revision:
            return "unknown", {"reason": "published_source_changed_before_adoption"}
        # Fetch only frozen bytes, never whatever happens to be remote HEAD now.
        self.git.git(path, "fetch", "--no-tags", str(transport), "refs/heads/captured")
        self.git.git(path, "merge-base", "--is-ancestor", head, revision)
        self.git.fixed(marker, {"commit": revision, "expected_head": head})
        self.fault("before_adoption")
        self.git.git(path, "merge", "--ff-only", "--no-edit", revision)
        self.fault("after_adoption")
        return self.run(effect, root, transport, initiate=False)
