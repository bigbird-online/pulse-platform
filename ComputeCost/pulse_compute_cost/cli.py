"""Explicit ComputeCost CLI with bounded, versioned materialization."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import pwd
import socket
import sys
import tempfile
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .adapters import AdapterError, collect_usage_events
from .app_server_account import (
    AppServerAccountError,
    probe_app_server_account,
)
from .app_server_observer import AppServerEpochObserver
from .app_server_thread_usage import (
    AppServerThreadUsageError,
    build_app_server_thread_usage_snapshot,
)
from .app_server_usage import (
    AppServerUsageError,
    AppServerUsageLedger,
    EpochUsageContext,
)
from .events import UsageEventError
from .ledger import LedgerError, UsageLedger
from .managed_snapshot import ManagedCostSnapshotError, build_managed_cost_snapshot
from .openrouter import (
    DEFAULT_REFERENCE_MODEL,
    OpenRouterContractError,
    OpenRouterPriceSnapshot,
    fetch_openrouter_activity,
    fetch_openrouter_models,
    load_dotenv_value,
    parse_openrouter_prices,
    summarize_openrouter_activity,
)
from .cc_usage import CC_RUNTIMES, CcUsageBinding, capture_cc_session_snapshot
from .pi_capture import capture_pi_session_snapshot
from .pi_usage import (
    PiUsageBinding,
    PiUsageError,
    build_pi_session_snapshot,
    validate_pi_session_snapshot,
)
from .query import QueryError, QueryWindow, query_ledger
from .collaboration_query import CollaborationQuery, query_collaboration
from .receipt_sources import collect_registered_sources
from .receipts import ReceiptError
from .delivery_value import HARNESS_REVIEW_SCHEMAS
from .runtime_binding import (
    RUNTIME_USAGE_BINDING_ENV,
    RUNTIME_USAGE_BINDING_FILE_ENV,
    RuntimeUsageBindingError,
    load_runtime_usage_binding,
    load_measurement_baseline_reference,
)
from .recommendations import (
    RecommendationError,
    build_recommendation_artifact,
    validate_recommendation_artifact,
)
from .scl_measurement import SclMeasurementError, load_scl_measurement_snapshot
from .harness_query_report import HarnessQueryError, load_report as load_harness_query_report
from .wi_discovery import WiDiscoveryError, auto_discover_wi_usage
from .wi_pricing import (WiPricingError, price_wi_usage_cohort,
                         retrospective_pricing_cohort as _retrospective_pricing_cohort)
from .wi_report import WiReportError, build_wi_report


def _collaboration_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--collaboration", action="store_true", help="use the versioned collaboration receipts, not legacy usage rows")
    parser.add_argument("--source-registry", type=Path, help="private, host-managed source registry; defaults to PULSE_COMPUTECOST_SOURCES")
    parser.add_argument("--catalog-snapshot", type=Path, help="timestamped public catalog: {retrieved_at, catalog}")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--scope", choices=("activity", "wi_lifetime", "accepted_wi"))
    parser.add_argument("--group-by", action="append", default=[], help="repeat or comma-separate wi,agent,day,week,month,runtime,model")
    parser.add_argument("--agent")
    parser.add_argument("--agent-role", choices=("executor", "responsible", "delegator", "reviewer"), default="executor")
    parser.add_argument("--runtime-filter", choices=("pi", "codex", "cc", "cc_sdk", "unknown"))
    parser.add_argument("--model-filter")
    parser.add_argument("--as-of", help="ledger ingestion-time cutoff, not backdated source observation time")
    parser.add_argument("--watermark", type=int)
    parser.add_argument("--context-ref", help="saved opaque stage context; exact facts/links only, no session expansion")
    parser.add_argument("--value-from", help="explicit ISO timestamp for the measured benefit period")
    parser.add_argument("--value-to", help="exclusive measured benefit period end")
    parser.add_argument("--cost-basis", choices=("machine_standard", "machine_plus_measured_human"), default="machine_standard")
    parser.add_argument("--human-hourly-rate", help="approved measured-human rate, never HAU conversion")
    parser.add_argument("--human-rate-policy")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulse-compute-cost")
    subparsers = parser.add_subparsers(dest="command", required=True)
    price = subparsers.add_parser(
        "openrouter-price",
        help="fetch one public, versioned OpenRouter price snapshot",
    )
    price.add_argument("--model", default=DEFAULT_REFERENCE_MODEL)
    price.add_argument("--timeout-seconds", type=float, default=20)
    snapshot = subparsers.add_parser(
        "openrouter-snapshot",
        help="fetch public prices and a secret-safe actual-usage summary",
    )
    snapshot.add_argument("--env-file", type=Path, required=True)
    snapshot.add_argument("--model", default=DEFAULT_REFERENCE_MODEL)
    snapshot.add_argument("--timeout-seconds", type=float, default=20)
    usage = subparsers.add_parser(
        "usage-probe",
        help="read and reconcile local Relay/Codex/CC evidence without writing a ledger",
    )
    usage.add_argument("--machine", required=True)
    usage.add_argument("--relay-metering-dir", type=Path)
    usage.add_argument("--cc-projects-dir", type=Path)
    usage.add_argument("--codex-sessions-dir", type=Path)
    usage.add_argument("--model-catalog", type=Path)
    usage.add_argument("--since", help="inclusive, timezone-aware ISO timestamp")

    managed = subparsers.add_parser(
        "managed-snapshot",
        help="materialize one current managed_cost_snapshot.v1 from local usage evidence",
    )
    managed.add_argument("--product-month", required=True, help="Beijing month, YYYY-MM")
    managed.add_argument("--machine", required=True)
    managed.add_argument("--relay-metering-dir", type=Path)
    managed.add_argument("--cc-projects-dir", type=Path)
    managed.add_argument("--codex-sessions-dir", type=Path)
    managed.add_argument("--model-catalog", type=Path)
    managed.add_argument("--model", default=DEFAULT_REFERENCE_MODEL)
    managed.add_argument("--timeout-seconds", type=float, default=20)
    managed.add_argument("--output", type=Path, required=True)

    query = subparsers.add_parser(
        "query",
        help="query one Beijing-local half-open cost window directly from the usage ledger",
    )
    query.add_argument("--ledger-root", type=Path)
    query.add_argument("--from", dest="from_date", help="YYYY-MM-DD")
    query.add_argument("--to", dest="to_date", help="YYYY-MM-DD, exclusive")
    query.add_argument(
        "--price-snapshot",
        type=Path,
        help="single OpenRouter reference-price snapshot applied uniformly to all known-source events",
    )
    query.add_argument(
        "--bridge-baseline-price-snapshot",
        type=Path,
        action="append",
        default=[],
        help="repeatable model-aware baseline price evidence for the S4 D-R-E-P bridge",
    )
    query.add_argument(
        "--bridge-current-price-snapshot",
        type=Path,
        action="append",
        default=[],
        help="repeatable model-aware current price evidence for the S4 D-R-E-P bridge",
    )

    _collaboration_options(query)
    query.add_argument("--work-item", action="append", default=[])
    collect = subparsers.add_parser("collect", help="import registered native/Host/Harness facts into UsageLedger")
    collect.add_argument("--source-registry", type=Path)
    collect.add_argument("--ledger-root", type=Path)

    saved_usage = subparsers.add_parser("import-scl-usage", help="join one saved native SCL record to the Host's existing receipt ledger")
    saved_usage.add_argument("--work-item", required=True)
    saved_usage.add_argument("--record-id", required=True)
    assessments = subparsers.add_parser("import-scl-assessments", help="observe exact native candidate decisions without issuing acceptance")
    assessments.add_argument("--work-item", required=True)
    assessments.add_argument("--candidate-id", required=True)

    stage_source = subparsers.add_parser("stage-source", help="read bounded native activity references; no transcript or WI grant")
    stage_source.add_argument("--start-byte", type=int, required=True)
    stage_source.add_argument("--end-byte", type=int, required=True)
    stage_record = subparsers.add_parser("record-stage", help="save native-bound declarations to the Host's existing receipt ledger")
    stage_record.add_argument("--input", required=True, help="bounded JSON command file, or - for stdin; no identity/store overrides")

    recommendation = subparsers.add_parser(
        "recommendation-artifact",
        help="build a Duke-facing recommend-only artifact from read-only manager facts",
    )
    recommendation.add_argument("--input", type=Path, required=True)

    app_server = subparsers.add_parser(
        "app-server-epoch",
        help="persist one host-observed App Server usage lifecycle fact",
    )
    app_server.add_argument(
        "action", choices=("bind", "observe", "complete", "seal")
    )
    app_server.add_argument("--ledger-path", type=Path, required=True)
    app_server.add_argument("--work-item")
    app_server.add_argument("--phase")
    app_server.add_argument("--epoch", type=int)
    app_server.add_argument("--runtime")
    app_server.add_argument("--procurement-source")
    app_server.add_argument("--model", required=True)
    app_server.add_argument("--reasoning-effort", required=True)
    app_server.add_argument(
        "--notification",
        type=Path,
        help="host-captured JSON notification; required except for seal",
    )
    app_server.add_argument(
        "--observed-at",
        help="timezone-aware host observation timestamp; defaults to current UTC",
    )
    app_server.add_argument("--operation-ref", help="required for source seal")

    wi_report = subparsers.add_parser(
        "wi-report",
        help="report exact sealed usage and explicit evidence gaps for one WI",
    )
    wi_report.add_argument("work_item")
    _collaboration_options(wi_report)
    wi_report.add_argument("--ledger-root", type=Path)
    wi_report.add_argument("--from", dest="from_date")
    wi_report.add_argument("--to", dest="to_date")
    wi_report.add_argument(
        "--app-server-ledger",
        type=Path,
        help="optional sealed App Server ledger; producer discovery runs without it",
    )
    wi_report.add_argument(
        "--no-auto-discover",
        action="store_true",
        help="disable registered local producer discovery",
    )
    wi_report.add_argument("--harness-root", type=Path)
    wi_report.add_argument("--codex-home", type=Path)
    wi_report.add_argument(
        "--cc-projects-dir",
        type=Path,
        help="optional Claude Code projects root for exact user-prompt WI discovery",
    )
    wi_report.add_argument("--model-catalog", type=Path)
    wi_report.add_argument(
        "--relay-config",
        type=Path,
        help="optional Relay registration source; defaults to RELAY_CONFIG_PATH or ~/.relay/relay.json",
    )
    wi_report.add_argument(
        "--relay-state",
        type=Path,
        help="optional Relay state source; defaults beside relay.json",
    )
    wi_report.add_argument(
        "--relay-pi-manifest",
        type=Path,
        help="optional Pi manifest; defaults to RELAY_PI_RUNTIME_MANIFEST_PATH or Relay local-pulse",
    )
    wi_report.add_argument(
        "--relay-metering-dir",
        type=Path,
        help="optional Relay metering source; defaults to METERING_DIR or beside relay.json",
    )
    wi_report.add_argument("--machine", default=socket.gethostname())
    wi_report.add_argument(
        "--openrouter-models-snapshot",
        type=Path,
        help="optional pinned raw OpenRouter /models response; WI machine cash uses flat standard rates from this digest-bound catalog, else live public prices are fetched",
    )
    wi_report.add_argument(
        "--no-auto-price",
        action="store_true",
        help="do not calculate WI machine cash for discovered usage",
    )
    wi_report.add_argument("--account-snapshot", type=Path)
    wi_report.add_argument("--pi-snapshot", type=Path)
    wi_report.add_argument(
        "--harness-query-snapshot",
        type=Path,
        help="replacement Harness inspect --view measurements query; explicit bounded read-side report without legacy discovery or pricing",
    )
    wi_report.add_argument(
        "--measurement-snapshot",
        type=Path,
        help="Harness s1_measurement_pilot.v1 projection for quality/effect/efficiency",
    )
    wi_report.add_argument(
        "--scl-measurement-snapshot",
        type=Path,
        help="optional prebuilt SCL attribution/usage/outcome/attention join",
    )
    wi_report.add_argument(
        "--thread-usage-snapshot",
        type=Path,
        help="host-bound App Server thread estimated credits/USD snapshot",
    )
    wi_report.add_argument(
        "--review-snapshot",
        type=Path,
        help="exact-candidate Harness work_item_review/v1 or v2",
    )
    wi_report.add_argument(
        "--business-value-receipt",
        type=Path,
        help="downstream-consumer verified business-value receipt",
    )

    thread_usage = subparsers.add_parser(
        "app-server-thread-usage",
        help="sanitize host-read thread credits/USD without emitting raw thread identity",
    )
    thread_usage.add_argument(
        "--response",
        type=Path,
        required=True,
        help="transient account/usage/read JSON response owned by the host",
    )

    pi_probe = subparsers.add_parser(
        "pi-session-probe",
        help="summarize the current host-injected PI_SESSION_FILE without raw identity",
    )
    pi_probe.add_argument("--work-item")
    pi_probe.add_argument("--phase")
    pi_probe.add_argument("--epoch", type=int)
    pi_probe.add_argument("--procurement-source")
    pi_probe.add_argument(
        "--lifecycle-state",
        choices=("open", "sealed"),
        default="open",
        help="host lifecycle state; only a post-final host hook may request sealed",
    )
    pi_probe.add_argument(
        "--measurement-role",
        choices=("baseline", "progress", "final"),
        help="managed Pi boundary role; Relay supplies this from the lifecycle hook",
    )

    cc_probe = subparsers.add_parser(
        "cc-session-probe",
        help="capture a bounded Claude Code transcript snapshot for the host-injected binding",
    )
    cc_probe.add_argument("--lifecycle-state", choices=("open", "sealed"), default="open")
    cc_probe.add_argument("--measurement-role", choices=("baseline", "progress", "final"), required=True)

    account_probe = subparsers.add_parser(
        "app-server-account-probe",
        help="read sanitized account usage and quota from an explicit App Server host",
    )
    account_probe.add_argument("--timeout-seconds", type=float, default=15)
    return parser


def _openrouter_price(args: argparse.Namespace) -> dict[str, object]:
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return parse_openrouter_prices(
        fetch_openrouter_models(timeout_seconds=args.timeout_seconds),
        args.model,
        retrieved_at=retrieved_at,
    ).to_dict()


def _openrouter_snapshot(args: argparse.Namespace) -> dict[str, object]:
    management_key = load_dotenv_value(args.env_file)
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    activity = summarize_openrouter_activity(
        fetch_openrouter_activity(
            management_key,
            timeout_seconds=args.timeout_seconds,
        )
    )
    prices = parse_openrouter_prices(
        fetch_openrouter_models(timeout_seconds=args.timeout_seconds),
        args.model,
        retrieved_at=retrieved_at,
    )
    return {
        "schema_version": 1,
        "generated_at": retrieved_at,
        "activity": activity,
        "reference_price": prices.to_dict(),
    }


def _usage_probe(args: argparse.Namespace) -> dict[str, object]:
    if not any((args.relay_metering_dir, args.cc_projects_dir, args.codex_sessions_dir)):
        raise AdapterError("usage-probe requires at least one local evidence directory")
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    collection = collect_usage_events(
        machine=args.machine,
        observed_at=observed_at,
        relay_metering_dir=args.relay_metering_dir,
        cc_projects_dir=args.cc_projects_dir,
        codex_sessions_dir=args.codex_sessions_dir,
        model_catalog_path=args.model_catalog,
        since=args.since,
    )
    return {
        "schema_version": 1,
        "generated_at": observed_at,
        "mode": "read_only_probe",
        "summary": collection.summary(),
    }


def _private_atomic_json(path: Path, value: Mapping[str, object]) -> None:
    if not path.is_absolute():
        raise ManagedCostSnapshotError("managed snapshot output must be absolute")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _managed_snapshot(args: argparse.Namespace) -> dict[str, object]:
    if not any((args.relay_metering_dir, args.cc_projects_dir, args.codex_sessions_dir)):
        raise AdapterError("managed-snapshot requires at least one local evidence directory")
    try:
        month_start = datetime.strptime(args.product_month, "%Y-%m").replace(
            day=1,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    except ValueError as exc:
        raise ManagedCostSnapshotError("product_month must be YYYY-MM") from exc
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    collection = collect_usage_events(
        machine=args.machine,
        observed_at=generated_at,
        relay_metering_dir=args.relay_metering_dir,
        cc_projects_dir=args.cc_projects_dir,
        codex_sessions_dir=args.codex_sessions_dir,
        model_catalog_path=args.model_catalog,
        since=month_start.astimezone(timezone.utc).isoformat(),
    )
    price = parse_openrouter_prices(
        fetch_openrouter_models(timeout_seconds=args.timeout_seconds),
        args.model,
        retrieved_at=generated_at,
    )
    snapshot = build_managed_cost_snapshot(
        product_month=args.product_month,
        events=[event for event in collection.events if event.product_month == args.product_month],
        price=price,
        generated_at=generated_at,
        settlement_status="provisional",
        source_coverage_complete=False,
    ).to_dict()
    _private_atomic_json(args.output, snapshot)
    return snapshot


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise QueryError(f"{field} must be a non-negative decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise QueryError(f"{field} must be a non-negative decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise QueryError(f"{field} must be a non-negative decimal")
    return parsed


def _load_price_snapshot(path: Path) -> OpenRouterPriceSnapshot:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise QueryError(f"cannot read price snapshot {path}") from exc
    if not isinstance(value, Mapping):
        raise QueryError("price snapshot must be a JSON object")
    if (
        value.get("schema_version") != 1
        or value.get("source") != "openrouter.models"
        or value.get("currency") != "USD"
        or value.get("unit") != "per_token"
    ):
        raise QueryError("unsupported price snapshot contract")
    pricing = value.get("pricing")
    if not isinstance(pricing, Mapping):
        raise QueryError("price snapshot pricing must be an object")
    for field in ("model_id", "canonical_slug", "retrieved_at"):
        if not isinstance(value.get(field), str) or not str(value.get(field)).strip():
            raise QueryError(f"price snapshot {field} must be a non-empty string")
    cache_raw = pricing.get("input_cache_read")
    return OpenRouterPriceSnapshot(
        model_id=str(value["model_id"]),
        canonical_slug=str(value["canonical_slug"]),
        retrieved_at=str(value["retrieved_at"]),
        prompt_per_token=_decimal(pricing.get("prompt"), "pricing.prompt"),
        completion_per_token=_decimal(
            pricing.get("completion"), "pricing.completion"
        ),
        input_cache_read_per_token=(
            _decimal(cache_raw, "pricing.input_cache_read")
            if cache_raw is not None
            else None
        ),
    )


def _native_query(args: argparse.Namespace) -> bool:
    return args.command in {"query", "wi-report"} and bool(os.environ.get("PULSE_WORK_ITEM_CORE_HOST"))


def _registry(args: argparse.Namespace) -> tuple[dict[str, Any] | None, Path | None]:
    if _native_query(args):
        # Default continuation reads the exact store used by record-stage. An
        # invocation environment or old snapshot must not silently fork it.
        allowed = {"command", "work_item", "from_date", "to_date", "collaboration", "timezone",
                   "scope", "group_by", "agent", "agent_role", "runtime_filter", "model_filter",
                   "as_of", "watermark", "context_ref", "value_from", "value_to", "cost_basis",
                   "human_hourly_rate", "human_rate_policy"}
        defaults = _parser().parse_args([args.command] + ([args.work_item] if args.command == "wi-report" else []))
        if any(name not in allowed and value != getattr(defaults, name) for name, value in vars(args).items()):
            raise ReceiptError("native query cannot override the registered store or price source with legacy options")
        from .native_stage_host import installed_host
        return installed_host().registry()
    path = args.source_registry or (Path(os.environ["PULSE_COMPUTECOST_SOURCES"])
                                   if os.environ.get("PULSE_COMPUTECOST_SOURCES") else None)
    if path is None:
        return None, None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ReceiptError("source registry must be a private regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ReceiptError("source registry must be an object")
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError("source registry is unavailable or invalid") from exc
    return value, path


# Full-ledger reads (~1.9 GB resident, 2.5 GB peak) do not fit beside the
# scheduled collect-shared on the 3.7 GB TCloud node; they belong on ownermac.
HEAVY_QUERY_REFUSED_SITES = frozenset({"tcloud"})
HEAVY_QUERY_OVERRIDE_ENV = "PULSE_COMPUTE_COST_ALLOW_HEAVY_ON_PEER"
EXIT_NODE_NOT_SERVED = 69  # sysexits EX_UNAVAILABLE: this node does not serve the command
_NATIVE_SOURCE_KINDS = frozenset({"codex", "pi", "cc", "relay_host"})


class HeavyQueryRefused(Exception):
    """The command needs the full ledger and this node does not serve it."""


def _account_registry_path() -> Path:
    # The deployment-registered location, under the account home even when an
    # Agent runs with an isolated HOME.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/pulse/compute-cost/sources.json"


def _registered_site(registry: Mapping[str, Any] | None) -> str | None:
    """The node every local native source is enrolled under, if unanimous."""
    if registry is None:
        try:
            registry = json.loads(_account_registry_path().read_bytes())
        except (OSError, ValueError):
            return None
    sources = registry.get("sources") if isinstance(registry, Mapping) else None
    nodes = {source.get("node") for source in sources or ()
             if isinstance(source, Mapping) and source.get("kind") in _NATIVE_SOURCE_KINDS}
    return nodes.pop() if len(nodes) == 1 and isinstance(next(iter(nodes)), str) else None


def _refuse_heavy_query_on_peer(registry: Mapping[str, Any] | None) -> None:
    if os.environ.get(HEAVY_QUERY_OVERRIDE_ENV) == "1":
        return
    site = _registered_site(registry)
    if site in HEAVY_QUERY_REFUSED_SITES:
        raise HeavyQueryRefused(
            f"该查询需加载全量计量账本，{site} 内存不足，本节点不提供。"
            "请在 ownermac 执行同一命令，或用 Stats 查询（Stats/exporter/pulse-stats-query.py / pulse_stats 工具）。"
            f"确需在本节点运行时，人类可设 {HEAVY_QUERY_OVERRIDE_ENV}=1 绕过。"
        )


def _receipt_ledger(args: argparse.Namespace, registry: Mapping[str, Any] | None) -> UsageLedger:
    root = args.ledger_root or (registry or {}).get("ledger_root") or os.environ.get("PULSE_COMPUTECOST_LEDGER")
    if not root:
        raise ReceiptError("collaboration query requires a configured ledger root")
    if registry:
        from .observation_authority import registered_ledger
        return registered_ledger(registry)
    return UsageLedger(root)


def _stage(args: argparse.Namespace) -> dict[str, Any]:
    from .native_stage_host import installed_host
    from .stage_record import source_summary
    host = installed_host()
    if args.command == "import-scl-usage":
        return host.import_usage(args.work_item, args.record_id)
    if args.command == "import-scl-assessments":
        return host.import_assessments(args.work_item, args.candidate_id)
    if args.command == "stage-source":
        return source_summary(host.capture({"start_byte": args.start_byte, "end_byte": args.end_byte}))
    try:
        if args.input == "-":
            raw = sys.stdin.buffer.read(65537)
        else:
            path = Path(args.input)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                raise ReceiptError("stage command requires a bounded regular file")
            raw = path.read_bytes()
        if len(raw) > 65536:
            raise ReceiptError("stage command exceeds the input bound")
        command = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ReceiptError("stage command is unavailable or invalid bounded JSON") from exc
    return host.record(command)


def _collect(args: argparse.Namespace) -> dict[str, Any]:
    registry, _ = _registry(args)
    if registry is None:
        raise ReceiptError("collect requires a host-managed source registry")
    return collect_registered_sources(registry, _receipt_ledger(args, registry))


def _collaboration_query(args: argparse.Namespace, *, work_item: str | None = None) -> dict[str, Any]:
    # Validate every query argument before opening a ledger or advancing a
    # source watermark.  A rejected request must be observationally read-only.
    query = CollaborationQuery(
        from_date=args.from_date, to_date=args.to_date, timezone=args.timezone,
        work_items=(work_item,) if work_item else tuple(args.work_item), agent=args.agent,
        agent_role=args.agent_role, runtime=args.runtime_filter, model=args.model_filter,
        group_by=tuple(part.strip() for raw in args.group_by for part in raw.split(",") if part.strip()),
        scope=args.scope or ("activity" if args.from_date else "wi_lifetime"),
        as_of=args.as_of, watermark=args.watermark, context_ref=args.context_ref,
        value_from=args.value_from, value_to=args.value_to,
        cost_basis=args.cost_basis, human_hourly_rate=args.human_hourly_rate, human_rate_policy=args.human_rate_policy,
    )
    from .catalog_source import resolve_catalog_path, observe_catalog_snapshot
    registry, registry_path = _registry(args)
    _refuse_heavy_query_on_peer(registry)
    snapshot, price_error = observe_catalog_snapshot(resolve_catalog_path(
        explicit=args.catalog_snapshot, registry=registry, registry_path=registry_path))
    catalog = snapshot['catalog'] if snapshot else None
    retrieved_at = snapshot['retrieved_at'] if snapshot else None
    ledger = _receipt_ledger(args, registry)
    collection = None
    # A historical read is read-only. Later imports cannot rewrite its watermark.
    if registry and args.as_of is None and args.watermark is None and not _native_query(args):
        collection = collect_registered_sources(registry, ledger)
    report = query_collaboration(ledger, query, catalog=catalog, catalog_observed_at=retrieved_at)
    if price_error:
        print(json.dumps({"price_observation": {"state": "unavailable", "reason": price_error}}), file=sys.stderr)
    if collection is not None:
        # Collection diagnostics are a transport wrapper, not part of the reproducible report digest.
        return {"collection": collection, "report": report}
    return report


def _query(args: argparse.Namespace) -> dict[str, Any]:
    if _native_query(args) or args.context_ref or args.collaboration or args.source_registry or os.environ.get("PULSE_COMPUTECOST_SOURCES"):
        return _collaboration_query(args)
    if not args.from_date or not args.to_date:
        raise QueryError("legacy query requires --from and --to; use --collaboration for WI lifetime")
    if not args.ledger_root:
        raise QueryError("legacy query requires --ledger-root")
    window = QueryWindow.from_text(args.from_date, args.to_date)
    prices = (
        [_load_price_snapshot(args.price_snapshot)]
        if args.price_snapshot is not None
        else []
    )
    return query_ledger(
        ledger=UsageLedger(args.ledger_root),
        window=window,
        prices=prices,
        bridge_baseline_prices=[
            _load_price_snapshot(path) for path in args.bridge_baseline_price_snapshot
        ],
        bridge_current_prices=[
            _load_price_snapshot(path) for path in args.bridge_current_price_snapshot
        ],
        generated_at=datetime.now(timezone.utc),
    )


def _recommendation_artifact(args: argparse.Namespace) -> dict[str, Any]:
    try:
        facts = json.loads(args.input.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, json.JSONDecodeError) as exc:
        raise RecommendationError(f"cannot read recommendation input {args.input}") from exc
    if not isinstance(facts, Mapping):
        raise RecommendationError("recommendation input must be a JSON object")
    artifact = build_recommendation_artifact(facts)
    validate_recommendation_artifact(artifact)
    return artifact


def _app_server_epoch(args: argparse.Namespace) -> dict[str, Any]:
    has_host_binding = bool(
        os.environ.get(RUNTIME_USAGE_BINDING_ENV)
        or os.environ.get(RUNTIME_USAGE_BINDING_FILE_ENV)
    )
    binding = load_runtime_usage_binding() if has_host_binding else None
    if binding is not None:
        if binding.runtime not in {"codex", "chatgpt_work", "chatgpt_web"}:
            raise AppServerUsageError(
                "host runtime usage binding is not for an App Server host"
            )
        explicit = {
            "work_item_id": args.work_item,
            "phase": args.phase,
            "epoch": args.epoch,
            "runtime": args.runtime,
            "procurement_source": args.procurement_source,
        }
        for field, value in explicit.items():
            if value is not None and value != getattr(binding, field):
                raise AppServerUsageError(
                    f"explicit {field} conflicts with host binding"
                )
        attribution = {
            "work_item_id": binding.work_item_id,
            "phase": binding.phase,
            "epoch": binding.epoch,
            "runtime": binding.runtime,
            "procurement_source": binding.procurement_source,
            "runtime_binding_sha256": binding.binding_sha256,
        }
    else:
        if any(
            value is None
            for value in (
                args.work_item,
                args.phase,
                args.epoch,
                args.runtime,
                args.procurement_source,
            )
        ):
            raise AppServerUsageError(
                "App Server attribution requires a host binding or all legacy explicit fields"
            )
        attribution = {
            "work_item_id": args.work_item,
            "phase": args.phase,
            "epoch": args.epoch,
            "runtime": args.runtime,
            "procurement_source": args.procurement_source,
            "runtime_binding_sha256": None,
        }
    context = EpochUsageContext(
        **attribution,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    observer = AppServerEpochObserver(
        AppServerUsageLedger(args.ledger_path), context
    )
    observed_at = args.observed_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    if args.action == "seal":
        if args.notification is not None:
            raise AppServerUsageError("seal does not accept --notification")
        if args.operation_ref is None:
            raise AppServerUsageError("seal requires --operation-ref")
        result = observer.seal_source(
            operation_ref=args.operation_ref, observed_at=observed_at
        )
    else:
        if args.notification is None:
            raise AppServerUsageError(
                f"{args.action} requires --notification from the host"
            )
        if args.operation_ref is not None:
            raise AppServerUsageError(
                "--operation-ref is only valid for source seal"
            )
        try:
            notification = json.loads(args.notification.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppServerUsageError(
                f"cannot read App Server notification {args.notification}"
            ) from exc
        method = {
            "bind": observer.bind,
            "observe": observer.observe,
            "complete": observer.complete_turn,
        }[args.action]
        result = method(notification, observed_at=observed_at)
    return {
        "schema": "app_server_epoch_usage_receipt/v1",
        "inserted": result.inserted,
        "duplicate": result.duplicate,
        "fact": result.fact,
    }


def _pricing_cohort(
    work_item_id: str,
    usage_cohort: Mapping[str, Any] | None,
    pi_snapshot: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if pi_snapshot is not None:
        try:
            validate_pi_session_snapshot(pi_snapshot)
        except PiUsageError as exc:
            raise WiPricingError(str(exc)) from exc
        if pi_snapshot.get("work_item_id") != work_item_id:
            raise WiPricingError("Pi usage snapshot belongs to another work item")
    # Explicit Pi snapshots remain diagnostics. Preserve the complete canonical
    # cohort, including its partial status, boundary and source evidence.
    if usage_cohort is None or usage_cohort.get("status") not in {"observed", "partial"}:
        return None
    requests = usage_cohort.get("request_partitions")
    if not isinstance(requests, list):
        raise WiPricingError("discovered usage lacks per-request pricing evidence")
    return usage_cohort if requests else None


def _retrospective_price_result(
    *,
    work_item_id: str,
    candidate_sha256: str | None,
    status: str,
    valuation: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "computecost_retrospective_openrouter_estimate/v1",
        "work_item_id": work_item_id,
        "status": status,
        "canonical_attribution": False,
        "canonical_total_eligible": False,
        "may_include_unrelated_turns": True,
        "noncanonical_total": True,
        "source_candidate_sha256": candidate_sha256,
        "window": "created_to_terminal",
        "classification": "pi_only_estimate_may_include_unrelated_turns",
        "valuation": dict(valuation) if valuation is not None else "unavailable",
        "reason": reason,
    }
    result["estimate_sha256"] = sha256(json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()).hexdigest()
    return result


def _discover_harness_root(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    configured = os.environ.get("HARNESS_ROOT")
    if configured:
        return Path(configured)
    for parent in (Path.cwd(), *Path.cwd().parents):
        candidate = parent / ".harness"
        if candidate.is_dir():
            return candidate
    return None


def _wi_report(args: argparse.Namespace) -> dict[str, Any]:
    if _native_query(args):
        return _collaboration_query(args, work_item=args.work_item)
    try:
        registry = _registry(args)[0]
    except ReceiptError:
        # Only site detection reads it here; the selected path keeps its own errors.
        registry = None
    _refuse_heavy_query_on_peer(registry)
    if args.harness_query_snapshot is not None:
        defaults = _parser().parse_args(["wi-report", args.work_item])
        incompatible = [
            key for key, value in vars(args).items()
            if key not in {"harness_query_snapshot", "no_auto_discover", "no_auto_price"}
            and value != getattr(defaults, key)
        ]
        if incompatible:
            raise HarnessQueryError("Harness query snapshot cannot mix report options: " + ", ".join(sorted(incompatible)))
        return load_harness_query_report(args.harness_query_snapshot, args.work_item)
    if args.context_ref or args.collaboration or args.source_registry or os.environ.get("PULSE_COMPUTECOST_SOURCES"):
        return _collaboration_query(args, work_item=args.work_item)
    account_snapshot = None
    if args.account_snapshot is not None:
        try:
            account_snapshot = json.loads(
                args.account_snapshot.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise WiReportError("cannot read App Server account snapshot") from exc
        if not isinstance(account_snapshot, Mapping):
            raise WiReportError("App Server account snapshot must be an object")
    pi_snapshot = None
    if args.pi_snapshot is not None:
        try:
            pi_snapshot = json.loads(args.pi_snapshot.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WiReportError("cannot read Pi usage snapshot") from exc
        if not isinstance(pi_snapshot, Mapping):
            raise WiReportError("Pi usage snapshot must be an object")
    measurement_snapshot = None
    if args.measurement_snapshot is not None:
        try:
            measurement_snapshot = json.loads(
                args.measurement_snapshot.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise WiReportError("cannot read Harness measurement snapshot") from exc
        if not isinstance(measurement_snapshot, Mapping):
            raise WiReportError("Harness measurement snapshot must be an object")
    scl_measurement_snapshot = None
    if args.scl_measurement_snapshot is not None:
        try:
            scl_measurement_snapshot = json.loads(
                args.scl_measurement_snapshot.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise WiReportError("cannot read SCL measurement snapshot") from exc
        if not isinstance(scl_measurement_snapshot, Mapping):
            raise WiReportError("SCL measurement snapshot must be an object")
    else:
        harness_root = _discover_harness_root(args.harness_root)
        if harness_root is not None:
            try:
                scl_measurement_snapshot = load_scl_measurement_snapshot(
                    harness_root, args.work_item
                )
            except SclMeasurementError as exc:
                raise WiReportError(str(exc)) from exc
    thread_usage_snapshot = None
    if args.thread_usage_snapshot is not None:
        try:
            thread_usage_snapshot = json.loads(
                args.thread_usage_snapshot.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise WiReportError("cannot read App Server thread usage snapshot") from exc
        if not isinstance(thread_usage_snapshot, Mapping):
            raise WiReportError("App Server thread usage snapshot must be an object")
    review_snapshot = None
    if args.review_snapshot is not None:
        try:
            review_snapshot = json.loads(
                args.review_snapshot.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise WiReportError("cannot read Harness review snapshot") from exc
        if not isinstance(review_snapshot, Mapping):
            raise WiReportError("Harness review snapshot must be an object")
        if review_snapshot.get("schema_version") not in HARNESS_REVIEW_SCHEMAS:
            if (
                review_snapshot.get("ok") is not True
                or review_snapshot.get("error") is not None
                or review_snapshot.get("review_state")
                not in {"approved", "changes_requested"}
                or not isinstance(review_snapshot.get("review"), Mapping)
                or review_snapshot["review"].get("decision")
                != review_snapshot.get("review_state")
            ):
                raise WiReportError(
                    "Harness review export is unavailable, stale, or malformed"
                )
            review_snapshot = review_snapshot["review"]
    business_value_receipt = None
    if args.business_value_receipt is not None:
        try:
            business_value_receipt = json.loads(
                args.business_value_receipt.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise WiReportError("cannot read business value receipt") from exc
        if not isinstance(business_value_receipt, Mapping):
            raise WiReportError("business value receipt must be an object")
    usage_cohort = None
    if not args.no_auto_discover:
        try:
            usage_cohort = auto_discover_wi_usage(
                args.work_item,
                machine=args.machine,
                harness_root=args.harness_root,
                codex_home=args.codex_home,
                cc_projects_dir=args.cc_projects_dir,
                model_catalog_path=args.model_catalog,
                scl_measurement_snapshot=scl_measurement_snapshot,
                relay_config_path=args.relay_config,
                relay_state_path=args.relay_state,
                relay_pi_manifest_path=args.relay_pi_manifest,
                relay_metering_dir=args.relay_metering_dir,
            ).to_dict()
        except WiDiscoveryError:
            if args.app_server_ledger is None:
                raise
    openrouter_reference_cost = None
    retrospective_openrouter_estimate = None
    if not args.no_auto_price and (usage_cohort is not None or pi_snapshot is not None):
        retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        canonical_pricing_cohort: Mapping[str, Any] | None = None
        retrospective_pricing_cohort: Mapping[str, Any] | None = None
        retrospective_candidate_sha256: str | None = None
        try:
            canonical_pricing_cohort = _pricing_cohort(
                args.work_item, usage_cohort, pi_snapshot
            )
        except WiPricingError as exc:
            openrouter_reference_cost = {
                "schema": "computecost_openrouter_cash_valuation/v2",
                "work_item_id": args.work_item,
                "status": "join_conflict",
                "reason": str(exc),
            }
        try:
            retrospective_pricing_cohort, retrospective_candidate_sha256 = (
                _retrospective_pricing_cohort(args.work_item, usage_cohort)
            )
        except WiPricingError as exc:
            retrospective_openrouter_estimate = _retrospective_price_result(
                work_item_id=args.work_item,
                candidate_sha256=None,
                status="join_conflict",
                reason=str(exc),
            )
        if retrospective_candidate_sha256 and retrospective_pricing_cohort is None:
            retrospective_openrouter_estimate = _retrospective_price_result(
                work_item_id=args.work_item,
                candidate_sha256=retrospective_candidate_sha256,
                status="token_quantity_unavailable",
                reason="retrospective Pi request Token evidence is unavailable",
            )
        if canonical_pricing_cohort is not None or retrospective_pricing_cohort is not None:
            try:
                if args.openrouter_models_snapshot is not None:
                    models_payload = json.loads(
                        args.openrouter_models_snapshot.read_text(encoding="utf-8")
                    )
                    if not isinstance(models_payload, Mapping):
                        raise WiPricingError("OpenRouter models snapshot must be an object")
                else:
                    models_payload = fetch_openrouter_models(timeout_seconds=20)
            except (OpenRouterContractError, OSError, json.JSONDecodeError, WiPricingError):
                if canonical_pricing_cohort is not None:
                    openrouter_reference_cost = {
                        "schema": "computecost_openrouter_cash_valuation/v2",
                        "work_item_id": args.work_item,
                        "status": "producer_unreachable",
                        "reason": "OpenRouter public model catalog could not be read",
                    }
                if retrospective_pricing_cohort is not None:
                    retrospective_openrouter_estimate = _retrospective_price_result(
                        work_item_id=args.work_item,
                        candidate_sha256=retrospective_candidate_sha256,
                        status="producer_unreachable",
                        reason="OpenRouter public model catalog could not be read",
                    )
            else:
                if canonical_pricing_cohort is not None:
                    try:
                        openrouter_reference_cost = price_wi_usage_cohort(
                            canonical_pricing_cohort,
                            models_payload,
                            retrieved_at=retrieved_at,
                        )
                    except WiPricingError as exc:
                        openrouter_reference_cost = {
                            "schema": "computecost_openrouter_cash_valuation/v2",
                            "work_item_id": args.work_item,
                            "status": "join_conflict",
                            "reason": str(exc),
                        }
                if retrospective_pricing_cohort is not None:
                    try:
                        valuation = price_wi_usage_cohort(
                            retrospective_pricing_cohort,
                            models_payload,
                            retrieved_at=retrieved_at,
                        )
                        retrospective_openrouter_estimate = _retrospective_price_result(
                            work_item_id=args.work_item,
                            candidate_sha256=retrospective_candidate_sha256,
                            status="calculated",
                            valuation=valuation,
                        )
                    except WiPricingError as exc:
                        retrospective_openrouter_estimate = _retrospective_price_result(
                            work_item_id=args.work_item,
                            candidate_sha256=retrospective_candidate_sha256,
                            status="join_conflict",
                            reason=str(exc),
                        )
    ledger = (
        AppServerUsageLedger(args.app_server_ledger)
        if args.app_server_ledger is not None
        else None
    )
    return build_wi_report(
        ledger,
        args.work_item,
        usage_cohort=usage_cohort,
        openrouter_reference_cost=openrouter_reference_cost,
        retrospective_openrouter_estimate=retrospective_openrouter_estimate,
        account_snapshot=account_snapshot,
        pi_snapshot=pi_snapshot,
        measurement_snapshot=measurement_snapshot,
        scl_measurement_snapshot=scl_measurement_snapshot,
        thread_usage_snapshot=thread_usage_snapshot,
        review_snapshot=review_snapshot,
        business_value_receipt=business_value_receipt,
    )


def _app_server_thread_usage(args: argparse.Namespace) -> dict[str, Any]:
    raw_thread_id = os.environ.get("PULSE_APP_SERVER_THREAD_ID")
    if not raw_thread_id:
        raise AppServerThreadUsageError(
            "PULSE_APP_SERVER_THREAD_ID is required from the App Server host"
        )
    binding = load_runtime_usage_binding()
    try:
        response = json.loads(args.response.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppServerThreadUsageError(
            "cannot read host App Server thread usage response"
        ) from exc
    return build_app_server_thread_usage_snapshot(
        response,
        raw_thread_id=raw_thread_id,
        binding=binding,
    )


def _pi_session_probe(args: argparse.Namespace) -> dict[str, Any]:
    origin = None
    session_file = os.environ.get("PI_SESSION_FILE")
    if not session_file:
        raise PiUsageError("PI_SESSION_FILE is unavailable from the Pi host")
    has_host_binding = bool(
        os.environ.get(RUNTIME_USAGE_BINDING_ENV)
        or os.environ.get(RUNTIME_USAGE_BINDING_FILE_ENV)
    )
    if has_host_binding:
        binding = load_runtime_usage_binding()
        origin = load_measurement_baseline_reference(binding)
        if origin is not None and args.measurement_role == "baseline":
            raise PiUsageError("an accepted canonical baseline cannot be regenerated")
        if binding.runtime != "pi":
            raise PiUsageError("host runtime usage binding is not for Pi")
        explicit = {
            "work_item_id": args.work_item,
            "phase": args.phase,
            "epoch": args.epoch,
            "procurement_source": args.procurement_source,
        }
        for field, value in explicit.items():
            if value is not None and value != getattr(binding, field):
                raise PiUsageError(f"explicit {field} conflicts with host binding")
        if binding.execution_surface is None or binding.authority_ref is None:
            raise PiUsageError(
                "managed Pi measurement requires a v3 surface/session binding"
            )
        pi_binding = PiUsageBinding(
            work_item_id=binding.work_item_id,
            phase=binding.phase,
            epoch=binding.epoch,
            procurement_source=binding.procurement_source,
            runtime_binding_sha256=(origin["baseline_binding_sha256"] if origin else binding.binding_sha256),
            agent_id=binding.agent_id,
            execution_surface=binding.execution_surface,
            authority_ref=binding.authority_ref,
        )
    else:
        if any(
            value is None
            for value in (
                args.work_item,
                args.phase,
                args.epoch,
                args.procurement_source,
            )
        ):
            raise PiUsageError(
                "Pi attribution requires a host binding or all legacy explicit fields"
            )
        pi_binding = PiUsageBinding(
            work_item_id=args.work_item,
            phase=args.phase,
            epoch=args.epoch,
            procurement_source=args.procurement_source,
        )
    baseline_snapshot = None
    if pi_binding.managed and args.measurement_role in {"progress", "final"}:
        baseline_path_text = os.environ.get("PULSE_PI_USAGE_BASELINE_SNAPSHOT")
        if not baseline_path_text:
            raise PiUsageError("managed Pi measurement baseline is unavailable")
        baseline_path = Path(baseline_path_text)
        if (
            not baseline_path.is_absolute()
            or baseline_path.is_symlink()
            or not baseline_path.is_file()
            or (baseline_path.stat().st_mode & 0o777) != 0o600
        ):
            raise PiUsageError("managed Pi measurement baseline must be a mode-0600 regular file")
        try:
            baseline_snapshot = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PiUsageError("managed Pi measurement baseline is unreadable") from exc
        if not isinstance(baseline_snapshot, Mapping):
            raise PiUsageError("managed Pi measurement baseline must be an object")
    if origin is not None and (baseline_snapshot is None or baseline_snapshot.get("snapshot_sha256") != origin["baseline_sha256"]):
        raise PiUsageError("canonical measurement origin baseline does not match")
    capture_path = os.environ.get("PULSE_PI_USAGE_CAPTURE_PATH")
    build = capture_pi_session_snapshot if capture_path else build_pi_session_snapshot
    return build(
        session_file,
        pi_binding,
        **({"output_path": capture_path} if capture_path else {}),
        lifecycle_state=args.lifecycle_state,
        measurement_role=args.measurement_role,
        baseline_snapshot=baseline_snapshot,
        unpersisted_baseline=os.environ.get("PULSE_PI_USAGE_UNPERSISTED_BASELINE") == "1",
    )


def _cc_session_probe(args: argparse.Namespace) -> dict[str, Any]:
    """Relay Host 在 CC settled 边界调用；transcript 路径与原始 session id 只经 env 传入。"""
    session_file = os.environ.get("CC_SESSION_FILE")
    session_id = os.environ.get("CC_SESSION_ID")
    capture_path = os.environ.get("PULSE_CC_USAGE_CAPTURE_PATH")
    if not session_file or not Path(session_file).is_absolute() or not session_id or not capture_path:
        raise PiUsageError("CC session file, session id and capture path are required host bindings")
    binding = load_runtime_usage_binding()
    if binding.runtime not in CC_RUNTIMES:
        raise PiUsageError("host runtime usage binding is not for Claude Code")
    if binding.execution_surface is None or binding.authority_ref is None:
        raise PiUsageError("managed CC measurement requires a v3 surface/session binding")
    cc_binding = CcUsageBinding(
        work_item_id=binding.work_item_id, agent_id=binding.agent_id, phase=binding.phase,
        epoch=binding.epoch, runtime=binding.runtime, procurement_source=binding.procurement_source,
        authority_ref=binding.authority_ref, execution_surface=binding.execution_surface,
        runtime_binding_sha256=binding.binding_sha256,
    )
    baseline_snapshot = None
    if args.measurement_role != "baseline":
        baseline_path = Path(os.environ.get("PULSE_CC_USAGE_BASELINE_SNAPSHOT") or "")
        if (
            not baseline_path.is_absolute() or baseline_path.is_symlink() or not baseline_path.is_file()
            or (baseline_path.stat().st_mode & 0o777) != 0o600
        ):
            raise PiUsageError("managed CC measurement baseline must be a mode-0600 regular file")
        try:
            baseline_snapshot = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PiUsageError("managed CC measurement baseline is unreadable") from exc
    return capture_cc_session_snapshot(
        session_file, session_id, cc_binding, output_path=capture_path,
        lifecycle_state=args.lifecycle_state, measurement_role=args.measurement_role,
        baseline_snapshot=baseline_snapshot,
    )


def _app_server_account_probe(args: argparse.Namespace) -> dict[str, Any]:
    binary = os.environ.get("PULSE_CODEX_APP_SERVER_BIN")
    codex_home = os.environ.get("PULSE_CODEX_HOME")
    if not binary or not codex_home:
        raise AppServerAccountError(
            "PULSE_CODEX_APP_SERVER_BIN and PULSE_CODEX_HOME are required host bindings"
        )
    return probe_app_server_account(
        binary=binary,
        codex_home=codex_home,
        timeout_seconds=args.timeout_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0].startswith("wi_"):
        arguments.insert(0, "wi-report")
    args = _parser().parse_args(arguments)
    try:
        if args.command == "openrouter-price":
            result = _openrouter_price(args)
        elif args.command == "openrouter-snapshot":
            result = _openrouter_snapshot(args)
        elif args.command == "usage-probe":
            result = _usage_probe(args)
        elif args.command == "managed-snapshot":
            result = _managed_snapshot(args)
        elif args.command == "collect":
            result = _collect(args)
        elif args.command in {"stage-source", "record-stage", "import-scl-usage", "import-scl-assessments"}:
            result = _stage(args)
        elif args.command == "query":
            result = _query(args)
        elif args.command == "recommendation-artifact":
            result = _recommendation_artifact(args)
        elif args.command == "app-server-epoch":
            result = _app_server_epoch(args)
        elif args.command == "wi-report":
            result = _wi_report(args)
        elif args.command == "app-server-thread-usage":
            result = _app_server_thread_usage(args)
        elif args.command == "pi-session-probe":
            result = _pi_session_probe(args)
        elif args.command == "cc-session-probe":
            result = _cc_session_probe(args)
        elif args.command == "app-server-account-probe":
            result = _app_server_account_probe(args)
        else:  # pragma: no cover - argparse owns the command set
            raise AssertionError(f"unsupported command: {args.command}")
    except (
        AdapterError,
        AppServerAccountError,
        AppServerThreadUsageError,
        AppServerUsageError,
        LedgerError,
        ManagedCostSnapshotError,
        OpenRouterContractError,
        PiUsageError,
        QueryError,
        ReceiptError,
        RecommendationError,
        RuntimeUsageBindingError,
        SclMeasurementError,
        HarnessQueryError,
        UsageEventError,
        WiDiscoveryError,
        WiPricingError,
        WiReportError,
        OSError,
    ) as exc:
        print(f"pulse-compute-cost: {exc}", file=sys.stderr)
        return 1
    except HeavyQueryRefused as exc:
        print(f"pulse-compute-cost: {exc}", file=sys.stderr)
        return EXIT_NODE_NOT_SERVED
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
