from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


DEFAULT_ENDPOINT = "http://127.0.0.1:9529/otel"
DEFAULT_TRACE_PATH = "v1/traces"
DEFAULT_METRICS_PATH = "v1/metrics"
DEFAULT_LOGS_PATH = "v1/logs"
DEFAULT_SERVICE_NAME = "hermes-otel-plugin"
DEFAULT_FLUSH_INTERVAL_MS = 30_000
DEFAULT_ROOT_SPAN_TTL_MS = 10 * 60 * 1000
DEFAULT_PROTOCOL = "http/protobuf"
DEFAULT_LOG_EVENTS = ("session", "api_request", "tool", "subagent")


@dataclass(slots=True)
class HermesOtelPluginConfig:
    enabled: bool = True
    endpoint: str = DEFAULT_ENDPOINT
    protocol: str = DEFAULT_PROTOCOL
    trace_path: str = DEFAULT_TRACE_PATH
    metrics_path: str = DEFAULT_METRICS_PATH
    logs_enabled: bool = False
    logs_path: str = DEFAULT_LOGS_PATH
    service_name: str = DEFAULT_SERVICE_NAME
    sample_rate: float = 1.0
    flush_interval_ms: int = DEFAULT_FLUSH_INTERVAL_MS
    root_span_ttl_ms: int = DEFAULT_ROOT_SPAN_TTL_MS
    trace_payload_debug_enabled: bool = False
    resource_attributes: dict[str, str | int | float | bool] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    log_events: tuple[str, ...] = DEFAULT_LOG_EVENTS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalize_endpoint(value: Any) -> str:
    if not isinstance(value, str):
        return DEFAULT_ENDPOINT
    trimmed = value.strip()
    return trimmed.rstrip("/") if trimmed else DEFAULT_ENDPOINT


def _normalize_path(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    trimmed = value.strip().strip("/")
    return trimmed or fallback


def _normalize_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    return fallback


def _normalize_str(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    trimmed = value.strip()
    return trimmed or fallback


def _normalize_float(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)):
        return fallback
    numeric = float(value)
    if numeric < minimum or numeric > maximum:
        return fallback
    return numeric


def _normalize_int(value: Any, fallback: int, minimum: int) -> int:
    if not isinstance(value, (int, float)):
        return fallback
    return max(minimum, int(value))


def _normalize_headers(value: Any) -> dict[str, str]:
    record = _as_mapping(value)
    normalized: dict[str, str] = {}
    for key, item in record.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, str) and item.strip():
            normalized[key] = item.strip()
    return normalized


def _normalize_resource_attributes(value: Any) -> dict[str, str | int | float | bool]:
    record = _as_mapping(value)
    normalized: dict[str, str | int | float | bool] = {}
    for key, item in record.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, (str, int, float, bool)):
            if isinstance(item, str):
                stripped = item.strip()
                if not stripped:
                    continue
                normalized[key] = stripped
            else:
                normalized[key] = item
    normalized["agent_runtime"] = "hermes"
    return normalized


def _normalize_log_events(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return DEFAULT_LOG_EVENTS
    normalized = tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )
    return normalized or DEFAULT_LOG_EVENTS


def resolve_plugin_config(raw_config: Any) -> HermesOtelPluginConfig:
    root = _as_mapping(raw_config)
    section = _as_mapping(root.get("hermes_otel_plugin", root))
    return HermesOtelPluginConfig(
        enabled=_normalize_bool(section.get("enabled"), True),
        endpoint=_normalize_endpoint(section.get("endpoint")),
        protocol=_normalize_str(section.get("protocol"), DEFAULT_PROTOCOL),
        trace_path=_normalize_path(section.get("trace_path"), DEFAULT_TRACE_PATH),
        metrics_path=_normalize_path(section.get("metrics_path"), DEFAULT_METRICS_PATH),
        logs_enabled=_normalize_bool(section.get("logs_enabled"), False),
        logs_path=_normalize_path(section.get("logs_path"), DEFAULT_LOGS_PATH),
        service_name=_normalize_str(section.get("service_name"), DEFAULT_SERVICE_NAME),
        sample_rate=_normalize_float(section.get("sample_rate"), 1.0, 0.0, 1.0),
        flush_interval_ms=_normalize_int(
            section.get("flush_interval_ms"),
            DEFAULT_FLUSH_INTERVAL_MS,
            1_000,
        ),
        root_span_ttl_ms=_normalize_int(
            section.get("root_span_ttl_ms"),
            DEFAULT_ROOT_SPAN_TTL_MS,
            1_000,
        ),
        trace_payload_debug_enabled=_normalize_bool(
            section.get("trace_payload_debug_enabled"),
            False,
        ),
        resource_attributes=_normalize_resource_attributes(section.get("resource_attributes")),
        headers=_normalize_headers(section.get("headers")),
        log_events=_normalize_log_events(section.get("log_events")),
    )


def load_plugin_config() -> HermesOtelPluginConfig:
    try:
        from hermes_cli.config import load_config

        raw = load_config()
    except Exception:
        raw = {}
    return resolve_plugin_config(raw)


def resolve_otlp_url(endpoint: str, signal_path: str) -> str:
    return f"{endpoint.rstrip('/')}/{signal_path.strip('/')}"

