"""Typed boundary values. Host authentication is supplied by the trusted embedding host."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from .repository_identity import remote_identity
from typing import Any, Callable, Mapping, cast

Json = dict[str, Any]
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")


class Rejected(Exception):
    def __init__(self, code: str, reason: str):
        self.code, self.reason = code, reason
        super().__init__(reason)


def encoded(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise Rejected("invalid_input", "Input must be finite UTF-8 JSON") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def operation_id(namespace: str, request_id: str) -> str:
    """Public identity of the operation (and its work record) one request commits."""
    return "op_" + digest([namespace, request_id])


def writer_identity(program: str, policy: Json) -> str:
    """Public admission identity for one exact program and immutable policy pair."""
    return "writer.v2." + digest({"program": program, "policy": digest(policy)})


def identity(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise Rejected("invalid_input", "Invalid opaque identifier")
    return value


def deadline(value: object) -> None:
    """Validate an explicitly recorded business instant; never choose a default."""
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
            value,
        )
        is None
    ):
        raise Rejected("invalid_input", "Deadline requires an explicit RFC3339 instant")
    if value.endswith("-00:00"):
        raise Rejected("invalid_input", "Deadline timezone offset is unknown")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise Rejected("invalid_input", "Deadline is not a valid date-time") from None


def fields(value: Json, required: set[str], optional: set[str] | None = None) -> None:
    # Name the fields: a caller cannot correct an input it cannot see is wrong.
    if not isinstance(value, dict):
        raise Rejected("invalid_input", "Business input must be an object with: " + ", ".join(sorted(required)))
    if "human_input_ref" in value.keys() - required - (optional or set()):
        raise Rejected(
            "invalid_input", "human_input_ref goes at the top level of execute, not in input"
        )
    if missing := required - value.keys():
        raise Rejected("invalid_input", "Required business input is missing: " + ", ".join(sorted(missing)))
    if extra := value.keys() - required - (optional or set()):
        raise Rejected("invalid_input", "Unsupported business input field: " + ", ".join(sorted(extra)))


@dataclass(frozen=True)
class SourceInput:
    """Exact bytes already authenticated by a Host adapter, never a boolean approval."""

    reference: str
    kind: str
    content: bytes
    # Human approval, when needed, is bound by the adapter to this exact business input.
    approved_intent: str | None = None
    approved_input_digest: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class ArtifactInput:
    """A fixed artifact captured by the trusted Host, stored with the accepting request."""

    object: str
    version: str
    content: bytes
    access: str = "team"

    def reference(self) -> Json:
        return {
            "object": self.object,
            "version": self.version,
            "sha256": hashlib.sha256(self.content).hexdigest(),
            "size": len(self.content),
            "access": self.access,
        }


@dataclass(frozen=True)
class RepositoryBinding:
    """Operator-admitted physical identity; never supplied by a business command."""

    repo_id: str
    source: str
    common_dir: str
    managed_root: str
    remote_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "remote_url", remote_identity(self.remote_url))


@dataclass(frozen=True)
class PublicationTarget:
    target_id: str
    repo_id: str
    remote_url: str
    ref: str = "refs/heads/main"

    def __post_init__(self) -> None:
        object.__setattr__(self, "remote_url", remote_identity(self.remote_url))


@dataclass(frozen=True)
class SourceConsumer:
    consumer_id: str
    repo_id: str
    path: str
    common_dir: str
    remote_url: str
    branch: str = "main"

    def __post_init__(self) -> None:
        object.__setattr__(self, "remote_url", remote_identity(self.remote_url))


@dataclass(frozen=True)
class NotificationRoute:
    """Trusted single-channel route identity; no business-supplied recipient."""

    route_id: str
    channel: str
    recipient_digest: str
    mode: str = "immediate"
    timezone: str = "UTC"
    audience: str = "operational"
    max_items: int = 16
    max_bytes: int = 4096
    transport_digest: str | None = None


@dataclass(frozen=True)
class RuntimeObservation:
    """Trusted invocation evidence, never accepted from a business command."""

    principal_digest: str
    observed_at: str
    presence: str
    reason: str
    source: str
    proof_digest: str | None = None


@dataclass(frozen=True)
class HostContext:
    """Trusted adapter result, not a deserializable CLI authorization request.

    Kernel calls its mandatory authenticate callback with the opaque caller handle.
    The callback must validate native identity, source and access policy before
    constructing this value. The kernel still checks current domain grants.
    """

    namespace: str
    actor: str
    session: str
    surface: str
    generation: int
    readable_wis: frozenset[str]
    allowed_intents: frozenset[str]
    source: SourceInput | None = None
    artifacts: tuple[ArtifactInput, ...] = ()
    effect_kinds: frozenset[str] = frozenset()
    verification_actor: str | None = None
    repositories: tuple[RepositoryBinding, ...] = ()
    publication_targets: tuple[PublicationTarget, ...] = ()
    source_consumers: tuple[SourceConsumer, ...] = ()
    git_source: Json | None = None
    fact_store: Json | None = None
    effect_binding: str | None = None
    usage_snapshot: Json | None = None
    native_cursor: int | None = None
    read_only_reason: str | None = None
    notification_routes: tuple[NotificationRoute, ...] = ()
    observed_revision: int = 0
    observed_work_item: str | None = None
    admission_policy: Json | None = None
    runtime_observations: tuple[RuntimeObservation, ...] = ()
    # Actual Host-controlled session handover, never an execute-envelope field.
    session_handover: Json | None = None
    # Host-attested Agent switch into this WI (`self_switch`), never an envelope field.
    session_switch: Json | None = None
    # How the native session was entered (Relay channel, "scheduler", "cli").
    # It grants nothing; an unattended "scheduler" run is refused main-only work.
    entry: str | None = None
    # Host-observed model and reasoning effort of this session, read from its own
    # native record, and the Relay review profile resolution (PR review gate).
    session_compute: Json | None = None
    review_profile: Json | None = None
    # Operator-admitted repositories whose pull requests the kernel may merge.
    merge_repositories: frozenset[str] = frozenset()
    # Host-read GitHub state of the pull request being registered.
    pull_request_observation: Json | None = None
    # Kernel-read {id, state} of the parent `create_child` names; never Host input.
    related_parent: Json | None = None
    # Kernel-read active WIs each untargeted main-only Agent recipient holds,
    # {actor: [{work_item_id, title}]}; never Host input.
    recipient_work: Json | None = None
    # The request_switch target as the kernel read it in the same transaction.
    switch_target: Json | None = None
    # Operator-installed producer workspaces, {actor: [absolute roots]}: the
    # layout the family read rule checks (access.family); never Host input.
    agent_workspaces: Json | None = None
    # Kernel-read unrevoked standing merge grants of the caller Agent across every
    # WI journal, [{grant_id, work_item_id, source}]; never Host input.
    merge_authority: tuple[Json, ...] = ()

    def can_read(self, wi: str) -> bool:
        return wi in self.readable_wis or "*" in self.readable_wis

    def principal(self) -> Json:
        return {"actor": self.actor, "session": self.session, "surface": self.surface}

    def validate(self) -> None:
        from .notifications import validate_routes

        validate_routes(self.notification_routes)
        if self.read_only_reason is not None:
            identity(self.read_only_reason)
            if self.allowed_intents or self.effect_kinds:
                raise Rejected("authority_denied", "Read-only Host cannot grant execution")
        for item in (self.namespace, self.actor, self.session, self.surface):
            identity(item)
        if type(self.generation) is not int or self.generation < 0:
            raise Rejected("authority_denied", "Invalid host generation")
        if len({r.repo_id for r in self.repositories}) != len(self.repositories):
            raise Rejected("authority_denied", "Repository policy is ambiguous")
        for repository in self.repositories:
            identity(repository.repo_id)
        if len({t.target_id for t in self.publication_targets}) != len(
            self.publication_targets
        ) or len({c.consumer_id for c in self.source_consumers}) != len(self.source_consumers):
            raise Rejected("authority_denied", "Source publication policy is ambiguous")
        for target in self.publication_targets:
            identity(target.target_id)
            identity(target.repo_id)
            if (
                target.ref != "refs/heads/main"
                or not target.remote_url
                or target.remote_url.startswith("-")
            ):
                raise Rejected("authority_denied", "Source publication requires admitted main")
        for consumer in self.source_consumers:
            identity(consumer.consumer_id)
            identity(consumer.repo_id)
            if consumer.branch != "main":
                raise Rejected("authority_denied", "Source consumer requires admitted main")
        if any(
            not isinstance(a, ArtifactInput) or not isinstance(a.content, bytes)
            for a in self.artifacts
        ):
            raise Rejected("source_invalid", "Artifact bytes are unavailable")
        if self.source is not None:
            identity(self.source.reference)
            if not isinstance(self.source.kind, str) or self.source.kind not in {
                "native_human",
                "native_agent",
                "independent_result",
                "native_usage",
            }:
                raise Rejected("source_invalid", "Unsupported authenticated source")
            if not isinstance(self.source.content, bytes) or not self.source.content:
                raise Rejected("source_invalid", "Source bytes are unavailable")


Authenticate = Callable[[object], HostContext]


@dataclass(frozen=True)
class Command:
    request_id: str
    intent: str
    input: Mapping[str, Any]
    work_item_id: str | None = None
    schema: str = "harness.command/v1"

    def normalized(self) -> Json:
        if self.schema != "harness.command/v1":
            raise Rejected("unsupported_schema", "Unsupported command schema")
        identity(self.request_id)
        identity(self.intent)
        if self.work_item_id is not None:
            identity(self.work_item_id)
        if not isinstance(self.input, dict):
            raise Rejected("invalid_input", "Business input must be an object")
        # Detach mutable caller-owned objects before entering the transaction.
        return cast(
            Json,
            json.loads(
                encoded(
                    {
                        "schema": self.schema,
                        "request_id": self.request_id,
                        "intent": self.intent,
                        "input": self.input,
                        "work_item_id": self.work_item_id,
                    }
                )
            ),
        )


@dataclass(frozen=True)
class Query:
    work_item_id: str | None = None
    revision: int | None = None
    view: str = "state"
    after: int = 0
    limit: int = 100
    compare_plan_text: str | None = None
    artifact_ref: Json | None = None
    status: str | None = None
    owner: str | None = None
    text: str | None = None
    age_threshold_seconds: int | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class Decision:
    events: list[Json]
    result: Json = field(default_factory=dict)


def human_decision(context: HostContext, intent: str, data: Json, wi: str) -> str:
    source = context.source
    if (
        source is None
        or source.kind != "native_human"
        or source.approved_intent != intent
        or source.approved_input_digest
        != digest({"intent": intent, "work_item_id": wi, "input": data})
    ):
        raise Rejected(
            "source_invalid",
            "An authenticated decision for this content is required: pass the human's "
            "message as the top-level human_input_ref of execute",
        )
    return source.sha256


def native_session_ref(namespace: str, actor: str, native_id: str, surface: str) -> str:
    return "session." + digest([namespace, actor, native_id, surface])
