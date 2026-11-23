"""Bounded effect execution. The journal owns intent, attempts and all observations."""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime
from pathlib import Path
import stat
import tempfile
from typing import Callable, Protocol, runtime_checkable

from .delivery import OBSOLETE
from .kernel import Kernel
from .migration import observation_blocks_effect
from .model import ArtifactInput, Command, Json, Query, digest, encoded


def admission_fenced(response: Json) -> bool:
    return response["status"] == "rejected" or any(
        capability["reason"]
        in {"inactive_installed_program", "foreign_fact_store", "policy_audit"}
        for capability in response["capabilities"]
    )


class Adapter(Protocol):
    def apply(self, effect: Json, content: bytes) -> tuple[str, Json]: ...
    def readback(self, effect: Json) -> tuple[str, Json]: ...


@runtime_checkable
class Prober(Protocol):
    """An adapter that can read back an obsolete resource intention without resuming it."""

    def probe(self, effect: Json) -> tuple[str, Json]: ...


# A deferral probe reads GitHub (each request bounded by pulse-gh's timeout), so a
# drain runs at most PROBE_LIMIT of them; the rest wait for a later drain. Which
# waiting effects are probed changes every PROBE_WINDOW seconds (a hash of the
# effect and the window), so none is left out for good.
PROBE_LIMIT = 2
PROBE_WINDOW = 60


def exhausted(effect: Json) -> bool:
    """A new attempt is refused until the owner records retry_effect."""
    retry = effect.get("retry") or {}
    return effect["status"] in {"requested", "observed_absence"} and retry.get(
        "attempts_remaining"
    ) == 0


class FileDelivery:
    """Publish exact bytes into an operator-configured root without overwriting files."""

    def __init__(self, root: Path):
        self.root = root.absolute()

    def target(self, logical: str, *, create: bool = False) -> Path:
        from .workflow import resource_paths

        resource_paths([logical])
        if self.root.resolve() != self.root or not self.root.is_dir():
            raise OSError("Delivery root must be an existing real directory")
        target = self.root / logical
        current = self.root
        for component in Path(logical).parts[:-1]:
            current = current / component
            if create:
                current.mkdir(exist_ok=True, mode=0o700)
            if current.is_symlink():
                raise OSError("Delivery directory redirection refused")
        if target.is_symlink():
            raise OSError("Delivery target redirection refused")
        return target

    def readback(self, effect: Json) -> tuple[str, Json]:
        try:
            target = self.target(effect["target"])
            with target.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    return "failure", {"reason": "not_regular_file"}
                content = stream.read()
            sha = hashlib.sha256(content).hexdigest()
            matches = (
                sha == effect["artifact"]["sha256"]
                and len(content) == effect["artifact"]["size"]
            )
            return ("success" if matches else "failure"), {
                "target": effect["target"],
                "sha256": sha,
                "size": len(content),
                "reason": "fixed_bytes_observed" if matches else "target_conflict",
            }
        except FileNotFoundError:
            return "absence", {"target": effect["target"], "reason": "target_absent"}
        except OSError:
            return "unknown", {
                "target": effect["target"],
                "reason": "readback_unavailable",
            }

    def apply(self, effect: Json, content: bytes) -> tuple[str, Json]:
        if (
            hashlib.sha256(content).hexdigest() != effect["artifact"]["sha256"]
            or len(content) != effect["artifact"]["size"]
        ):
            return "failure", {"reason": "fixed_input_mismatch"}
        target = self.target(effect["target"], create=True)
        # A hard-link publication is atomic and refuses any existing target.
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".delivery-", delete=True
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(stream.name, target)
                descriptor = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except FileExistsError:
                pass
        return self.readback(effect)


