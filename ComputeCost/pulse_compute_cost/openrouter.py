"""Read-only OpenRouter contracts with secret-safe summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen


DEFAULT_REFERENCE_MODEL = "deepseek/deepseek-v4-flash-0731"
OPENROUTER_ACTIVITY_URL = "https://openrouter.ai/api/v1/activity"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class OpenRouterContractError(ValueError):
    """Raised when an OpenRouter payload or local credential contract is invalid."""


@dataclass(frozen=True)
class OpenRouterPriceSnapshot:
    """Versioned, exact OpenRouter standard prices (WI machine-cash authority)."""

    model_id: str
    canonical_slug: str
    retrieved_at: str
    prompt_per_token: Decimal
    completion_per_token: Decimal
    input_cache_read_per_token: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source": "openrouter.models",
            "retrieved_at": self.retrieved_at,
            "model_id": self.model_id,
            "canonical_slug": self.canonical_slug,
            "currency": "USD",
            "unit": "per_token",
            "pricing": {
                "prompt": _decimal_text(self.prompt_per_token),
                "completion": _decimal_text(self.completion_per_token),
                "input_cache_read": (
                    _decimal_text(self.input_cache_read_per_token)
                    if self.input_cache_read_per_token is not None
                    else None
                ),
            },
        }


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise OpenRouterContractError(f"{field} must be a decimal value")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise OpenRouterContractError(f"{field} must be a decimal value") from exc
    if not parsed.is_finite() or parsed < 0:
        raise OpenRouterContractError(f"{field} must be non-negative and finite")
    return parsed


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OpenRouterContractError(f"{field} must be a non-negative integer")
    return value


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpenRouterContractError(f"{field} must be a non-empty string")
    return value.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_dotenv_value(path: str | Path, key: str = "OPENROUTER_M_KEY") -> str:
    """Read one exact dotenv key without evaluating the file or returning other values."""

    matches: list[str] = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        if not separator or name.strip() != key:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise OpenRouterContractError(f"{key} is empty or invalid")
        matches.append(value)
    if len(matches) != 1:
        raise OpenRouterContractError(f"expected exactly one {key} entry")
    return matches[0]


def _fetch_json(
    request: Request,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any],
) -> Mapping[str, Any]:
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = json.load(response, parse_float=Decimal)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise OpenRouterContractError("OpenRouter request or JSON decoding failed") from exc
    if not isinstance(payload, Mapping):
        raise OpenRouterContractError("OpenRouter response must be a JSON object")
    return payload


def fetch_openrouter_activity(
    management_key: str,
    *,
    timeout_seconds: float = 20,
    opener: Callable[..., Any] = urlopen,
) -> Mapping[str, Any]:
    """Fetch the read-only 30-completed-UTC-day activity payload."""

    if not isinstance(management_key, str) or not management_key:
        raise OpenRouterContractError("management key is required")
    request = Request(
        OPENROUTER_ACTIVITY_URL,
        headers={
            "Authorization": f"Bearer {management_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    return _fetch_json(request, timeout_seconds=timeout_seconds, opener=opener)


def fetch_openrouter_models(
    *,
    timeout_seconds: float = 20,
    opener: Callable[..., Any] = urlopen,
) -> Mapping[str, Any]:
    """Fetch the public model catalog without sending a management credential."""

    request = Request(
        OPENROUTER_MODELS_URL,
        headers={"Accept": "application/json"},
        method="GET",
    )
    return _fetch_json(request, timeout_seconds=timeout_seconds, opener=opener)


def parse_openrouter_prices(
    payload: Mapping[str, Any],
    model_id: str = DEFAULT_REFERENCE_MODEL,
    *,
    retrieved_at: str | None = None,
) -> OpenRouterPriceSnapshot:
    """Extract one exact price snapshot from the public model catalog."""

    requested = _nonempty_string(model_id, "model_id")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise OpenRouterContractError("models.data must be a list")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("id") == requested]
    if len(matches) != 1:
        raise OpenRouterContractError(f"expected exactly one model row for {requested}")
    row = matches[0]
    pricing = row.get("pricing")
    if not isinstance(pricing, Mapping):
        raise OpenRouterContractError("model pricing must be an object")
    if "prompt" not in pricing or "completion" not in pricing:
        raise OpenRouterContractError("model pricing requires prompt and completion")
    cache_value = pricing.get("input_cache_read")
    return OpenRouterPriceSnapshot(
        model_id=requested,
        canonical_slug=_nonempty_string(row.get("canonical_slug"), "canonical_slug"),
        retrieved_at=_nonempty_string(retrieved_at or _utc_now(), "retrieved_at"),
        prompt_per_token=_nonnegative_decimal(pricing["prompt"], "pricing.prompt"),
        completion_per_token=_nonnegative_decimal(
            pricing["completion"], "pricing.completion"
        ),
        input_cache_read_per_token=(
            _nonnegative_decimal(cache_value, "pricing.input_cache_read")
            if cache_value is not None
            else None
        ),
    )


def _decimal_text(value: Decimal) -> str:
    if not value:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def summarize_openrouter_activity(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and aggregate activity without retaining identity-bearing fields."""

    rows = payload.get("data")
    if not isinstance(rows, list):
        raise OpenRouterContractError("activity.data must be a list")

    totals = {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "usage_usd": Decimal("0"),
        "byok_usage_usd": Decimal("0"),
    }
    by_day: dict[str, dict[str, Any]] = {}
    by_model: dict[tuple[str, str, str], dict[str, Any]] = {}

    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise OpenRouterContractError(f"activity.data[{index}] must be an object")
        raw_date = _nonempty_string(row.get("date"), f"activity.data[{index}].date")
        try:
            day = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date().isoformat()
        except ValueError as exc:
            raise OpenRouterContractError(
                f"activity.data[{index}].date must be ISO-compatible"
            ) from exc
        model = _nonempty_string(row.get("model"), f"activity.data[{index}].model")
        model_permaslug = _nonempty_string(
            row.get("model_permaslug"), f"activity.data[{index}].model_permaslug"
        )
        provider = _nonempty_string(
            row.get("provider_name"), f"activity.data[{index}].provider_name"
        )
        parsed = {
            "requests": _nonnegative_int(
                row.get("requests"), f"activity.data[{index}].requests"
            ),
            "prompt_tokens": _nonnegative_int(
                row.get("prompt_tokens"), f"activity.data[{index}].prompt_tokens"
            ),
            "completion_tokens": _nonnegative_int(
                row.get("completion_tokens"),
                f"activity.data[{index}].completion_tokens",
            ),
            "reasoning_tokens": _nonnegative_int(
                row.get("reasoning_tokens"),
                f"activity.data[{index}].reasoning_tokens",
            ),
            "usage_usd": _nonnegative_decimal(
                row.get("usage"), f"activity.data[{index}].usage"
            ),
            "byok_usage_usd": _nonnegative_decimal(
                row.get("byok_usage_inference", 0),
                f"activity.data[{index}].byok_usage_inference",
            ),
        }
        for field in ("requests", "prompt_tokens", "completion_tokens", "reasoning_tokens"):
            totals[field] += parsed[field]
        totals["usage_usd"] += parsed["usage_usd"]
        totals["byok_usage_usd"] += parsed["byok_usage_usd"]

        day_bucket = by_day.setdefault(
            day,
            {
                "date": day,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "usage_usd": Decimal("0"),
            },
        )
        model_key = (model, model_permaslug, provider)
        model_bucket = by_model.setdefault(
            model_key,
            {
                "model": model,
                "model_permaslug": model_permaslug,
                "provider": provider,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "usage_usd": Decimal("0"),
            },
        )
        for bucket in (day_bucket, model_bucket):
            for field in ("requests", "prompt_tokens", "completion_tokens", "reasoning_tokens"):
                bucket[field] += parsed[field]
            bucket["usage_usd"] += parsed["usage_usd"]

    def serialize_bucket(bucket: Mapping[str, Any]) -> dict[str, Any]:
        rendered = dict(bucket)
        rendered["usage_usd"] = _decimal_text(rendered["usage_usd"])
        return rendered

    days = sorted(by_day)
    return {
        "schema_version": 1,
        "source": "openrouter.activity",
        "coverage": "last_30_completed_utc_days",
        "period": {
            "start_utc_day": days[0] if days else None,
            "end_utc_day": days[-1] if days else None,
        },
        "row_count": len(rows),
        "totals": {
            "requests": totals["requests"],
            "prompt_tokens": totals["prompt_tokens"],
            "completion_tokens": totals["completion_tokens"],
            "reasoning_tokens": totals["reasoning_tokens"],
            "usage_usd": _decimal_text(totals["usage_usd"]),
            "byok_usage_usd": _decimal_text(totals["byok_usage_usd"]),
        },
        "by_day": [serialize_bucket(by_day[day]) for day in days],
        "by_model": [
            serialize_bucket(by_model[key])
            for key in sorted(by_model)
        ],
    }
