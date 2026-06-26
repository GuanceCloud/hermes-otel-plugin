# Metrics

[中文版本](../zh/metrics.md)

## Overview

This document describes the metrics currently emitted by `hermes-otel-plugin`.

The naming model follows the same product direction as `openclaw-otel-plugin`:

- `gen_ai.client.*`
- `gen_ai.agent.*`
- `gen_ai.runtime.*`

Current boundary:

- `gen_ai.client.*`
  Used for OpenTelemetry GenAI client model-call metrics emitted by this plugin.
  Hermes still keeps the older `gen_ai.agent.*` model metrics during the compatibility transition.
- `gen_ai.agent.*`
  Used for Hermes request, legacy model operation/token metrics, session token, skill, and subagent metrics.
- `gen_ai.runtime.*`
  Used for Hermes runtime process metrics currently observable through plugin hooks.

## Common Tags

### Shared Tags

- `agent_runtime`
- `session_id`
- `platform`
- `provider_name`
- `gen_ai.provider.name`
- `request_model`
- `gen_ai.request.model`
- `response_model`
- `gen_ai.response.model`
- `operation_name`
- `gen_ai.operation.name`
- `token_type`
- `gen_ai.token.type`
- `tool_name`
- `tool_result_status`
- `skill_name`
- `model_name`
- `subagent_role`
- `outcome`
- `session_state`
- `request_type`
- `review_category`

### Tag Notes

| tag | meaning |
| --- | --- |
| `agent_runtime` | Fixed to `hermes` |
| `session_id` | Hermes session id |
| `platform` | Current runtime platform, such as `cli` |
| `provider_name` | Model provider |
| `gen_ai.provider.name` | OpenTelemetry GenAI provider name; currently mirrors `provider_name` |
| `request_model` | Requested model |
| `gen_ai.request.model` | OpenTelemetry GenAI requested model; mirrors `request_model` |
| `response_model` | Response model |
| `gen_ai.response.model` | OpenTelemetry GenAI response model; mirrors `response_model` |
| `operation_name` | Operation category such as `model`, `tool`, `skill`, `subagent` |
| `gen_ai.operation.name` | OpenTelemetry GenAI operation name such as `chat`, `execute_tool`, `invoke_agent` |
| `token_type` | Token bucket such as `input`, `output`, `total` |
| `gen_ai.token.type` | OpenTelemetry GenAI token bucket for client token metrics, currently `input` or `output` |
| `tool_name` | Tool name |
| `tool_result_status` | Explicit status extracted from tool result payload |
| `skill_name` | Skill name |
| `model_name` | Model attribution tag used for tool operation metrics |
| `subagent_role` | Child agent role |
| `outcome` | Normalized outcome such as `completed`, `error`, `failed`, `interrupted` |
| `session_state` | Request/session final state used by request metrics |
| `request_type` | Request classification such as `user_request`, `auto_review` |
| `review_category` | Review subtype, currently `skill` for auto review flows |

## Metric List

### GenAI Client

Hermes emits OpenTelemetry GenAI client metrics for model calls while retaining the older agent metrics.

| metric | type | unit | tags | description |
| --- | --- | --- | --- | --- |
| `gen_ai.client.operation.duration` | Histogram | `s` | `agent_runtime`, `agent_version`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `outcome`, `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.conversation.id`, `error.type`, `server.address`, `server.port` | Model-call duration using OpenTelemetry GenAI client semantics |
| `gen_ai.client.token.usage` | Histogram | `{token}` | `agent_runtime`, `agent_version`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `token_type`, `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.conversation.id`, `gen_ai.token.type` | Model-call input/output token usage using OpenTelemetry GenAI client semantics |

### GenAI Agent

