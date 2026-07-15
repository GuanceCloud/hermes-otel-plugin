from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import urlparse

from .otel_runtime import OTelRuntime


def _attrs(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


def _operation_metric_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"error", "failed", "failure", "expired"}:
        return "error"
    return "ok"


def _token_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    if usage is None:
        return {}
    payload = usage
    input_tokens = payload.get("input_tokens")
    output_tokens = payload.get("output_tokens")
    input_value = int(input_tokens) if isinstance(input_tokens, (int, float)) and input_tokens >= 0 else 0
    output_value = int(output_tokens) if isinstance(output_tokens, (int, float)) and output_tokens >= 0 else 0
    return {
        "input": input_value,
        "output": output_value,
    }


def _server_attrs(base_url: str | None) -> dict[str, Any]:
    if not base_url:
        return {}
    parsed = urlparse(base_url)
    if not parsed.hostname:
        return {}
    return _attrs(
        **{
            "server.address": parsed.hostname,
            "server.port": parsed.port,
        }
    )


def _standard_model_attrs(
    *,
    session_id: str,
    provider_name: str | None,
    request_model: str | None,
    response_model: str | None = None,
    error_type: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    return _attrs(
        **{
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider_name,
            "gen_ai.request.model": request_model,
            "gen_ai.response.model": response_model,
            "gen_ai.conversation.id": session_id,
            "error.type": error_type,
            **_server_attrs(base_url),
        }
    )


class MetricManager:
    def __init__(self, runtime: OTelRuntime, logger: logging.Logger | None = None) -> None:
        self._runtime = runtime
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._initialized = False
        self._workflow_duration = None
        self._operation_count = None
        self._client_token_usage = None
        self._client_operation_duration = None

    def _ensure_instruments(self) -> bool:
        if self._initialized:
            return True
        with self._lock:
            if self._initialized:
                return True
            meter = self._runtime.get_meter()
            if meter is None:
                return False
            self._workflow_duration = meter.create_histogram(
                "gen_ai.workflow.duration",
                unit="s",
                description="Hermes agent workflow duration",
            )
            self._operation_count = meter.create_counter(
                "gen_ai.agent.operation.count",
                description="Hermes agent operation count",
            )
            self._client_token_usage = meter.create_histogram(
                "gen_ai.client.token.usage",
                unit="{token}",
                description="GenAI client token usage observed by Hermes",
            )
            self._client_operation_duration = meter.create_histogram(
                "gen_ai.client.operation.duration",
                unit="s",
                description="GenAI client operation duration observed by Hermes",
            )
            self._initialized = True
            return True

    def record_turn_started(self, session_id: str, platform: str, model: str, request_type: str | None = None) -> None:
        return

    def record_turn_finished(
        self,
        session_id: str,
        platform: str,
        model: str,
        status: str,
        duration_ms: float,
        provider_name: str | None = None,
        response_model: str | None = None,
        request_type: str | None = None,
        review_category: str | None = None,
        session_state: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        if not self._ensure_instruments():
            return
        attrs = _attrs(
            session_id=session_id,
            **{
                "gen_ai.conversation.id": session_id,
                "gen_ai.operation.name": "invoke_agent",
            },
        )
        self._workflow_duration.record(max(0.0, duration_ms) / 1000.0, attrs)

    def record_api_request(
        self,
        session_id: str,
        platform: str,
        request_model: str,
        provider_name: str,
        duration_ms: float,
        status: str,
        response_model: str | None = None,
        usage: dict[str, Any] | None = None,
        error_type: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not self._ensure_instruments():
            return
        standard_attrs = _standard_model_attrs(
            session_id=session_id,
            provider_name=provider_name,
            request_model=request_model,
            response_model=response_model,
            error_type=error_type,
            base_url=base_url,
        )
        attrs = _attrs(session_id=session_id, **standard_attrs)
        self._operation_count.add(
            1,
            _attrs(
                **standard_attrs,
                status=_operation_metric_status(status),
            ),
        )
        self._client_operation_duration.record(max(0.0, duration_ms) / 1000.0, attrs)

        numeric_usage = _token_usage(usage)
        for token_type, value in numeric_usage.items():
            if isinstance(value, (int, float)) and value >= 0:
                token_attrs = _attrs(
                    session_id=session_id,
                    **standard_attrs,
                    **{
                        "gen_ai.token.type": token_type,
                    },
                )
                self._client_token_usage.record(float(value), token_attrs)

    def record_tool_call(
        self,
        session_id: str,
        platform: str,
        tool_name: str,
        duration_ms: float,
        status: str,
        result_status: str | None = None,
        skill_name: str | None = None,
        model_name: str | None = None,
    ) -> None:
        if not self._ensure_instruments():
            return
        attrs = _attrs(
            session_id=session_id,
            **{
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool_name,
                "gen_ai.conversation.id": session_id,
            },
            tool_result_status=result_status,
        )
        self._operation_count.add(
            1,
            _attrs(
                **{
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool_name,
                    "status": _operation_metric_status(status),
                }
            ),
        )
        self._client_operation_duration.record(max(0.0, duration_ms) / 1000.0, attrs)

    def record_skill_activation(self, session_id: str, skill_name: str) -> None:
        return

    def record_skill_operation(
        self,
        session_id: str,
        skill_name: str,
        duration_ms: float,
        status: str,
    ) -> None:
        if not self._ensure_instruments():
            return
        attrs = _attrs(
            session_id=session_id,
            **{
                "gen_ai.operation.name": "skill",
                "gen_ai.conversation.id": session_id,
                "gen.ai.skill.name": skill_name,
            },
        )
        self._operation_count.add(
            1,
            _attrs(
                **{
                    "gen_ai.operation.name": "skill",
                    "gen.ai.skill.name": skill_name,
                    "status": _operation_metric_status(status),
                }
            ),
        )
        self._client_operation_duration.record(max(0.0, duration_ms) / 1000.0, attrs)

    def record_subagent(
        self,
        session_id: str,
        child_role: str,
        duration_ms: float,
        status: str,
    ) -> None:
        return

    def record_session_start(self, session_id: str, platform: str) -> None:
        return

    def record_session_end(self, session_id: str, platform: str, status: str) -> None:
        return

    def record_session_reset(self, session_id: str, platform: str) -> None:
        return

    def record_interrupted_turn(self, session_id: str, platform: str) -> None:
        return
