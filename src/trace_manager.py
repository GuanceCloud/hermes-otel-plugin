from __future__ import annotations

import json
import logging
from pathlib import Path
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


def _attrs(**kwargs: Any) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if value is not None}


def _json_preview(value: Any, limit: int = 240) -> str | None:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except Exception:
        text = str(value)
    return _clip(text, limit=limit)


def _json_attr(value: Any, limit: int = 65536) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
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


def _request_body_from_payload(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {}
    body = request.get("body")
    return body if isinstance(body, dict) else {}


def _first_present_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _coerce_message_list(*values: Any) -> list[Any]:
    for value in values:
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            return [{"role": "user", "content": value}]
    return []


def _content_part_from_text(text: Any) -> dict[str, Any] | None:
    content = _clip(text, limit=12000)
    if content is None:
        return None
    return {"type": "text", "content": content}


def _normalise_content_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        part = _content_part_from_text(content)
        return [part] if part is not None else []
    if isinstance(content, list):
        parts: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, str):
                part = _content_part_from_text(item)
                if part is not None:
                    parts.append(part)
                continue
            if not isinstance(item, dict):
                continue
            part_type = str(item.get("type") or "").strip()
            if part_type in {"text", "input_text", "output_text"}:
                part = _content_part_from_text(item.get("text") or item.get("content"))
                if part is not None:
                    parts.append(part)
            elif part_type in {"image", "image_url", "input_image"}:
                image_url = item.get("image_url") or item.get("url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                parts.append(_attrs(type="image", image_url=_clip(image_url, limit=12000)))
            else:
                parts.append(_attrs(type=part_type or "content", content=_clip(item, limit=12000)))
        return parts
    if isinstance(content, dict):
        if content.get("_multimodal"):
            return _normalise_content_parts(content.get("content") or content.get("text_summary"))
        part = _content_part_from_text(content.get("text") or content.get("content"))
        return [part] if part is not None else []
    part = _content_part_from_text(content)
    return [part] if part is not None else []


def _tool_call_name_and_args(tool_call: Any) -> tuple[str | None, Any]:
    if not isinstance(tool_call, dict):
        return None, None
    function = tool_call.get("function")
    if isinstance(function, dict):
        return function.get("name") or tool_call.get("name"), function.get("arguments")
    return tool_call.get("name"), tool_call.get("arguments") or tool_call.get("args")


def _decode_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except Exception:
        return value


def _normalise_input_message(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    role = _clip(message.get("role"), limit=64) or "user"
    parts = _normalise_content_parts(message.get("content"))
    if role == "tool":
        result = _decode_jsonish(message.get("content"))
        parts = [
            _attrs(
                type="tool_call_response",
                id=_clip(message.get("tool_call_id"), limit=256),
                name=_clip(message.get("name"), limit=256),
                result=result,
            )
        ]
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        name, arguments = _tool_call_name_and_args(tool_call)
        parts.append(
            _attrs(
                type="tool_call",
                id=_clip(tool_call.get("id"), limit=256),
                name=_clip(name, limit=256),
                arguments=_decode_jsonish(arguments),
            )
        )
    if not parts:
        return None
    return {"role": role, "parts": parts}


def _normalise_output_message(message: Any, finish_reason: Any = None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    role = _clip(message.get("role"), limit=64) or "assistant"
    parts = _normalise_content_parts(message.get("content"))
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        name, arguments = _tool_call_name_and_args(tool_call)
        parts.append(
            _attrs(
                type="tool_call",
                id=_clip(tool_call.get("id"), limit=256),
                name=_clip(name, limit=256),
                arguments=_decode_jsonish(arguments),
            )
        )
    if not parts:
        return None
    return _attrs(role=role, parts=parts, finish_reason=_clip(message.get("finish_reason") or finish_reason, limit=128))


def _gen_ai_input_messages_attr(messages: list[Any]) -> str | None:
    normalised = [item for item in (_normalise_input_message(message) for message in messages) if item is not None]
    return _json_attr(normalised) if normalised else None


def _gen_ai_output_messages_attr(messages: list[Any], finish_reason: Any = None) -> str | None:
    normalised = [
        item
        for item in (_normalise_output_message(message, finish_reason=finish_reason) for message in messages)
        if item is not None
    ]
    return _json_attr(normalised) if normalised else None


def _gen_ai_system_instructions_attr(body: dict[str, Any]) -> str | None:
    instructions = body.get("instructions") or body.get("system")
    if instructions is None:
        return None
    if isinstance(instructions, list):
        parts = []
        for item in instructions:
            if isinstance(item, dict):
                parts.extend(_normalise_content_parts(item.get("content") or item.get("text")))
            else:
                part = _content_part_from_text(item)
                if part is not None:
                    parts.append(part)
    else:
        parts = _normalise_content_parts(instructions)
    return _json_attr(parts) if parts else None


def _gen_ai_tool_definitions_attr(body: dict[str, Any]) -> str | None:
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    return _json_attr(tools)


def _gen_ai_request_attrs(body: dict[str, Any]) -> dict[str, Any]:
    response_format = body.get("response_format")
    output_type = None
    if isinstance(response_format, dict):
        output_type = response_format.get("type")
    elif isinstance(response_format, str):
        output_type = response_format
    max_tokens = body.get("max_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    return _attrs(
        **{
            "gen_ai.request.choice.count": _as_optional_int(body.get("n") or body.get("candidate_count")),
            "gen_ai.request.frequency_penalty": body.get("frequency_penalty") if isinstance(body.get("frequency_penalty"), (int, float)) else None,
            "gen_ai.request.max_tokens": _as_optional_int(max_tokens),
            "gen_ai.request.presence_penalty": body.get("presence_penalty") if isinstance(body.get("presence_penalty"), (int, float)) else None,
            "gen_ai.request.seed": _as_optional_int(body.get("seed")),
            "gen_ai.request.temperature": body.get("temperature") if isinstance(body.get("temperature"), (int, float)) else None,
            "gen_ai.request.top_k": body.get("top_k") if isinstance(body.get("top_k"), (int, float)) else None,
            "gen_ai.request.top_p": body.get("top_p") if isinstance(body.get("top_p"), (int, float)) else None,
            "gen_ai.request.stop_sequences": body.get("stop") if isinstance(body.get("stop"), list) else None,
            "gen_ai.request.stream": body.get("stream") if isinstance(body.get("stream"), bool) else None,
            "gen_ai.output.type": _clip(output_type, limit=64),
            "gen_ai.tool.definitions": _gen_ai_tool_definitions_attr(body),
            "gen_ai.system_instructions": _gen_ai_system_instructions_attr(body),
        }
    )


def _assistant_message_from_response(response: Any, assistant_message: Any) -> dict[str, Any]:
    if isinstance(assistant_message, dict):
        return assistant_message
    if isinstance(response, dict):
        nested = response.get("assistant_message")
        if isinstance(nested, dict):
            return nested
        output = response.get("output")
        if isinstance(output, list):
            messages = [item for item in output if isinstance(item, dict) and item.get("role")]
            if messages:
                return messages[-1]
    return {}


def _resolve_span_kind(resource_name: str) -> str:
    normalized = str(resource_name or "").strip().lower()
    if normalized == "hermes_request":
        return "request"
    if normalized in {"agent_run", "invoke_agent"}:
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
    parsed_name = _extract_skill_name_from_payload(args, parsed)
    if isinstance(parsed_name, str) and parsed_name.strip():
        return parsed_name.strip()
    arg_name = args.get("name")
    if isinstance(arg_name, str) and arg_name.strip():
        return arg_name.strip()
    return None


def _payload_mapping_value(payload: Any, *path: str) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value


def _extract_skill_frontmatter(content: Any) -> tuple[dict[str, str], str]:
    if not isinstance(content, str):
        return {}, ""
    text = content.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, content
    metadata: dict[str, str] = {}
    for line in lines[1:end_index]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = _strip_optional_quotes(raw_value.strip())
        if key and value:
            metadata[key] = value
    return metadata, "\n".join(lines[end_index + 1 :])


def _first_markdown_paragraph(content: str) -> str | None:
    if not content:
        return None
    lines = content.splitlines()
    paragraph: list[str] = []
    in_code_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if not paragraph and stripped.startswith("#"):
            continue
        paragraph.append(stripped)
    if not paragraph:
        return None
    return _clip(" ".join(paragraph), limit=2048)


def _extract_skill_name_from_path(skill_path: str | None) -> str | None:
    if not skill_path:
        return None
    candidate = Path(skill_path)
    if candidate.name.lower() == "skill.md":
        return _clip(candidate.parent.name, limit=256)
    return None


def _normalize_skill_source_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"system", "user", "workspace"}:
        return normalized
    return None


def _infer_skill_source_type(skill_path: str | None) -> str | None:
    if not skill_path:
        return None
    normalized = skill_path.replace("\\", "/")
    if "/.codex/skills/.system/" in normalized or normalized.endswith("/.codex/skills/.system/SKILL.md"):
        return "system"
    if "/.codex/skills/" in normalized:
        return "user"
    return "workspace"


def _nearest_package_json_version(skill_path: str | None) -> str | None:
    if not skill_path:
        return None
    try:
        current = Path(skill_path).expanduser()
    except Exception:
        return None
    if current.name.lower() == "skill.md":
        current = current.parent
    for directory in [current, *current.parents]:
        candidate = directory / "package.json"
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        version = payload.get("version") if isinstance(payload, dict) else None
        if isinstance(version, str) and version.strip():
            return _clip(version, limit=256)
    return None


def _extract_skill_name_from_payload(args: dict[str, Any], parsed: Any) -> str | None:
    parsed_path = _extract_skill_path(parsed)
    path_name = _extract_skill_name_from_path(parsed_path)
    if path_name:
        return path_name
    parsed_name = parsed.get("name") if isinstance(parsed, dict) else None
    if isinstance(parsed_name, str) and parsed_name.strip():
        return _clip(parsed_name, limit=256)
    arg_name = args.get("name")
    if isinstance(arg_name, str) and arg_name.strip():
        return _clip(arg_name, limit=256)
    return None


def _extract_skill_path(parsed: Any) -> str | None:
    if not isinstance(parsed, dict):
        return None
    for value in (
        parsed.get("path"),
        parsed.get("skill_path"),
        parsed.get("entry_path"),
        parsed.get("file_path"),
        _payload_mapping_value(parsed, "source", "path"),
        _payload_mapping_value(parsed, "metadata", "path"),
    ):
        if isinstance(value, str) and value.strip():
            return _clip(value, limit=4096)
    return None


def _extract_skill_description(parsed: Any, content: Any) -> str | None:
    frontmatter, body = _extract_skill_frontmatter(content)
    description = parsed.get("description") if isinstance(parsed, dict) else None
    if isinstance(description, str) and description.strip():
        return _clip(description, limit=2048)
    frontmatter_description = frontmatter.get("description")
    if frontmatter_description:
        return _clip(frontmatter_description, limit=2048)
    return _first_markdown_paragraph(body)


def _extract_skill_version(parsed: Any, skill_path: str | None, content: Any) -> str | None:
    if isinstance(parsed, dict):
        for value in (
            parsed.get("version"),
            parsed.get("skill_version"),
            _payload_mapping_value(parsed, "source", "version"),
            _payload_mapping_value(parsed, "metadata", "version"),
            _payload_mapping_value(parsed, "frontmatter", "version"),
        ):
            if isinstance(value, str) and value.strip():
                return _clip(value, limit=256)
    frontmatter, _ = _extract_skill_frontmatter(content)
    frontmatter_version = frontmatter.get("version")
    if frontmatter_version:
        return _clip(frontmatter_version, limit=256)
    return _nearest_package_json_version(skill_path)


def _extract_skill_source_type(parsed: Any, skill_path: str | None) -> str | None:
    explicit = None
    if isinstance(parsed, dict):
        explicit = (
            parsed.get("source_type")
            or parsed.get("source.type")
            or _payload_mapping_value(parsed, "source", "type")
            or _payload_mapping_value(parsed, "metadata", "source_type")
        )
    normalized = _normalize_skill_source_type(explicit)
    if normalized:
        return normalized
    return _infer_skill_source_type(skill_path)


def _skill_trace_attrs(
    tool_name: str,
    args: dict[str, Any],
    parsed: Any,
    tool_call_id: str | None,
    outcome: str | None,
) -> dict[str, Any]:
    if str(tool_name).strip().lower() != "skill_view":
        return {}
    content = parsed.get("content") if isinstance(parsed, dict) else None
    skill_path = _extract_skill_path(parsed)
    skill_name = _extract_skill_name_from_payload(args, parsed)
    skill_description = _extract_skill_description(parsed, content)
    skill_source_type = _extract_skill_source_type(parsed, skill_path)
    skill_version = _extract_skill_version(parsed, skill_path, content)
    result_status = None
    if outcome is not None:
        result_status = "error" if outcome == "error" else "completed"
    attrs = {
        "skill_name": skill_name,
        "skill_description": skill_description,
        "skill.name": skill_name,
        "skill.description": skill_description,
        "skill.path": skill_path,
        "skill_call_id": _clip(tool_call_id, limit=256),
        "skill.source.type": skill_source_type,
        "skill.result_status": result_status,
        "gen_ai.skill.name": skill_name,
        "gen_ai.skill.description": skill_description,
        "gen_ai.skill.path": skill_path,
        "gen_ai.skill.source.type": skill_source_type,
        "gen_ai.skill.result_status": result_status,
        "gen_ai.skill.version": skill_version,
    }
    return {key: value for key, value in attrs.items() if value is not None}


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


def _usage_attrs(usage_summary: dict[str, int], *, include_gen_ai_usage: bool = True) -> dict[str, int]:
    attrs = {
        "usage_input_tokens": usage_summary["input_tokens"],
        "usage_output_tokens": usage_summary["output_tokens"],
        "usage_total_tokens": usage_summary["total_tokens"],
        "usage_cache_read_input_tokens": usage_summary["cache_read_tokens"],
        "usage_cache_write_input_tokens": usage_summary["cache_write_tokens"],
        "usage_cache_total_tokens": usage_summary["cache_total_tokens"],
        "usage_reasoning_tokens": usage_summary["reasoning_tokens"],
    }
    if include_gen_ai_usage:
        attrs.update(
            {
                "gen_ai.usage.input_tokens": usage_summary["input_tokens"],
                "gen_ai.usage.output_tokens": usage_summary["output_tokens"],
                "gen_ai.usage.total_tokens": usage_summary["total_tokens"],
                "gen_ai.usage.cache_read_input_tokens": usage_summary["cache_read_tokens"],
                "gen_ai.usage.cache_write_input_tokens": usage_summary["cache_write_tokens"],
                "gen_ai.usage.reasoning_tokens": usage_summary["reasoning_tokens"],
                "gen_ai.usage.cache_read.input_tokens": usage_summary["cache_read_tokens"],
                "gen_ai.usage.cache_creation.input_tokens": usage_summary["cache_write_tokens"],
                "gen_ai.usage.reasoning.output_tokens": usage_summary["reasoning_tokens"],
            }
        )
    return attrs


def _as_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.isdigit():
            return int(normalized)
    return None


def _api_error_attrs(
    error: Any,
    *,
    status_code: Any = None,
    retry_count: Any = None,
    max_retries: Any = None,
    retryable: Any = None,
    reason: Any = None,
    base_url: Any = None,
) -> dict[str, Any]:
    error_type = None
    error_message = None
    error_code = None
    if isinstance(error, dict):
        error_type = error.get("type")
        error_message = error.get("message")
        error_code = error.get("code")
    elif error is not None:
        error_message = error
    standard_error_type = _clip(error_code) or _clip(error_type) or _clip(reason)
    return {
        "outcome": "error",
        "error_type": _clip(error_type),
        "error_message": _clip(error_message, limit=1200),
        "error_code": _clip(error_code),
        "error.type": standard_error_type,
        "error_reason": _clip(reason),
        "http_status_code": _as_optional_int(status_code),
        "retry_count": _as_optional_int(retry_count),
        "max_retries": _as_optional_int(max_retries),
        "retryable": retryable if isinstance(retryable, bool) else None,
        "base_url": _clip(base_url, limit=512),
    }


def _is_terminal_api_error(
    *,
    retryable: Any = None,
    retry_count: Any = None,
    max_retries: Any = None,
) -> bool:
    if retryable is False:
        return True
    current_retry = _as_optional_int(retry_count)
    retry_limit = _as_optional_int(max_retries)
    if current_retry is not None and retry_limit is not None and current_retry >= retry_limit:
        return True
    return False


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


def _gen_ai_common_attrs(session_id: str) -> dict[str, Any]:
    return {
        "gen_ai.conversation.id": session_id,
    }


def _gen_ai_agent_attrs(session_id: str, model: str | None = None) -> dict[str, Any]:
    attrs = {
        **_gen_ai_common_attrs(session_id),
        "gen_ai.operation.name": "invoke_agent",
    }
    if model is not None:
        attrs["gen_ai.request.model"] = model
    return attrs


def _gen_ai_model_attrs(
    *,
    session_id: str,
    provider_name: str | None = None,
    request_model: str | None = None,
    response_model: str | None = None,
) -> dict[str, Any]:
    return {
        **_gen_ai_common_attrs(session_id),
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": provider_name,
        "gen_ai.request.model": request_model,
        "gen_ai.response.model": response_model,
    }


def _gen_ai_tool_attrs(session_id: str, tool_name: str | None = None) -> dict[str, Any]:
    return {
        **_gen_ai_common_attrs(session_id),
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool_name,
    }


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
            **_gen_ai_common_attrs(turn.session_id),
            "gen_ai.request.model": turn.model,
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
            "invoke_agent",
            parent_span=root_span,
            start_time_ns=started_at_ns,
        )
        self._runtime.set_span_attributes(
            agent_span,
            {
                "agent_runtime": AGENT_RUNTIME,
                "agent_version": AGENT_VERSION,
                "span_kind": _resolve_span_kind("invoke_agent"),
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
                **_gen_ai_agent_attrs(session_id, model),
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
        base_url: str | None = None,
        api_mode: str | None = None,
        approx_input_tokens: int | None = None,
        request_char_count: int | None = None,
        max_tokens: int | None = None,
        request_messages: list[Any] | None = None,
        messages: list[Any] | None = None,
        request: Any = None,
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
            "base_url": base_url,
            "request_model": model,
            **_gen_ai_model_attrs(
                session_id=session_id,
                provider_name=provider,
                request_model=model,
            ),
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
        request_body = _request_body_from_payload(request)
        input_messages = _coerce_message_list(
            request_body.get("messages"),
            request_body.get("input"),
            request_messages,
            messages,
        )
        if input_messages:
            attrs["gen_ai.input.messages"] = _gen_ai_input_messages_attr(input_messages)
        if request_body:
            attrs.update(_gen_ai_request_attrs(request_body))
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
        response: Any = None,
        assistant_message: Any = None,
        assistant_response: Any = None,
        output_messages: list[Any] | None = None,
        response_messages: list[Any] | None = None,
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
        response_payload = response if isinstance(response, dict) else {}
        resolved_response_model = response_model or _clip(response_payload.get("model"), limit=256) or model
        assistant_payload = _assistant_message_from_response(response_payload, assistant_message)
        output_message_candidates = _coerce_message_list(
            output_messages,
            response_messages,
            [assistant_payload] if assistant_payload else None,
            [{"role": "assistant", "content": assistant_response}] if assistant_response is not None else None,
        )
        attrs = {
            "finish_reason": finish_reason,
            "response_model": resolved_response_model,
            **_gen_ai_model_attrs(
                session_id=session_id,
                provider_name=provider,
                request_model=model,
                response_model=resolved_response_model,
            ),
            "output_length": assistant_content_chars,
            "assistant_tool_call_count": assistant_tool_call_count,
            "gen_ai.response.id": _clip(response_payload.get("id"), limit=256),
            "gen_ai.response.finish_reasons": [str(finish_reason)] if finish_reason else None,
            "gen_ai.output.messages": _gen_ai_output_messages_attr(
                output_message_candidates,
                finish_reason=finish_reason,
            ),
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
            error_type=None,
            base_url=active.attrs.get("base_url"),
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

    def record_api_request_error(
        self,
        session_id: str,
        platform: str,
        model: str,
        provider: str,
        api_call_count: int,
        api_duration: float | None = None,
        base_url: str | None = None,
        status_code: int | None = None,
        retry_count: int | None = None,
        max_retries: int | None = None,
        retryable: bool | None = None,
        reason: str | None = None,
        error: Any = None,
        request: Any = None,
        request_messages: list[Any] | None = None,
        messages: list[Any] | None = None,
        **_: Any,
    ) -> None:
        if self.is_child_session(session_id):
            return
        turn = self._ensure_turn(session_id=session_id, platform=platform, model=model)
        self._mark_turn_activity(turn)
        turn.provider_name = provider
        turn.request_model = model
        error_attrs = _api_error_attrs(
            error,
            status_code=status_code,
            retry_count=retry_count,
            max_retries=max_retries,
            retryable=retryable,
            reason=reason,
            base_url=base_url,
        )
        request_body = _request_body_from_payload(request)
        input_messages = _coerce_message_list(
            request_body.get("messages"),
            request_body.get("input"),
            request_messages,
            messages,
        )
        if input_messages:
            error_attrs["gen_ai.input.messages"] = _gen_ai_input_messages_attr(input_messages)
        if request_body:
            error_attrs.update(_gen_ai_request_attrs(request_body))
        terminal_error = _is_terminal_api_error(
            retryable=retryable,
            retry_count=retry_count,
            max_retries=max_retries,
        )
        turn.api_error_seen = True
        turn.api_error_terminal = turn.api_error_terminal or terminal_error

        key = str(api_call_count)
        active = turn.active_requests.pop(key, None)
        if active is not None:
            self._runtime.set_span_attributes(active.span, error_attrs)
            active.attrs.update({key: value for key, value in error_attrs.items() if value is not None})
            self._runtime.end_span(
                active.span,
                status_code="ERROR",
                description=error_attrs.get("error_message") or error_attrs.get("error_type") or "api_request_error",
            )

        if terminal_error:
            parent_attrs = {
                **error_attrs,
                "provider_name": provider,
                "request_model": model,
                **_gen_ai_model_attrs(
                    session_id=session_id,
                    provider_name=provider,
                    request_model=model,
                ),
            }
            self._runtime.set_span_attributes(turn.agent_span, parent_attrs)
            self._runtime.set_span_attributes(turn.root_span, parent_attrs)

        duration_ms = (api_duration or 0.0) * 1000.0
        self._metrics.record_api_request(
            session_id=session_id,
            platform=platform,
            request_model=model,
            provider_name=provider,
            response_model=model,
            duration_ms=duration_ms,
            outcome="error",
            usage=None,
            error_type=error_attrs.get("error.type"),
            base_url=base_url,
        )
        self._logs.emit_api_request(
            "Hermes model request failed",
            {
                "session_id": session_id,
                "platform": platform,
                "provider_name": provider,
                "request_model": model,
                **error_attrs,
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
                    item.attrs["gen_ai.tool.call.id"] = tool_call_id
                    self._runtime.set_span_attributes(
                        item.span,
                        {
                            "tool_call_id": tool_call_id,
                            "gen_ai.tool.call.id": tool_call_id,
                        },
                    )
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
        parent_span = turn.pending_llm.span if turn.pending_llm is not None else turn.agent_span
        span = self._runtime.start_span(
            f"tool:{tool_name}",
            parent_span=parent_span,
            start_time_ns=started_at_ns,
        )
        attrs = {
            "agent_runtime": AGENT_RUNTIME,
            "agent_version": AGENT_VERSION,
            "span_kind": _resolve_span_kind(f"tool:{tool_name}"),
            "session_id": resolved_session_id,
            "platform": turn.platform,
            "tool_name": tool_name,
            **_gen_ai_tool_attrs(resolved_session_id, tool_name),
            "tool_call_id": tool_call_id,
            "gen_ai.tool.call.id": _clip(tool_call_id, limit=256),
            "gen_ai.tool.call.arguments": _json_attr(args),
            "tool_phase": "call",
            "tool_arg_keys": ",".join(sorted(args.keys())),
            "tool_args_preview": args_preview,
            "tool_target": _extract_tool_target(tool_name, args),
            "tool_command": _extract_tool_command(tool_name, args),
            **_skill_trace_attrs(tool_name, args, None, tool_call_id, None),
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
        self._emit_skill_span(
            turn=turn,
            tool_name=tool_name,
            parsed=parsed,
            outcome=outcome,
            loaded_at_ns=_wall_ns(),
            source_span=active.span,
            source_attrs=active.attrs,
        )
        self._runtime.set_span_attributes(
            active.span,
            {
                "tool_phase": "result",
                "tool_outcome": outcome,
                "tool_result_status": result_status,
                "tool_result_preview": _json_preview(parsed),
                "gen_ai.tool.call.result": _json_attr(parsed),
                **_skill_trace_attrs(
                    tool_name,
                    args,
                    parsed,
                    str(active.attrs.get("tool_call_id") or tool_call_id or "").strip() or None,
                    outcome,
                ),
                "error.type": outcome if outcome == "error" else None,
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

    def _emit_skill_span(
        self,
        turn: TurnState,
        tool_name: str,
        parsed: Any,
        outcome: str,
        loaded_at_ns: int,
        source_span: Any,
        source_attrs: dict[str, Any],
    ) -> None:
        if tool_name != "skill_view" or outcome != "completed" or not isinstance(parsed, dict):
            return
        skill_attrs = _skill_trace_attrs(tool_name, {}, parsed, source_attrs.get("tool_call_id"), outcome)
        skill_name = str(skill_attrs.get("skill_name") or "").strip()
        if not skill_name:
            return
        previous = turn.active_skills.pop(skill_name, None)
        if previous is not None:
            self._runtime.end_span(previous.span, end_time_ns=loaded_at_ns, status_code="OK", description="reloaded")
            self._metrics.record_skill_operation(
                session_id=turn.session_id,
                skill_name=str(previous.attrs.get("skill_name") or skill_name),
                duration_ms=max(0.0, (_mono_ns() - previous.started_monotonic_ns) / 1_000_000.0),
                outcome="completed",
            )
        span = self._runtime.start_span(
            f"skill:{skill_name}",
            parent_span=source_span,
            start_time_ns=loaded_at_ns,
        )
        content = parsed.get("content")
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
                **_gen_ai_common_attrs(turn.session_id),
                **skill_attrs,
                "skill_content_length": len(content) if isinstance(content, str) else None,
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
            attrs={"skill_name": skill_name},
        )
        self._metrics.record_skill_activation(
            session_id=turn.session_id,
            skill_name=skill_name,
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
                **_gen_ai_agent_attrs(parent_session_id),
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
        final_outcome = "failed" if outcome == "finalized" and turn.api_error_terminal else outcome
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
            "final_status": final_outcome,
            "response_model": resolved_response_model,
            "provider_name": turn.provider_name,
            **_gen_ai_agent_attrs(turn.session_id, turn.request_model or turn.model),
            "gen_ai.provider.name": turn.provider_name,
            "gen_ai.response.model": resolved_response_model,
            "output_length": output_length if assistant_response is not None else None,
            "output_preview": output_preview,
        }
        self._runtime.set_span_attributes(turn.agent_span, final_attrs)
        self._runtime.set_span_attributes(turn.root_span, final_attrs)
        status_code = "ERROR" if final_outcome in {"failed", "expired"} else "OK"
        if final_outcome in {"interrupted", "superseded", "reset"}:
            status_code = "UNSET"
        self._runtime.end_span(turn.agent_span, status_code=status_code, description=final_outcome)
        self._runtime.end_span(turn.root_span, status_code=status_code, description=final_outcome)
        self._metrics.record_turn_finished(
            session_id=turn.session_id,
            platform=final_platform,
            model=turn.model,
            provider_name=turn.provider_name,
            response_model=resolved_response_model,
            request_type=turn.request_type,
            review_category=turn.review_category,
            session_state=final_outcome,
            usage=aggregate_usage,
            outcome=final_outcome,
            duration_ms=duration_ms,
        )
        if final_outcome == "interrupted":
            self._metrics.record_interrupted_turn(turn.session_id, final_platform)
        self._logs.emit_session_event(
            "Hermes turn finished",
            session_id=turn.session_id,
            platform=final_platform,
            outcome=final_outcome,
        )
