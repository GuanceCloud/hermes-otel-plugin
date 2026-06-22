from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import urlparse

from . import AGENT_RUNTIME, AGENT_VERSION
from .otel_runtime import OTelRuntime


def _attrs(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


def _token_totals(usage: dict[str, Any] | None) -> dict[str, int]:
    payload = usage or {}
    input_tokens = payload.get("input_tokens")
    output_tokens = payload.get("output_tokens")
    input_value = int(input_tokens) if isinstance(input_tokens, (int, float)) and input_tokens >= 0 else 0
    output_value = int(output_tokens) if isinstance(output_tokens, (int, float)) and output_tokens >= 0 else 0
    return {
        "input": input_value,
        "output": output_value,
        "total": input_value + output_value,
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
        self._request_count = None
        self._request_duration = None
        self._token_usage = None
        self._client_token_usage = None
        self._client_operation_duration = None
        self._session_token_input = None
        self._session_token_output = None
        self._session_token_total = None
        self._session_token_usage = None
        self._operation_count = None
        self._operation_duration = None
        self._session_trace_count = None
        self._skill_activation_count = None
        self._subagent_count = None
        self._subagent_duration = None
        self._tool_call_count = None
        self._tool_call_duration = None
        self._session_start_count = None
        self._session_end_count = None
        self._session_reset_count = None
        self._turn_interrupted_count = None

    def _ensure_instruments(self) -> bool:
        if self._initialized:
            return True
        with self._lock:
            if self._initialized:
                return True
            meter = self._runtime.get_meter()
            if meter is None:
                return False
            self._request_count = meter.create_counter(
                "gen_ai.agent.request.count",
                description="Hermes agent turn completions observed by the plugin",
            )
            self._request_duration = meter.create_histogram(
                "gen_ai.agent.request.duration",
                unit="ms",
                description="Hermes agent turn duration",
            )
            self._token_usage = meter.create_histogram(
                "gen_ai.agent.token.usage",
                unit="{token}",
                description="Hermes model token usage",
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
            self._session_token_input = meter.create_counter(
                "gen_ai.agent.session.token.input",
                description="Hermes session-level aggregated input tokens",
            )
            self._session_token_output = meter.create_counter(
                "gen_ai.agent.session.token.output",
                description="Hermes session-level aggregated output tokens",
            )
            self._session_token_total = meter.create_counter(
                "gen_ai.agent.session.token.total",
                description="Hermes session-level aggregated total tokens",
            )
            self._session_token_usage = meter.create_counter(
                "gen_ai.agent.session.token.usage",
                description="Hermes session-level aggregated token usage by token_type",
            )
            self._operation_count = meter.create_counter(
                "gen_ai.agent.operation.count",
                description="Hermes model/tool/subagent operations",
            )
            self._operation_duration = meter.create_histogram(
                "gen_ai.agent.operation.duration",
                unit="ms",
                description="Hermes model/tool/subagent operation duration",
            )
            self._session_trace_count = meter.create_counter(
                "gen_ai.agent.session.trace.count",
                description="Hermes traces started by the plugin",
            )
            self._skill_activation_count = meter.create_counter(
                "gen_ai.agent.skill.activation.count",
                description="Hermes skill activation count",
            )
            self._subagent_count = meter.create_counter(
                "gen_ai.agent.subagent.count",
                description="Hermes subagent completions observed by the plugin",
            )
            self._subagent_duration = meter.create_histogram(
                "gen_ai.agent.subagent.duration",
                unit="ms",
                description="Hermes subagent execution duration",
            )
            self._tool_call_count = meter.create_counter(
                "gen_ai.runtime.tool.call.count",
                description="Hermes tool calls observed by the plugin",
            )
            self._tool_call_duration = meter.create_histogram(
                "gen_ai.runtime.tool.call.duration",
                unit="ms",
                description="Hermes tool call duration",
            )
            self._session_start_count = meter.create_counter(
                "gen_ai.runtime.session.start.count",
                description="Hermes session starts observed by the plugin",
            )
            self._session_end_count = meter.create_counter(
                "gen_ai.runtime.session.end.count",
                description="Hermes session endings observed by the plugin",
            )
            self._session_reset_count = meter.create_counter(
                "gen_ai.runtime.session.reset.count",
                description="Hermes session resets observed by the plugin",
            )
            self._turn_interrupted_count = meter.create_counter(
                "gen_ai.runtime.turn.interrupted.count",
                description="Hermes interrupted turns observed by the plugin",
            )
            self._initialized = True
            return True

    def record_turn_started(self, session_id: str, platform: str, model: str, request_type: str | None = None) -> None:
        if not self._ensure_instruments():
            return
        self._session_trace_count.add(
            1,
            _attrs(
                agent_runtime=AGENT_RUNTIME,
                agent_version=AGENT_VERSION,
                session_id=session_id,
                platform=platform,
                request_model=model,
                request_type=request_type,
            ),
        )

    def record_turn_finished(
        self,
        session_id: str,
        platform: str,
        model: str,
        outcome: str,
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
            agent_runtime=AGENT_RUNTIME,
            agent_version=AGENT_VERSION,
            session_id=session_id,
            platform=platform,
            provider_name=provider_name,
            request_model=model,
            response_model=response_model,
            request_type=request_type,
            review_category=review_category,
            session_state=session_state,
            outcome=outcome,
        )
        self._request_count.add(1, attrs)
        self._request_duration.record(max(0.0, duration_ms), attrs)

        numeric_usage = _token_totals(usage)
        session_token_attrs = _attrs(
            agent_runtime=AGENT_RUNTIME,
            agent_version=AGENT_VERSION,
            session_id=session_id,
            provider_name=provider_name,
            request_model=model,
        )
        self._session_token_input.add(numeric_usage["input"], session_token_attrs)
        self._session_token_output.add(numeric_usage["output"], session_token_attrs)
        self._session_token_total.add(numeric_usage["total"], session_token_attrs)
        for token_type, value in numeric_usage.items():
            self._session_token_usage.add(
                value,
                _attrs(
                    **session_token_attrs,
                    token_type=token_type,
                ),
            )

    def record_api_request(
        self,
        session_id: str,
        platform: str,
        request_model: str,
        provider_name: str,
        duration_ms: float,
        outcome: str,
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
        attrs = _attrs(
            agent_runtime=AGENT_RUNTIME,
            agent_version=AGENT_VERSION,
            session_id=session_id,
            platform=platform,
            operation_name="model",
            provider_name=provider_name,
            request_model=request_model,
            response_model=response_model,
            outcome=outcome,
            **standard_attrs,
        )
        self._operation_count.add(1, attrs)
        self._operation_duration.record(max(0.0, duration_ms), attrs)
        self._client_operation_duration.record(max(0.0, duration_ms) / 1000.0, attrs)

        numeric_usage = _token_totals(usage)
        for token_type, value in numeric_usage.items():
            if isinstance(value, (int, float)) and value >= 0:
                legacy_token_attrs = _attrs(
                    agent_runtime=AGENT_RUNTIME,
                    agent_version=AGENT_VERSION,
                    session_id=session_id,
                    platform=platform,
                    provider_name=provider_name,
                    request_model=request_model,
                    response_model=response_model,
                    token_type=token_type,
                    **standard_attrs,
                )
                self._token_usage.record(float(value), legacy_token_attrs)
                if token_type in {"input", "output"}:
                    self._client_token_usage.record(
                        float(value),
                        _attrs(
                            **legacy_token_attrs,
                            **{
                                "gen_ai.token.type": token_type,
                            },
                        ),
                    )

    def record_tool_call(
        self,
        session_id: str,
        platform: str,
        tool_name: str,
        duration_ms: float,
        outcome: str,
        result_status: str | None = None,
        skill_name: str | None = None,
        model_name: str | None = None,
    ) -> None:
        if not self._ensure_instruments():
            return
        runtime_attrs = _attrs(
            agent_runtime=AGENT_RUNTIME,
            agent_version=AGENT_VERSION,
            session_id=session_id,
            platform=platform,
            tool_name=tool_name,
            skill_name=skill_name,
            tool_result_status=result_status,
            outcome=outcome,
        )
        self._tool_call_count.add(1, runtime_attrs)
        self._tool_call_duration.record(max(0.0, duration_ms), runtime_attrs)

        operation_attrs = _attrs(
            agent_runtime=AGENT_RUNTIME,
            agent_version=AGENT_VERSION,
            session_id=session_id,
            platform=platform,
            operation_name="tool",
            tool_name=tool_name,
            **{
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool_name,
                "gen_ai.conversation.id": session_id,
            },
            skill_name=skill_name,
            model_name=model_name,
            tool_result_status=result_status,
            outcome=outcome,
        )
        self._operation_count.add(1, operation_attrs)
        self._operation_duration.record(max(0.0, duration_ms), operation_attrs)

    def record_skill_activation(self, session_id: str, skill_name: str, skill_source: str = "runtime") -> None:
        if not self._ensure_instruments():
            return
        self._skill_activation_count.add(
            1,
            _attrs(
                agent_runtime=AGENT_RUNTIME,
                agent_version=AGENT_VERSION,
                session_id=session_id,
                skill_name=skill_name,
                skill_source=skill_source,
            ),
        )

    def record_skill_operation(
        self,
        session_id: str,
        skill_name: str,
        duration_ms: float,
        outcome: str,
        skill_source: str = "runtime",
    ) -> None:
        if not self._ensure_instruments():
            return
        attrs = _attrs(
            agent_runtime=AGENT_RUNTIME,
            agent_version=AGENT_VERSION,
            session_id=session_id,
            operation_name="skill",
            skill_name=skill_name,
            skill_source=skill_source,
            outcome=outcome,
        )
        self._operation_count.add(1, attrs)
        self._operation_duration.record(max(0.0, duration_ms), attrs)

    def record_subagent(
        self,
        session_id: str,
        child_role: str,
        duration_ms: float,
        outcome: str,
    ) -> None:
        if not self._ensure_instruments():
            return
        attrs = _attrs(
            agent_runtime=AGENT_RUNTIME,
            agent_version=AGENT_VERSION,
            session_id=session_id,
            subagent_role=child_role,
            outcome=outcome,
            operation_name="subagent",
        )
        self._subagent_count.add(1, attrs)
        self._subagent_duration.record(max(0.0, duration_ms), attrs)
        self._operation_count.add(1, attrs)
        self._operation_duration.record(max(0.0, duration_ms), attrs)

    def record_session_start(self, session_id: str, platform: str) -> None:
        if not self._ensure_instruments():
            return
        self._session_start_count.add(
            1,
            _attrs(agent_runtime=AGENT_RUNTIME, agent_version=AGENT_VERSION, session_id=session_id, platform=platform),
        )

    def record_session_end(self, session_id: str, platform: str, outcome: str) -> None:
        if not self._ensure_instruments():
            return
        self._session_end_count.add(
            1,
            _attrs(
                agent_runtime=AGENT_RUNTIME,
                agent_version=AGENT_VERSION,
                session_id=session_id,
                platform=platform,
                outcome=outcome,
            ),
        )

    def record_session_reset(self, session_id: str, platform: str) -> None:
        if not self._ensure_instruments():
            return
        self._session_reset_count.add(
            1,
            _attrs(agent_runtime=AGENT_RUNTIME, agent_version=AGENT_VERSION, session_id=session_id, platform=platform),
        )

    def record_interrupted_turn(self, session_id: str, platform: str) -> None:
        if not self._ensure_instruments():
            return
        self._turn_interrupted_count.add(
            1,
            _attrs(agent_runtime=AGENT_RUNTIME, agent_version=AGENT_VERSION, session_id=session_id, platform=platform),
        )