| metric | type | unit | tags | description |
| --- | --- | --- | --- | --- |
| `gen_ai.agent.request.count` | Counter | - | `agent_runtime`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `session_state`, `outcome`, `request_type`, `review_category` | Count of completed Hermes requests |
| `gen_ai.agent.request.duration` | Histogram | `ms` | `agent_runtime`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `session_state`, `outcome`, `request_type`, `review_category` | Total duration of a Hermes request |
| `gen_ai.agent.token.usage` | Histogram | `{token}` | `agent_runtime`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `token_type` | Per-model-call token usage |
| `gen_ai.agent.operation.count` | Counter | - | Base: `agent_runtime`, `session_id`, `outcome`, `operation_name`<br>`operation_name=model`: `platform`, `provider_name`, `request_model`, `response_model`<br>`operation_name=tool`: `platform`, `tool_name`, `skill_name`, `model_name`, `tool_result_status`<br>`operation_name=skill`: `skill_name`<br>`operation_name=subagent`: `subagent_role` | Operation count by model, tool, skill, and subagent |
| `gen_ai.agent.operation.duration` | Histogram | `ms` | Base: `agent_runtime`, `session_id`, `outcome`, `operation_name`<br>`operation_name=model`: `platform`, `provider_name`, `request_model`, `response_model`<br>`operation_name=tool`: `platform`, `tool_name`, `skill_name`, `model_name`, `tool_result_status`<br>`operation_name=skill`: `skill_name`<br>`operation_name=subagent`: `subagent_role` | Operation duration by model, tool, skill, and subagent |
| `gen_ai.agent.session.token.input` | Counter | `{token}` | `agent_runtime`, `session_id`, `provider_name`, `request_model` | Aggregated input tokens written once when a Hermes request finishes |
| `gen_ai.agent.session.token.output` | Counter | `{token}` | `agent_runtime`, `session_id`, `provider_name`, `request_model` | Aggregated output tokens written once when a Hermes request finishes |
| `gen_ai.agent.session.token.total` | Counter | `{token}` | `agent_runtime`, `session_id`, `provider_name`, `request_model` | Aggregated total tokens written once when a Hermes request finishes |
| `gen_ai.agent.session.token.usage` | Counter | `{token}` | `agent_runtime`, `session_id`, `provider_name`, `request_model`, `token_type` | Aggregated session token counters by token type |
| `gen_ai.agent.session.trace.count` | Counter | - | `agent_runtime`, `session_id`, `platform`, `request_model`, `request_type` | Count of traces started by the plugin |
| `gen_ai.agent.skill.activation.count` | Counter | - | `agent_runtime`, `session_id`, `skill_name` | Number of successful skill activations |
| `gen_ai.agent.subagent.count` | Counter | - | `agent_runtime`, `session_id`, `subagent_role`, `outcome`, `operation_name` | Number of observed subagent completions |
| `gen_ai.agent.subagent.duration` | Histogram | `ms` | `agent_runtime`, `session_id`, `subagent_role`, `outcome`, `operation_name` | Duration of observed subagent executions |

### GenAI Runtime

| metric | type | unit | tags | description |
| --- | --- | --- | --- | --- |
| `gen_ai.runtime.tool.call.count` | Counter | - | `agent_runtime`, `session_id`, `platform`, `tool_name`, `skill_name`, `tool_result_status`, `outcome` | Count of observed tool calls |
| `gen_ai.runtime.tool.call.duration` | Histogram | `ms` | `agent_runtime`, `session_id`, `platform`, `tool_name`, `skill_name`, `tool_result_status`, `outcome` | Duration of observed tool calls |
| `gen_ai.runtime.session.start.count` | Counter | - | `agent_runtime`, `session_id`, `platform` | Count of Hermes session start events |
| `gen_ai.runtime.session.end.count` | Counter | - | `agent_runtime`, `session_id`, `platform`, `outcome` | Count of Hermes session end events |
| `gen_ai.runtime.session.reset.count` | Counter | - | `agent_runtime`, `session_id`, `platform` | Count of Hermes session reset events |
| `gen_ai.runtime.turn.interrupted.count` | Counter | - | `agent_runtime`, `session_id`, `platform` | Count of interrupted Hermes requests |

## Current Alignment With OpenClaw

The following parts are already aligned in principle:

- request metrics
- operation metrics
- tool runtime metrics
- session trace count
- session token aggregation
- skill activation metric

The following parts are not currently emitted by Hermes because the plugin hook model does not expose the needed runtime signals:

- `gen_ai.runtime.message.*`
- `gen_ai.runtime.queue.*`
- `gen_ai.runtime.session.stuck.*`
- `gen_ai.runtime.webhook.*`

## Notes

- `gen_ai.agent.token.usage` records per-model-call token usage.
- `gen_ai.client.token.usage` records the same model-call token usage using standard GenAI attributes and only writes `input` / `output` token buckets.
- `gen_ai.agent.session.token.*` records request-aggregated token totals at request finalization time.
- `tool_result_status` remains distinct from normalized `outcome`.
- `request_type=user_request` is the default classification.
- `request_type=auto_review` is used for Hermes automatic review flows, and may also carry `review_category=skill`.
- See [Semantic Field Mapping](semantic-field-mapping.md) for old-to-standard field relationships.
