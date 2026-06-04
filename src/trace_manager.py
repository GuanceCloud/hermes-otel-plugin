from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import AGENT_RUNTIME, AGENT_VERSION
from .config import HermesOtelPluginConfig
from .log_manager import LogManager
from .metric_manager import MetricManager
from .otel_runtime import OTelRuntime
from .state_store import (
    ActiveSpanState,
    PendingLlmState,
    SessionLineageResolver,
    SessionMetadataResolver,
    SessionPromptResolver,
    TurnState,
    TurnStore,
)


def _clip(value: Any, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _json_preview(value: Any, limit: int = 240) -> str | None:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except Exception:
        text = str(value)
    return _clip(text, limit=limit)


def _json_size(value: Any) -> tuple[int, int]:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return len(text), len(text.encode("utf-8"))


def _count_image_tokens(msg: dict[str, Any], cost_per_image: int = 1500) -> int:
    count = 0
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"image", "image_url", "input_image"}:
                count += 1
    stashed = msg.get("_anthropic_content_blocks") if isinstance(msg, dict) else None
    if isinstance(stashed, list):
        for part in stashed:
            if isinstance(part, dict) and part.get("type") == "image":
                count += 1
    if isinstance(content, dict) and content.get("_multimodal"):
        inner = content.get("content")
        if isinstance(inner, list):
            for part in inner:
                if isinstance(part, dict) and part.get("type") in {"image", "image_url"}:
                    count += 1
    return count * cost_per_image


def _estimate_message_chars(msg: dict[str, Any]) -> int:
    if not isinstance(msg, dict):
        return len(str(msg))
    shadow: dict[str, Any] = {}
    for key, value in msg.items():
        if key == "_anthropic_content_blocks":
            continue
        if key == "content":
            if isinstance(value, list):
                cleaned = []
                for part in value:
                    if isinstance(part, dict) and part.get("type") in {"image", "image_url", "input_image"}:
                        cleaned.append({"type": part.get("type"), "image": "[stripped]"})
                    else:
                        cleaned.append(part)
                shadow[key] = cleaned
            elif isinstance(value, dict) and value.get("_multimodal"):
                shadow[key] = value.get("text_summary", "")
            else:
                shadow[key] = value
        else:
            shadow[key] = value
    return len(str(shadow))