class Effects:
    def __init__(
        self,
        kernel: Kernel,
        adapter: Adapter,
        host: Callable[[tuple[ArtifactInput, ...]], object],
        *,
        fault: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.kernel, self.adapter, self.host = kernel, adapter, host
        self.fault = fault or (lambda stage: None)
        self.clock = clock or (lambda: datetime.now().timestamp())

    def drain(self, wi: str, *, budget: int = 8, kinds: frozenset[str] | None = None) -> list[Json]:
        if type(budget) is not int or not 1 <= budget <= 64:
            raise ValueError("A bounded effect budget is required")
        inspected = self.kernel.inspect(Query(wi), self.host(()))
        if admission_fenced(inspected):
            return [inspected]
        results: list[Json] = []
        view = inspected["result"]["current_observation"]
        # An optional ambiguous notice must not consume every manual/1 batch.
        # Keep dependency checks and relative order; idle batches still read it.
        effects = sorted(
            inspected["effects"],
            key=lambda item: (
                item["effect_kind"] == "notification_send"
                and not item["required"]
                and item["status"] in {"attempt_started", "unknown", "observed_failure"}
            ),
        )
        # The adapter may know a new attempt would only repeat the last absence
        # (a merge whose checks still run): such an effect waits, writing nothing.
        deferrable = getattr(self.adapter, "deferrable", None)
        # A workspace effect runs only on the host that holds the workspace.
        local: Callable[[Json], bool] = getattr(self.adapter, "local", lambda e: True)
        defer: Callable[[Json], str | None] = getattr(self.adapter, "defer", lambda e: None)
        waiting = {
            e["effect_id"] for e in effects
            if callable(deferrable)
            and (kinds is None or e["effect_kind"] in kinds)
            and not e.get("obsolete") and not exhausted(e) and e.get("startable") is not False
            and not observation_blocks_effect(view, e["effect_id"]) and deferrable(e)
        }
        window = int(self.clock() // PROBE_WINDOW)
        probed = set(sorted(
            waiting, key=lambda i: digest({"effect_id": i, "window": window}))[:PROBE_LIMIT])
        for effect in effects:
            if kinds is not None and effect["effect_kind"] not in kinds:
                continue
            if kinds is None and effect["effect_kind"] == "pr_review_dispatch":
                continue  # Only the Relay review gate executes review requests.
            if not local(effect):
                continue
            # A deferral is a read-only probe, not a step: it never takes the
            # batch from an effect behind it that can run now.
            if sum(r.get("status") != "deferred" for r in results) >= budget:
                break
            if observation_blocks_effect(view, effect["effect_id"]):
                continue
            if effect["status"] in {"observed_success", "cancelled"}:
                continue
            if effect["status"] == "observed_failure" and effect["effect_kind"] in {
                "merge_pull_request", "workspace_retire",
            }:
                # A failed merge needs a new reviewed round; a failed retirement kept the
                # workspace and only a new approved request retires it. Neither is read back.
                continue
            if exhausted(effect) and not effect.get("obsolete"):
                continue  # The kernel would refuse the same request until retry_effect.
            if effect["status"] in {"requested", "observed_absence"} and (
                    effect.get("startable") is False):
                continue  # A merge of ended work or a superseded round: refused for good.
            if effect["status"] in {"requested", "observed_absence"}:
                if effect["effect_id"] in waiting:
                    reason = defer(effect) if effect["effect_id"] in probed else "probe_limit"
                    if reason is not None:
                        results.append({"status": "deferred", "effect_id": effect["effect_id"],
                                        "reason": reason})
                        continue
                request = f"send.{effect['effect_id']}.{len(effect['attempts'])}"
                admitted = self.kernel.execute(
                    Command(
                        request, "begin_effect", {"effect_id": effect["effect_id"]}, wi
                    ),
                    self.host(()),
                )
                if (
                    (admitted.get("error") or {}).get("code") == OBSOLETE
                    and effect["attempts"]
                    and isinstance(self.adapter, Prober)
                ):
                    # An earlier attempt may still act: only the fenced probe settles it.
                    outcome, details = self.adapter.probe(effect)
                    results.append(self._observe(
                        wi, effect, effect["attempts"][-1]["id"], outcome, details))
                    continue
                if admitted["status"] == "rejected":
                    results.append(admitted)
                    continue
                if observation_blocks_effect(
                    admitted["result"]["current_observation"], effect["effect_id"]
                ):
                    results.append(admitted)
                    continue
                if admitted["result"]["original_request_result"].get("suppressed"):
                    results.append(admitted)
                    continue
                attempt = admitted["result"]["original_request_result"]["attempt_id"]
                self.fault("after_attempt")
                from .workspaces import EFFECT_KINDS

                if effect["effect_kind"] in EFFECT_KINDS | {
                    "verification_dispatch",
                    "notification_send",
                    "merge_pull_request",
                }:
                    content = encoded(effect["snapshot"])
                else:
                    fixed = self.kernel.inspect(
                        Query(wi, view="artifact", artifact_ref=effect["artifact"]),
                        self.host(()),
                    )
                    if fixed["status"] == "rejected":
                        results.append(fixed)
                        continue
                    content = base64.b64decode(
                        fixed["result"]["artifact"]["base64"], validate=True
                    )
                latest = self.kernel.inspect(Query(wi), self.host(()))
                if admission_fenced(latest) or observation_blocks_effect(
                    latest["result"]["current_observation"], effect["effect_id"]
                ):
                    results.append(latest)
                    continue
                if effect["effect_kind"] == "notification_send":
                    guarded = self.kernel.execute(
                        Command(
                            "guard."
                            + effect["effect_id"]
                            + "."
                            + str(latest["work_item"]["local_revision"]),
                            "continue_effect",
                            {"effect_id": effect["effect_id"]},
                            wi,
                        ),
                        self.host(()),
                    )
                    if guarded["status"] == "rejected":
                        results.append(guarded)
                        continue
                try:
                    outcome, details = self.adapter.apply(effect, content)
                except OSError:
                    outcome, details = "unknown", {"reason": "send_result_unavailable"}
                self.fault("after_send")
            else:
                attempt = effect["attempts"][-1]["id"]
                from .workspaces import EFFECT_KINDS

                if effect["effect_kind"] in EFFECT_KINDS:
                    admitted = self.kernel.execute(
                        Command(
                            "continue."
                            + effect["effect_id"]
                            + "."
                            + str(inspected["work_item"]["local_revision"]),
                            "continue_effect",
                            {"effect_id": effect["effect_id"]},
                            wi,
                        ),
                        self.host(()),
                    )
                    if (admitted.get("error") or {}).get("code") == OBSOLETE and isinstance(
                        self.adapter, Prober
                    ):
                        # Never resumed: a read-only probe proves whether it acted.
                        # An unchanged probe replays its observation, writing nothing.
                        outcome, details = self.adapter.probe(effect)
                        results.append(self._observe(wi, effect, attempt, outcome, details))
                        continue
                    if admitted["status"] == "rejected":
                        results.append(admitted)
                        continue
                    if observation_blocks_effect(
                        admitted["result"]["current_observation"], effect["effect_id"]
                    ):
                        results.append(admitted)
                        continue
                    # A retirement stays current across handover and completion
                    # (`workspaces.obsolete`); allocation and revision do not.
                    if effect["effect_kind"] != "workspace_retire" and (
                        admitted["work_item"]["generation"] != effect["generation"]
                        or admitted["work_item"]["business_progress"]
                        in {"completed", "terminated"}
                    ):
                        results.append(admitted)
                        continue
                latest = self.kernel.inspect(Query(wi), self.host(()))
                if admission_fenced(latest) or observation_blocks_effect(
                    latest["result"]["current_observation"], effect["effect_id"]
                ):
                    results.append(latest)
                    continue
                outcome, details = self.adapter.readback(effect)
                self.fault("after_readback")
            results.append(self._observe(wi, effect, attempt, outcome, details))
        return results

    def _observe(self, wi: str, effect: Json, attempt: str, outcome: str, details: Json) -> Json:
        observation = {
            "effect_id": effect["effect_id"],
            "attempt_id": attempt,
            "outcome": outcome,
            "details": details,
        }
        proof = ArtifactInput(
            "observations/" + effect["effect_id"],
            digest(observation),
            encoded(observation),
        )
        data = {**observation, "evidence": [proof.reference()]}
        self.fault("before_observation")
        return self.kernel.execute(
            Command("observe." + digest(data), "observe_effect", data, wi),
            self.host((proof,)),
        )
