# OpenTelemetry GenAI Semantic Field Mapping

[中文版本](../zh/semantic-field-mapping.md)

## Overview

This page describes the compatibility mapping from historical Hermes telemetry fields to OpenTelemetry GenAI semantic convention fields.

The current strategy keeps span fields backward-compatible while moving metrics to the standard GenAI metric set. The plugin emits standard `gen_ai.*` / `error.type` fields on spans, but legacy `gen_ai.agent.*` and `gen_ai.runtime.*` metrics have been removed.

## Span Fields

| current field | new standard field | handling | notes |
| --- | --- | --- | --- |
| `session_id` | `gen_ai.conversation.id` | dual-write | Uses the Hermes session id to correlate traces, metrics, and logs |
| `provider_name` | `gen_ai.provider.name` | dual-write | Mirrors the host provider value, such as `openai-codex` |
| `request_model` | `gen_ai.request.model` | dual-write | Requested model |
| `response_model` | `gen_ai.response.model` | dual-write | Response model; falls back to request model when missing |
| provider request messages | `gen_ai.input.messages` | standard | Written as a JSON string on spans from Hermes' sanitized request payload |
| provider assistant message | `gen_ai.output.messages` | standard | Written as a JSON string on spans from Hermes' sanitized response payload |
| provider response id | `gen_ai.response.id` | standard | Emitted when the provider response exposes an id |
| `finish_reason` | `gen_ai.response.finish_reasons` | dual-write | Standard field is an array |
| provider request parameters | `gen_ai.request.*` | standard | Emits observable `choice.count`, `max_tokens`, `temperature`, `top_p`, `top_k`, `frequency_penalty`, `presence_penalty`, `seed`, `stop_sequences`, and `stream` |
| provider system instructions | `gen_ai.system_instructions` | standard | Emitted when instructions are separate from chat history |
| provider tool definitions | `gen_ai.tool.definitions` | standard | Emitted when request payload includes tool definitions |
| `operation_name=model` | `gen_ai.operation.name=chat` | dual-write | Model calls |
| `operation_name=tool` | `gen_ai.operation.name=execute_tool` | dual-write | Tool calls |
| `invoke_agent` span | `gen_ai.operation.name=invoke_agent` | same name | Span name uses the standard operation name |
| `tool_name` | `gen_ai.tool.name` | dual-write | `tool:*` spans and tool operation metrics |
| `tool_call_id` | `gen_ai.tool.call.id` | dual-write | Tool call identifier |
| `tool_args_preview` | `gen_ai.tool.call.arguments` | dual-write | Standard field is written as a JSON string on spans |
| `tool_result_preview` | `gen_ai.tool.call.result` | dual-write | Standard field is written as a JSON string on spans |
| `usage_input_tokens` | `gen_ai.usage.input_tokens` | dual-write | Dual-written on `llm` spans only |
| `usage_output_tokens` | `gen_ai.usage.output_tokens` | dual-write | Dual-written on `llm` spans only |
| `usage_total_tokens` | `gen_ai.usage.total_tokens` | dual-write | Dual-written on `llm` spans only |
| `usage_cache_read_input_tokens` | `gen_ai.usage.cache_read.input_tokens` | dual-write | Dual-written on `llm` spans only; old `gen_ai.usage.cache_read_input_tokens` is also retained |
| `usage_cache_write_input_tokens` | `gen_ai.usage.cache_creation.input_tokens` | dual-write | Dual-written on `llm` spans only; old `gen_ai.usage.cache_write_input_tokens` is also retained |
| `usage_reasoning_tokens` | `gen_ai.usage.reasoning.output_tokens` | dual-write | Dual-written on `llm` spans only; old `gen_ai.usage.reasoning_tokens` is also retained |
| `error_type` / `error_code` | `error.type` | dual-write | `error.type` prefers low-cardinality `error_code`, then `error_type` |

## Metrics

| current metric | new standard metric | handling | notes |
| --- | --- | --- | --- |
| `gen_ai.agent.token.usage` | `gen_ai.client.token.usage` | replaced | Standard metric writes only `gen_ai.token.type=input|output`; total/cache/reasoning buckets are no longer emitted as metrics |
| `gen_ai.agent.operation.duration` with `operation_name=model` | `gen_ai.client.operation.duration` | replaced | Standard metric unit is seconds |
| `gen_ai.agent.operation.duration` with `operation_name=tool` | `gen_ai.client.operation.duration` | replaced | Tool operations use `gen_ai.operation.name=execute_tool` |
| `gen_ai.agent.operation.duration` with `operation_name=skill` | `gen_ai.client.operation.duration` | replaced | Skill operations use `gen_ai.operation.name=skill` and `gen.ai.skill.name` |
| `gen_ai.agent.request.duration` | `gen_ai.workflow.duration` | replaced | Workflow duration uses seconds and `gen_ai.operation.name=invoke_agent` |
| `gen_ai.agent.request.count` | no replacement | removed | No request counter is emitted |
| `gen_ai.agent.operation.count` | retained | retained | Uses normalized `status` for `chat`, `execute_tool`, and `skill` operations |
| `gen_ai.agent.session.token.*` | no replacement | removed | `hermes_request` / `invoke_agent` no longer emit aggregated usage metrics |
| `gen_ai.agent.skill.activation.count` | no direct counter replacement | removed | Skill duration is represented by `gen_ai.client.operation.duration` with `gen_ai.operation.name=skill` |
| `gen_ai.agent.subagent.*` | no replacement | removed | Subagent activity remains visible through spans |
| `gen_ai.runtime.*` | no replacement | removed | Runtime hook counters/histograms are no longer emitted |

## Span Names

| current span | standard semantic expression | handling |
| --- | --- | --- |
| `hermes_request` | `gen_ai.conversation.id` plus request/session extension fields | span name retained |
| `invoke_agent` | `gen_ai.operation.name=invoke_agent` | span name uses the standard operation name |
| `llm` | `gen_ai.operation.name=chat` | span name retained |
| `tool:*` | `gen_ai.operation.name=execute_tool` plus `gen_ai.tool.name` | span name retained |
| `skill:*` | Hermes skill extension semantics | span name retained |
| `subagent:*` | `gen_ai.operation.name=invoke_agent` plus `subagent_role` | span name retained |

## Retained Hermes Extension Fields

The following fields currently have no stable GenAI standard replacement and remain Hermes extension fields:

- `agent_runtime`
- `agent_version`
- `platform`
- `span_kind`
- `request_type`
- `review_category`
- `session_key`
- `session_namespace`
- `session_agent`
- `session_channel`
- `session_scope`
- `session_channel_target`
- `tool_result_status`
- `tool_phase`
- `tool_status`
- `skill.name`
- `skill.description`
- `skill.path`
- `skill.source.type`
- `skill.result_status`
- `skill_call_id`
- `gen_ai.skill.name`
- `gen_ai.skill.description`
- `gen_ai.skill.path`
- `gen_ai.skill.source.type`
- `gen_ai.skill.result_status`
- `gen_ai.skill.version`
- `subagent_role`

Notes:

- `skill.*` is the current compatibility-facing skill field family emitted by the plugin.
- `gen_ai.skill.*` is a project extension set that aligns with the direction of community proposals, but it is not an official OpenTelemetry GenAI standard field set yet.
- `skill_name` and `skill_description` remain available as backward-compatible fields.