def _request_user_prompt_stats(messages: list[Any]) -> int:
    total_chars = 0
    image_tokens = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        total_chars += _estimate_message_chars(message)
        image_tokens += _count_image_tokens(message)
    return ((total_chars + 3) // 4) + image_tokens


def _tool_call_preview(tool_names: list[str]) -> str | None:
    normalized: list[str] = []
    seen: set[str] = set()
    for name in tool_names:
        value = str(name).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    if not normalized:
        return None
    return _clip(f"toolCall:{','.join(normalized)}", limit=1200)


def _resolve_span_kind(resource_name: str) -> str:
    normalized = str(resource_name or "").strip().lower()
    if normalized == "hermes_request":
        return "request"
    if normalized == "agent_run":
        return "agent"
    if normalized == "llm":
        return "llm"
    if normalized.startswith("tool:"):
        return "tool"
    if normalized.startswith("skill:"):
        return "skill"
    if normalized.startswith("subagent:"):
        return "subagent"
    return "internal"


def _detect_turn_classification(user_message: str) -> dict[str, Any]:
    normalized = str(user_message or "").strip().lower()
    if not normalized:
        return {
            "request_type": "user_request",
            "is_auto_review": False,
        }

    auto_review_markers = (
        "review the conversation above and consider saving or updating a skill if appropriate",
        "focus on: was a non-trivial approach used to complete a task",
        "saving or updating a skill",
    )
    if any(marker in normalized for marker in auto_review_markers):
        return {
            "request_type": "auto_review",
            "is_auto_review": True,
            "review_category": "skill",
        }

    return {
        "request_type": "user_request",
        "is_auto_review": False,
    }


def _normalize_skill_metric_outcome(reason: str) -> str:
    normalized = str(reason or "").strip().lower()
    if normalized in {"failed", "expired", "error"}:
        return "error"
    return "completed"


def _normalized_tool_args_preview(args: dict[str, Any]) -> str:
    return _json_preview(args, limit=4096) or ""


def _extract_tool_result_status(parsed: Any) -> str | None:
    if not isinstance(parsed, dict):
        return None
    details = parsed.get("details")
    if isinstance(details, dict):
        nested_status = details.get("status")
        if isinstance(nested_status, str) and nested_status.strip():
            return nested_status.strip()
    status = parsed.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    return None


def _extract_tool_command(tool_name: str, args: dict[str, Any]) -> str | None:
    normalized_tool_name = str(tool_name).strip().lower()
    if normalized_tool_name == "terminal":
        command = args.get("cmd") or args.get("command")
        if isinstance(command, str):
            return _clip(command, limit=4096)
    return None


def _extract_tool_target(tool_name: str, args: dict[str, Any]) -> str | None:
    normalized_tool_name = str(tool_name).strip().lower()
    if normalized_tool_name in {"read_file", "write_file", "edit_file"}:
        path = args.get("path")
        if isinstance(path, str):
            return _clip(path, limit=1024)
    if normalized_tool_name == "search_files":
        path = args.get("path")
        if isinstance(path, str):
            return _clip(path, limit=1024)
        pattern = args.get("pattern")
        if isinstance(pattern, str):
            return _clip(pattern, limit=1024)
    if normalized_tool_name == "skill_view":
        skill_name = args.get("name")
        if isinstance(skill_name, str):
            return _clip(skill_name, limit=256)
    return None


def _extract_tool_skill_name(tool_name: str, args: dict[str, Any], parsed: Any) -> str | None:
    if str(tool_name).strip().lower() != "skill_view":
        return None
    parsed_name = parsed.get("name") if isinstance(parsed, dict) else None
    if isinstance(parsed_name, str) and parsed_name.strip():
        return parsed_name.strip()
    arg_name = args.get("name")
    if isinstance(arg_name, str) and arg_name.strip():
        return arg_name.strip()
    return None


def _resolve_tool_outcome(tool_name: str, parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return "completed"

    if parsed.get("error"):
        return "error"

    status = str(parsed.get("status") or "").strip().lower()
    if status in {"error", "failed", "failure", "blocked", "timeout", "cancelled", "canceled"}:
        return "error"
    if status in {"ok", "success", "completed"}:
        return "completed"

    success = parsed.get("success")
    if success is False:
        return "error"
    if success is True:
        return "completed"

    if tool_name == "delegate_task":
        results = parsed.get("results")
        if isinstance(results, list) and results:
            child_statuses = [
                str(item.get("status") or item.get("exit_reason") or "").strip().lower()
                for item in results
                if isinstance(item, dict)
            ]
            if any(
                child_status in {"error", "failed", "failure", "blocked", "timeout", "cancelled", "canceled"}
                for child_status in child_statuses
            ):
                return "error"
            if child_statuses and all(
                child_status in {"ok", "success", "completed"} for child_status in child_statuses
            ):
                return "completed"

    return "completed"


def _wall_ns() -> int:
    return time.time_ns()


def _mono_ns() -> int:
    return time.monotonic_ns()


def _as_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    payload = usage or {}
    input_tokens = _as_non_negative_int(payload.get("input_tokens"))
    output_tokens = _as_non_negative_int(payload.get("output_tokens"))
    cache_read_tokens = _as_non_negative_int(payload.get("cache_read_tokens"))
    cache_write_tokens = _as_non_negative_int(payload.get("cache_write_tokens"))
    reasoning_tokens = _as_non_negative_int(payload.get("reasoning_tokens"))
    total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_total_tokens": cache_read_tokens + cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _usage_attrs(usage_summary: dict[str, int]) -> dict[str, int]:
    return {
        "usage_input_tokens": usage_summary["input_tokens"],
        "usage_output_tokens": usage_summary["output_tokens"],
        "usage_total_tokens": usage_summary["total_tokens"],
        "usage_cache_read_input_tokens": usage_summary["cache_read_tokens"],
        "usage_cache_write_input_tokens": usage_summary["cache_write_tokens"],
        "usage_cache_total_tokens": usage_summary["cache_total_tokens"],
        "usage_reasoning_tokens": usage_summary["reasoning_tokens"],
    }


def _normalize_cache_usage_for_turn(turn: TurnState, usage_summary: dict[str, int]) -> dict[str, int]:
    raw_cache_read_tokens = usage_summary["cache_read_tokens"]
    raw_cache_write_tokens = usage_summary["cache_write_tokens"]

    previous_cache_read_tokens = turn.last_raw_cache_read_tokens
    previous_cache_write_tokens = turn.last_raw_cache_write_tokens

    cache_read_tokens = raw_cache_read_tokens
    if (
        previous_cache_read_tokens is not None
        and raw_cache_read_tokens >= previous_cache_read_tokens
    ):
        cache_read_tokens = raw_cache_read_tokens - previous_cache_read_tokens

    cache_write_tokens = raw_cache_write_tokens
    if (
        previous_cache_write_tokens is not None
        and raw_cache_write_tokens >= previous_cache_write_tokens
    ):
        cache_write_tokens = raw_cache_write_tokens - previous_cache_write_tokens

    turn.last_raw_cache_read_tokens = raw_cache_read_tokens
    turn.last_raw_cache_write_tokens = raw_cache_write_tokens

    normalized = dict(usage_summary)
    normalized["cache_read_tokens"] = cache_read_tokens
    normalized["cache_write_tokens"] = cache_write_tokens
    normalized["cache_total_tokens"] = cache_read_tokens + cache_write_tokens
    return normalized


class TraceManager:
    def __init__(
        self,
        runtime: OTelRuntime,
        metrics: MetricManager,
        logs: LogManager,
        config: HermesOtelPluginConfig,
        logger: logging.Logger | None = None,
        lineage: SessionLineageResolver | None = None,
        session_metadata: SessionMetadataResolver | None = None,
        session_prompt: SessionPromptResolver | None = None,
    ) -> None:
        self._runtime = runtime
        self._metrics = metrics
        self._logs = logs
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._store = TurnStore()
        self._lineage = lineage or SessionLineageResolver()
        self._session_metadata = session_metadata or SessionMetadataResolver()
        self._session_prompt = session_prompt or SessionPromptResolver()

    def is_child_session(self, session_id: str | None) -> bool:
        return self._lineage.is_child_session(session_id)

    def parent_session_id(self, session_id: str | None) -> str | None:
        return self._lineage.get_parent_session_id(session_id)

    def _cleanup_expired_turns(self) -> None:
        for turn in self._store.expire(self._config.root_span_ttl_ms):
            self._finalize_turn_state(turn, outcome="expired", assistant_response=None)

    def _mark_turn_activity(self, turn: TurnState) -> None:
        turn.last_activity_monotonic_ns = _mono_ns()

    def _derive_llm_tool_context_preview(self, turn: TurnState) -> str | None:
        if not turn.tool_context_since_last_llm:
            return None
        return _clip(" | ".join(turn.tool_context_since_last_llm), limit=1200)

    def _finalize_pending_llm(self, turn: TurnState, assistant_response: str | None = None) -> None:
        pending = turn.pending_llm
        if pending is None:
            return
        attrs: dict[str, Any] = {}
        output_kind = pending.output_kind
        output_preview = None
        if pending.tool_names:
            output_kind = "tool_call"
            output_preview = _tool_call_preview(pending.tool_names)
        elif assistant_response is not None:
            output_kind = output_kind or "text"
            output_preview = _clip(assistant_response, limit=1200)
        if output_kind is not None:
            attrs["output_kind"] = output_kind
        if output_preview is not None:
            attrs["output_preview"] = output_preview
        if attrs:
            self._runtime.set_span_attributes(pending.span, attrs)
        self._runtime.end_span(pending.span, end_time_ns=pending.end_time_ns, status_code="OK")
        turn.pending_llm = None

    def _turn_attrs(
        self,
        turn: TurnState,
    ) -> dict[str, Any]:
        return {
            "agent_runtime": AGENT_RUNTIME,
            "agent_version": AGENT_VERSION,
            "span_kind": _resolve_span_kind("hermes_request"),
            "session_id": turn.session_id,
            "session_key": turn.session_key,
            "session_namespace": turn.session_namespace,
            "session_agent": turn.session_agent,
            "session_channel": turn.session_channel,
            "session_scope": turn.session_scope,
            "session_channel_target": turn.session_channel_target,
            "session_create_at": turn.session_create_at,
            "session_updated_at": turn.session_updated_at,
            "session_chat_type": turn.session_chat_type,
            "session_file": turn.session_file,
            "platform": turn.platform,
            "request_model": turn.model,
            "input_length": len(turn.user_message or ""),
            "input_preview": _clip(turn.user_message),
            "conversation_length": turn.conversation_length,
            "is_first_turn": bool(turn.is_first_turn),
            "request_type": turn.request_type,
            "is_auto_review": turn.is_auto_review,
            "review_category": turn.review_category,
        }

    def _create_turn_state(
        self,
        session_id: str,
        platform: str,
        model: str,
        user_message: str,
        conversation_history: list[Any],
        is_first_turn: bool,
    ) -> TurnState:
        started_at_ns = _wall_ns()
        started_monotonic_ns = _mono_ns()
        classification = _detect_turn_classification(user_message)
        metadata = self._session_metadata.get_metadata(session_id)
        turn = TurnState(
            session_id=session_id,
            platform=platform,
            model=model,
            user_message=user_message,
            conversation_length=len(conversation_history),
            is_first_turn=is_first_turn,
            root_span=None,
            agent_span=None,
            started_at_ns=started_at_ns,
            started_monotonic_ns=started_monotonic_ns,
            last_activity_monotonic_ns=started_monotonic_ns,
            request_type=str(classification.get("request_type") or "user_request"),
            is_auto_review=bool(classification.get("is_auto_review")),
            review_category=classification.get("review_category"),
            session_key=metadata.session_key if metadata else None,
            session_namespace=metadata.session_namespace if metadata else None,
            session_agent=metadata.session_agent if metadata else None,
            session_channel=metadata.session_channel if metadata else None,
            session_scope=metadata.session_scope if metadata else None,
            session_channel_target=metadata.session_channel_target if metadata else None,
            session_create_at=metadata.session_create_at if metadata else None,
            session_updated_at=metadata.session_updated_at if metadata else None,
            session_chat_type=metadata.session_chat_type if metadata else None,
            session_file=metadata.session_file if metadata else None,
        )
        root_span = self._runtime.start_span("hermes_request", start_time_ns=started_at_ns)
        self._runtime.set_span_attributes(
            root_span,
            self._turn_attrs(turn),
        )
        agent_span = self._runtime.start_span(
            "agent_run",
            parent_span=root_span,
            start_time_ns=started_at_ns,
        )
        self._runtime.set_span_attributes(
            agent_span,
            {
                "agent_runtime": AGENT_RUNTIME,
                "agent_version": AGENT_VERSION,
                "span_kind": _resolve_span_kind("agent_run"),
                "session_id": session_id,
                "session_key": turn.session_key,
                "session_namespace": turn.session_namespace,
                "session_agent": turn.session_agent,
                "session_channel": turn.session_channel,
                "session_scope": turn.session_scope,
                "session_channel_target": turn.session_channel_target,
                "session_create_at": turn.session_create_at,
                "session_updated_at": turn.session_updated_at,
                "session_chat_type": turn.session_chat_type,
                "session_file": turn.session_file,
                "platform": platform,
                "request_model": model,
                **classification,
            },
        )
        turn.root_span = root_span
        turn.agent_span = agent_span
        return turn

    def _ensure_turn(
        self,
        session_id: str,
        platform: str,
        model: str,
        user_message: str = "",
        conversation_history: list[Any] | None = None,
        is_first_turn: bool = False,
    ) -> TurnState:
        self._cleanup_expired_turns()
        turn = self._store.get_turn(session_id)
        if turn is not None:
            self._mark_turn_activity(turn)
            return turn
        synthetic = self._create_turn_state(
            session_id=session_id,
            platform=platform,
            model=model,
            user_message=user_message,
            conversation_history=conversation_history or [],
            is_first_turn=is_first_turn,
        )
        self._store.replace_turn(session_id, synthetic)
        return synthetic

    def start_turn(
        self,
        session_id: str,
        user_message: str,
        conversation_history: list[Any],
        is_first_turn: bool,
        model: str,
        platform: str,
        **_: Any,
    ) -> None:
        if self.is_child_session(session_id):
            return
        self._cleanup_expired_turns()
        new_state = self._create_turn_state(
            session_id=session_id,
            platform=platform,
            model=model,
            user_message=user_message,
            conversation_history=conversation_history,
            is_first_turn=is_first_turn,
        )
        previous = self._store.replace_turn(session_id, new_state)
        if previous is not None:
            self._finalize_turn_state(previous, outcome="superseded", assistant_response=None)
        self._metrics.record_turn_started(session_id, platform, model, request_type=new_state.request_type)

    def finish_turn(
        self,
        session_id: str,
        assistant_response: str | None = None,
        completed: bool = True,
        interrupted: bool = False,
        platform: str | None = None,
        **_: Any,
    ) -> None:
        if self.is_child_session(session_id):
            return
        turn = self._store.pop_turn(session_id)
        if turn is None:
            return
        self._mark_turn_activity(turn)
        outcome = "completed"
        if interrupted:
            outcome = "interrupted"
        elif not completed:
            outcome = "failed"
        self._finalize_turn_state(turn, outcome=outcome, assistant_response=assistant_response, platform=platform)

    def finalize_session(self, session_id: str | None, platform: str, outcome: str) -> None:
        if not session_id:
            return
        if self.is_child_session(session_id):
            return
        turn = self._store.pop_turn(session_id)
        if turn is None:
            return
        self._mark_turn_activity(turn)
        self._finalize_turn_state(turn, outcome=outcome, assistant_response=None, platform=platform)

    def start_api_request(
        self,
        session_id: str,
        platform: str,
        model: str,
        provider: str,
        api_call_count: int,
        api_mode: str | None = None,
        approx_input_tokens: int | None = None,
        request_char_count: int | None = None,
        max_tokens: int | None = None,
        request_messages: list[Any] | None = None,
        message_count: int | None = None,
        tool_count: int | None = None,
        **_: Any,
    ) -> None:
        if self.is_child_session(session_id):
            return
        turn = self._ensure_turn(session_id=session_id, platform=platform, model=model)
        self._mark_turn_activity(turn)
        self._finalize_pending_llm(turn)
        key = str(api_call_count)
        started_at_ns = _wall_ns()
        active_skill_names = sorted(turn.active_skills)
        request_span = self._runtime.start_span(
            "llm",
            parent_span=turn.agent_span,
            start_time_ns=started_at_ns,
        )
        attrs = {
            "agent_runtime": AGENT_RUNTIME,
            "agent_version": AGENT_VERSION,
            "span_kind": _resolve_span_kind("llm"),
            "session_id": session_id,
            "platform": platform,
            "provider_name": provider,
            "request_model": model,
            "api_mode": api_mode,
            "api_call_count": api_call_count,
            "input_length": request_char_count,
            "max_tokens": max_tokens,
            "request_message_count": message_count,
            "request_tool_count": tool_count,
            "skill_count": len(active_skill_names) or None,
            "skills": ",".join(active_skill_names) if active_skill_names else None,
        }
        if request_messages is not None:
            payload_chars, payload_bytes = _json_size(request_messages)
            attrs["request_payload_item_count"] = len(request_messages)
            attrs["request_payload_chars"] = payload_chars
            attrs["request_payload_bytes"] = payload_bytes
            if turn.request_user_prompt_estimated_tokens is None:
                user_prompt_tokens = _request_user_prompt_stats(request_messages)
                turn.request_user_prompt_estimated_tokens = user_prompt_tokens
                parent_attrs = {
                    "request_user_prompt_estimated_tokens": user_prompt_tokens,
                }
                self._runtime.set_span_attributes(turn.root_span, parent_attrs)
                self._runtime.set_span_attributes(turn.agent_span, parent_attrs)
        prompt_diagnostics = self._session_prompt.get_prompt_diagnostics(session_id)
        if prompt_diagnostics is not None:
            attrs["system_prompt_chars"] = prompt_diagnostics.system_prompt_chars
            attrs["system_prompt_bytes"] = prompt_diagnostics.system_prompt_bytes
            attrs["system_prompt_hash"] = prompt_diagnostics.system_prompt_hash
        if api_call_count == 1:
            attrs["input_preview"] = _clip(turn.user_message, limit=1200)
        tool_context_preview = self._derive_llm_tool_context_preview(turn)
        if tool_context_preview is not None:
            attrs["tool_context_preview"] = tool_context_preview
        if approx_input_tokens is not None:
            attrs["approx_input_tokens"] = approx_input_tokens
        self._runtime.set_span_attributes(request_span, attrs)
        turn.tool_context_since_last_llm.clear()
        turn.active_requests[key] = ActiveSpanState(
            key=key,
            span=request_span,
            started_at_ns=started_at_ns,
            started_monotonic_ns=_mono_ns(),
            attrs=attrs,
        )
        turn.provider_name = provider
        turn.request_model = model

    def finish_api_request(
        self,
        session_id: str,
        platform: str,
        model: str,
        provider: str,
        api_call_count: int,
        api_duration: float | None = None,
        finish_reason: str | None = None,
        response_model: str | None = None,
        usage: dict[str, Any] | None = None,
        assistant_content_chars: int | None = None,
        assistant_tool_call_count: int | None = None,
        **_: Any,
    ) -> None:
        if self.is_child_session(session_id):
            return
        turn = self._store.get_turn(session_id)
        if turn is None:
            return
        self._mark_turn_activity(turn)
        key = str(api_call_count)
        active = turn.active_requests.pop(key, None)
        if active is None:
            return
        duration_ms = (api_duration or 0.0) * 1000.0
        outcome = "error" if finish_reason in {"error", "length"} else "completed"
        usage_summary = _normalize_cache_usage_for_turn(turn, _normalize_usage(usage))
        resolved_response_model = response_model or model
        attrs = {
            "finish_reason": finish_reason,
            "response_model": resolved_response_model,
            "output_length": assistant_content_chars,
            "assistant_tool_call_count": assistant_tool_call_count,
            **_usage_attrs(usage_summary),
        }
        self._runtime.set_span_attributes(active.span, attrs)
        end_time_ns = _wall_ns()
        if outcome == "error":
            self._runtime.end_span(
                active.span,
                end_time_ns=end_time_ns,
                status_code="ERROR",
                description=finish_reason or "",
            )
        else:
            turn.pending_llm = PendingLlmState(
                span=active.span,
                end_time_ns=end_time_ns,
                output_kind="tool_call" if (assistant_tool_call_count or 0) > 0 else "text",
            )
        turn.response_model = resolved_response_model
        turn.aggregate_input_tokens += usage_summary["input_tokens"]
        turn.aggregate_output_tokens += usage_summary["output_tokens"]
        turn.aggregate_cache_read_tokens += usage_summary["cache_read_tokens"]
        turn.aggregate_cache_write_tokens += usage_summary["cache_write_tokens"]
        turn.aggregate_reasoning_tokens += usage_summary["reasoning_tokens"]
        self._metrics.record_api_request(
            session_id=session_id,
            platform=platform,
            request_model=model,
            provider_name=provider,
            response_model=resolved_response_model,
            duration_ms=duration_ms,
            outcome=outcome,
            usage=usage,
        )
        self._logs.emit_api_request(
            "Hermes model request finished",
            {
                "session_id": session_id,
                "platform": platform,
                "provider_name": provider,
                "request_model": model,
                "response_model": resolved_response_model,
                "finish_reason": finish_reason,
                **_usage_attrs(usage_summary),
            },
        )

    def start_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        task_id: str | None = None,
        session_id: str | None = None,
        platform: str | None = None,
        model: str | None = None,
        tool_call_id: str | None = None,
        **_: Any,
    ) -> None:
        resolved_session_id = session_id or task_id or ""
        if not resolved_session_id:
            return
        if self.is_child_session(resolved_session_id):
            return
        turn = self._ensure_turn(
            session_id=resolved_session_id,
            platform=platform or "unknown",
            model=model or "unknown",
        )
        self._mark_turn_activity(turn)
        if turn.pending_llm is not None:
            turn.pending_llm.tool_names.append(tool_name)
        args_preview = _normalized_tool_args_preview(args)
        if tool_call_id:
            for key, item in list(turn.active_tools.items()):
                if (
                    item.attrs.get("tool_name") == tool_name
                    and item.attrs.get("tool_args_preview") == args_preview
                    and not item.attrs.get("tool_call_id")
                ):
                    item.key = tool_call_id
                    item.attrs["tool_call_id"] = tool_call_id
                    self._runtime.set_span_attributes(item.span, {"tool_call_id": tool_call_id})
                    turn.active_tools[tool_call_id] = item
                    if key != tool_call_id:
                        turn.active_tools.pop(key, None)
                    return
        if not tool_call_id:
            for item in turn.active_tools.values():
                if (
                    item.attrs.get("tool_name") == tool_name
                    and item.attrs.get("tool_args_preview") == args_preview
                ):
                    return
        key = self._store.allocate_tool_key(resolved_session_id, tool_call_id)
        if key is None:
            return
        started_at_ns = _wall_ns()
        span = self._runtime.start_span(
            f"tool:{tool_name}",
            parent_span=turn.agent_span,
            start_time_ns=started_at_ns,
        )
        attrs = {
            "agent_runtime": AGENT_RUNTIME,
            "agent_version": AGENT_VERSION,
            "span_kind": _resolve_span_kind(f"tool:{tool_name}"),
            "session_id": resolved_session_id,
            "platform": turn.platform,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "tool_phase": "call",
            "tool_arg_keys": ",".join(sorted(args.keys())),
            "tool_args_preview": args_preview,
            "tool_target": _extract_tool_target(tool_name, args),
            "tool_command": _extract_tool_command(tool_name, args),
            "skill_name": _extract_tool_skill_name(tool_name, args, None),
        }
        self._runtime.set_span_attributes(span, attrs)
        turn.active_tools[key] = ActiveSpanState(
            key=key,
            span=span,
            started_at_ns=started_at_ns,
            started_monotonic_ns=_mono_ns(),
            attrs=attrs,
        )
        if tool_name == "delegate_task":
            turn.last_delegate_tool_span = span
            delegate_profile = args.get("profile")
            if isinstance(delegate_profile, str) and delegate_profile.strip():
                turn.last_delegate_profile = delegate_profile.strip()

    def finish_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: str,
        task_id: str | None = None,
        session_id: str | None = None,
        platform: str | None = None,
        tool_call_id: str | None = None,
        **_: Any,
    ) -> None:
        resolved_session_id = session_id or task_id or ""
        if not resolved_session_id:
            return
        if self.is_child_session(resolved_session_id):
            return
        turn = self._store.get_turn(resolved_session_id)
        if turn is None:
            return
        self._mark_turn_activity(turn)
        active_key = tool_call_id or None
        active = turn.active_tools.pop(active_key, None) if active_key else None
        if active is None:
            matching_keys = [
                key
                for key, item in turn.active_tools.items()
                if item.attrs.get("tool_name") == tool_name
            ]
            if matching_keys:
                active_key = matching_keys[-1]
                active = turn.active_tools.pop(active_key, None)
        if active is None and turn.active_tools:
            active_key = next(reversed(turn.active_tools))
            active = turn.active_tools.pop(active_key, None)
        if active is None:
            return
        duration_ms = max(0.0, (_mono_ns() - active.started_monotonic_ns) / 1_000_000.0)
        parsed: Any = None
        try:
            parsed = json.loads(result)
        except Exception:
            parsed = result
        outcome = _resolve_tool_outcome(tool_name, parsed)
        result_status = _extract_tool_result_status(parsed)
        skill_name = _extract_tool_skill_name(tool_name, args, parsed)
        self._runtime.set_span_attributes(
            active.span,
            {
                "tool_phase": "result",
                "tool_outcome": outcome,
                "tool_result_status": result_status,
                "tool_result_preview": _json_preview(parsed),
                "skill_name": skill_name,
            },
        )
        self._runtime.end_span(
            active.span,
            status_code="ERROR" if outcome == "error" else "OK",
            description=outcome,
        )
        self._metrics.record_tool_call(
            session_id=resolved_session_id,
            platform=platform or turn.platform,
            tool_name=tool_name,
            duration_ms=duration_ms,
            outcome=outcome,
            result_status=result_status,
            skill_name=skill_name,
            model_name=turn.response_model or turn.request_model or turn.model,
        )
        self._logs.emit_tool(
            "Hermes tool call finished",
            {
                "session_id": resolved_session_id,
                "platform": platform or turn.platform,
                "tool_name": tool_name,
                "tool_outcome": outcome,
                "tool_result_status": result_status,
                "tool_args_preview": _json_preview(args),
                "tool_result_preview": _json_preview(parsed),
            },
        )
        tool_context = _clip(
            f"{tool_name}: {(_json_preview(parsed, limit=180) or outcome)}",
            limit=320,
        )
        if tool_context:
            turn.tool_context_since_last_llm.append(tool_context)
            if len(turn.tool_context_since_last_llm) > 6:
                turn.tool_context_since_last_llm = turn.tool_context_since_last_llm[-6:]
        self._emit_skill_span(
            turn=turn,
            tool_name=tool_name,
            parsed=parsed,
            outcome=outcome,
            loaded_at_ns=_wall_ns(),
            source_attrs=active.attrs,
        )

    def _emit_skill_span(
        self,
        turn: TurnState,
        tool_name: str,
        parsed: Any,
        outcome: str,
        loaded_at_ns: int,
        source_attrs: dict[str, Any],
    ) -> None:
        if tool_name != "skill_view" or outcome != "completed" or not isinstance(parsed, dict):
            return
        skill_name = str(parsed.get("name") or "").strip()
        if not skill_name:
            return
        previous = turn.active_skills.pop(skill_name, None)
        if previous is not None:
            self._runtime.end_span(previous.span, end_time_ns=loaded_at_ns, status_code="OK", description="reloaded")
            self._metrics.record_skill_operation(
                session_id=turn.session_id,
                skill_name=str(previous.attrs.get("skill_name") or skill_name),
                skill_source=str(previous.attrs.get("skill_source") or "runtime"),
                duration_ms=max(0.0, (_mono_ns() - previous.started_monotonic_ns) / 1_000_000.0),
                outcome="completed",
            )
        span = self._runtime.start_span(
            f"skill:{skill_name}",
            parent_span=turn.agent_span,
            start_time_ns=loaded_at_ns,
        )
        content = parsed.get("content")
        description = parsed.get("description")
        tags = parsed.get("tags")
        related_skills = parsed.get("related_skills")
        self._runtime.set_span_attributes(
            span,
            {
                "agent_runtime": AGENT_RUNTIME,
                "agent_version": AGENT_VERSION,
                "span_kind": _resolve_span_kind(f"skill:{skill_name}"),
                "session_id": turn.session_id,
                "platform": turn.platform,
                "skill_name": skill_name,
                "skill_source": "runtime",
                "skill_description": _clip(description),
                "skill_content_length": len(content) if isinstance(content, str) else None,
                "skill_source_tool_call_id": source_attrs.get("tool_call_id"),
                "skill_related_skills": (
                    ",".join(str(item).strip() for item in related_skills if str(item).strip())
                    if isinstance(related_skills, list)
                    else None
                ),
                "skill_tags": (
                    ",".join(str(item).strip() for item in tags if str(item).strip())
                    if isinstance(tags, list)
                    else None
                ),
            },
        )
        turn.active_skills[skill_name] = ActiveSpanState(
            key=skill_name,
            span=span,
            started_at_ns=loaded_at_ns,
            started_monotonic_ns=_mono_ns(),
            attrs={"skill_name": skill_name, "skill_source": "runtime"},
        )
        self._metrics.record_skill_activation(
            session_id=turn.session_id,
            skill_name=skill_name,
            skill_source="runtime",
        )

    def _close_active_skills(self, turn: TurnState, end_time_ns: int, reason: str) -> None:
        if not turn.active_skills:
            return
        status_code = "OK"
        if reason in {"failed", "expired"}:
            status_code = "ERROR"
        elif reason in {"interrupted", "superseded", "reset"}:
            status_code = "UNSET"
        for active in list(turn.active_skills.values()):
            self._runtime.end_span(
                active.span,
                end_time_ns=end_time_ns,
                status_code=status_code,
                description=reason,
            )
            self._metrics.record_skill_operation(
                session_id=turn.session_id,
                skill_name=str(active.attrs.get("skill_name") or active.key),
                skill_source=str(active.attrs.get("skill_source") or "runtime"),
                duration_ms=max(0.0, (_mono_ns() - active.started_monotonic_ns) / 1_000_000.0),
                outcome=_normalize_skill_metric_outcome(reason),
            )
        turn.active_skills.clear()

    def record_subagent_stop(
        self,
        parent_session_id: str,
        child_role: str | None,
        child_summary: str | None,
        child_status: str,
        duration_ms: int,
        **_: Any,
    ) -> None:
        turn = self._store.get_turn(parent_session_id)
        parent_span = None
        runtime_role = child_role or "default"
        display_role = runtime_role
        if turn is not None:
            self._mark_turn_activity(turn)
            parent_span = turn.last_delegate_tool_span or turn.agent_span
            if runtime_role in {"leaf", "default"} and turn.last_delegate_profile:
                display_role = turn.last_delegate_profile
        end_ns = _wall_ns()
        start_ns = end_ns - max(0, int(duration_ms)) * 1_000_000
        span = self._runtime.start_span(
            f"subagent:{display_role}",
            parent_span=parent_span,
            start_time_ns=start_ns,
        )
        self._runtime.set_span_attributes(
            span,
            {
                "agent_runtime": AGENT_RUNTIME,
                "agent_version": AGENT_VERSION,
                "span_kind": _resolve_span_kind(f"subagent:{display_role}"),
                "session_id": parent_session_id,
                "subagent_role": display_role,
                "subagent_runtime_role": runtime_role if runtime_role != display_role else None,
                "outcome": child_status,
                "output_preview": _clip(child_summary),
                "output_length": len(child_summary or ""),
            },
        )
        self._runtime.end_span(
            span,
            end_time_ns=end_ns,
            status_code="ERROR" if child_status not in {"completed", "ok"} else "OK",
            description=child_status,
        )
        self._metrics.record_subagent(
            session_id=parent_session_id,
            child_role=display_role,
            duration_ms=float(duration_ms),
            outcome=child_status,
        )
        self._logs.emit_subagent(
            "Hermes subagent finished",
            {
                "session_id": parent_session_id,
                "subagent_role": display_role,
                "subagent_runtime_role": runtime_role if runtime_role != display_role else None,
                "outcome": child_status,
                "output_preview": _clip(child_summary),
            },
        )

    def _finalize_turn_state(
        self,
        turn: TurnState,
        outcome: str,
        assistant_response: str | None,
        platform: str | None = None,
    ) -> None:
        for active in list(turn.active_requests.values()):
            self._runtime.end_span(active.span, status_code="ERROR", description="orphaned_request")
        for active in list(turn.active_tools.values()):
            self._runtime.end_span(active.span, status_code="ERROR", description="orphaned_tool")
        self._finalize_pending_llm(turn, assistant_response=assistant_response)
        self._close_active_skills(turn, end_time_ns=_wall_ns(), reason=outcome)
        duration_ms = max(0.0, (_mono_ns() - turn.started_monotonic_ns) / 1_000_000.0)
        final_platform = platform or turn.platform
        output_length = len(assistant_response or "")
        output_preview = _clip(assistant_response)
        aggregate_total_tokens = turn.aggregate_input_tokens + turn.aggregate_output_tokens
        aggregate_usage = {
            "input_tokens": turn.aggregate_input_tokens,
            "output_tokens": turn.aggregate_output_tokens,
            "total_tokens": aggregate_total_tokens,
            "cache_read_tokens": turn.aggregate_cache_read_tokens,
            "cache_write_tokens": turn.aggregate_cache_write_tokens,
            "cache_total_tokens": turn.aggregate_cache_read_tokens + turn.aggregate_cache_write_tokens,
            "reasoning_tokens": turn.aggregate_reasoning_tokens,
        }
        resolved_response_model = turn.response_model or turn.request_model or turn.model
        final_attrs = {
            "final_status": outcome,
            "response_model": resolved_response_model,
            "provider_name": turn.provider_name,
            "output_length": output_length if assistant_response is not None else None,
            "output_preview": output_preview,
            **_usage_attrs(aggregate_usage),
        }
        self._runtime.set_span_attributes(turn.agent_span, final_attrs)
        self._runtime.set_span_attributes(turn.root_span, final_attrs)
        status_code = "ERROR" if outcome in {"failed", "expired"} else "OK"
        if outcome in {"interrupted", "superseded", "reset"}:
            status_code = "UNSET"
        self._runtime.end_span(turn.agent_span, status_code=status_code, description=outcome)
        self._runtime.end_span(turn.root_span, status_code=status_code, description=outcome)
        self._metrics.record_turn_finished(
            session_id=turn.session_id,
            platform=final_platform,
            model=turn.model,
            provider_name=turn.provider_name,
            response_model=resolved_response_model,
            request_type=turn.request_type,
            review_category=turn.review_category,
            session_state=outcome,
            usage=aggregate_usage,
            outcome=outcome,
            duration_ms=duration_ms,
        )
        if outcome == "interrupted":
            self._metrics.record_interrupted_turn(turn.session_id, final_platform)
        self._logs.emit_session_event(
            "Hermes turn finished",
            session_id=turn.session_id,
            platform=final_platform,
            outcome=outcome,
        )
