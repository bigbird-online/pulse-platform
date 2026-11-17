"""Durable App Server epoch lifecycle observer for ComputeCost v2."""

from __future__ import annotations

from typing import Any

from .app_server_usage import (
    APP_SERVER_FACT_SCHEMA,
    AppServerTokenUpdate,
    AppServerUsageError,
    AppServerUsageLedger,
    EpochUsageContext,
    LedgerAppendResult,
    UsageWatermark,
    _completed_refs,
    _fact_id,
    _nonempty,
    _opaque_ref,
    _timestamp,
    _with_content_digest,
)


class AppServerEpochObserver:
    """Durable epoch state machine driven only by host notifications."""

    def __init__(self, ledger: AppServerUsageLedger, context: EpochUsageContext):
        self.ledger = ledger
        self.context = context

    def _facts(self) -> list[dict[str, Any]]:
        return [
            fact
            for fact in self.ledger.read()
            if fact["context"]["context_ref"] == self.context.context_ref
        ]

    def _baseline(self) -> dict[str, Any] | None:
        matches = [fact for fact in self._facts() if fact["kind"] == "bind_baseline"]
        if len(matches) > 1:
            raise AppServerUsageError("epoch has multiple bind baselines")
        return matches[0] if matches else None

    def _latest_usage(self) -> dict[str, Any] | None:
        facts = [
            fact
            for fact in self._facts()
            if fact["kind"] in {"bind_baseline", "turn_usage"}
        ]
        return facts[-1] if facts else None

    def _completed_turns(self) -> set[str]:
        return {
            fact["turn_ref"]
            for fact in self._facts()
            if fact["kind"] == "turn_completed"
        }

    def _append_idempotent(self, fact: dict[str, Any]) -> LedgerAppendResult:
        for prior in self.ledger.read():
            if prior["fact_id"] != fact["fact_id"]:
                continue
            transient = {"observed_at", "content_sha256"}
            prior_semantic = {
                key: value for key, value in prior.items() if key not in transient
            }
            fact_semantic = {
                key: value for key, value in fact.items() if key not in transient
            }
            if prior_semantic != fact_semantic:
                raise AppServerUsageError("fact_id conflicts with immutable evidence")
            return LedgerAppendResult(False, True, prior)
        return self.ledger.append(fact)

    def _make_fact(
        self,
        *,
        kind: str,
        observed_at: str,
        update: AppServerTokenUpdate,
        step_delta: UsageWatermark,
        epoch_delta: UsageWatermark,
        identity: object,
    ) -> dict[str, Any]:
        return _with_content_digest({
            "schema": APP_SERVER_FACT_SCHEMA,
            "fact_id": _fact_id(kind, self.context.context_ref, identity),
            "kind": kind,
            "observed_at": _timestamp(observed_at),
            "context": self.context.to_dict(),
            "thread_ref": update.thread_ref,
            "turn_ref": update.turn_ref,
            "last": update.last.to_dict(),
            "cumulative": update.total.to_dict(),
            "step_delta": step_delta.to_dict(),
            "epoch_delta": epoch_delta.to_dict(),
            "model_context_window": update.model_context_window,
            "coverage": "exact_app_server_notification",
            "quality": "validated",
            "producer": "openai_app_server",
        })

    def bind(
        self, notification: object, *, observed_at: str
    ) -> LedgerAppendResult:
        update = AppServerTokenUpdate.from_notification(notification)
        prior = self._baseline()
        zero = update.total.delta_from(update.total)
        fact = self._make_fact(
            kind="bind_baseline",
            observed_at=observed_at,
            update=update,
            step_delta=zero,
            epoch_delta=zero,
            identity={"thread_ref": update.thread_ref, "cumulative": update.total.to_dict()},
        )
        if prior is not None and prior["fact_id"] != fact["fact_id"]:
            raise AppServerUsageError("epoch baseline is already bound to other evidence")
        return self._append_idempotent(fact)

    def observe(
        self, notification: object, *, observed_at: str
    ) -> LedgerAppendResult:
        update = AppServerTokenUpdate.from_notification(notification)
        baseline = self._baseline()
        latest = self._latest_usage()
        if baseline is None or latest is None:
            raise AppServerUsageError("epoch must bind a baseline before usage updates")
        if update.thread_ref != baseline["thread_ref"]:
            raise AppServerUsageError("usage update belongs to another thread")
        baseline_total = UsageWatermark.from_dict(
            baseline["cumulative"], "baseline.cumulative"
        )
        latest_total = UsageWatermark.from_dict(latest["cumulative"], "latest.cumulative")
        if (
            latest["kind"] == "turn_usage"
            and latest["turn_ref"] == update.turn_ref
            and latest_total == update.total
            and UsageWatermark.from_dict(latest["last"], "latest.last") == update.last
        ):
            return LedgerAppendResult(False, True, latest)
        step_delta = update.total.delta_from(latest_total)
        epoch_delta = update.total.delta_from(baseline_total)
        if latest_total.plus(update.last) != update.total:
            raise AppServerUsageError(
                "provider last usage does not reconcile with cumulative total"
            )
        fact = self._make_fact(
            kind="turn_usage",
            observed_at=observed_at,
            update=update,
            step_delta=step_delta,
            epoch_delta=epoch_delta,
            identity={
                "thread_ref": update.thread_ref,
                "turn_ref": update.turn_ref,
                "cumulative": update.total.to_dict(),
            },
        )
        return self._append_idempotent(fact)

    def complete_turn(
        self, notification: object, *, observed_at: str
    ) -> LedgerAppendResult:
        thread_ref, turn_ref = _completed_refs(notification)
        baseline = self._baseline()
        latest = self._latest_usage()
        if baseline is None or latest is None:
            raise AppServerUsageError("epoch must bind before turn completion")
        if thread_ref != baseline["thread_ref"]:
            raise AppServerUsageError("completed turn belongs to another thread")
        identity = {"thread_ref": thread_ref, "turn_ref": turn_ref}
        fact = _with_content_digest({
            **latest,
            "fact_id": _fact_id("turn_completed", self.context.context_ref, identity),
            "kind": "turn_completed",
            "observed_at": _timestamp(observed_at),
            "thread_ref": thread_ref,
            "turn_ref": turn_ref,
        })
        return self._append_idempotent(fact)

    def seal_source(
        self, *, operation_ref: str, observed_at: str
    ) -> LedgerAppendResult:
        operation = _nonempty(operation_ref, "operation_ref")
        baseline = self._baseline()
        latest = self._latest_usage()
        if baseline is None or latest is None:
            raise AppServerUsageError("epoch cannot seal without a baseline")
        if latest["turn_ref"] not in self._completed_turns():
            raise AppServerUsageError(
                "source seal requires final usage for a completed turn"
            )
        identity = {"operation_ref": operation}
        fact = _with_content_digest({
            **latest,
            "fact_id": _fact_id("source_seal", self.context.context_ref, identity),
            "kind": "source_seal",
            "observed_at": _timestamp(observed_at),
            "operation_ref": _opaque_ref("operation", operation),
        })
        return self._append_idempotent(fact)
