# Metrics

[中文版本](../zh/metrics.md)

## Overview

`hermes-otel-plugin` emits four OpenTelemetry GenAI-aligned metrics:

- `gen_ai.workflow.duration`
- `gen_ai.agent.operation.count`
- `gen_ai.client.operation.duration`
- `gen_ai.client.token.usage`

The plugin no longer emits legacy `gen_ai.agent.*` or `gen_ai.runtime.*` metrics. Request-level, tool-level, and skill-level durations are represented by the standard workflow/client operation metrics, and token metrics are emitted only from model-call usage.

## Metric List

| metric | type | unit | tags | description |
| --- | --- | --- | --- | --- |
| `gen_ai.workflow.duration` | Histogram | `s` | `session_id`, `gen_ai.conversation.id`, `gen_ai.operation.name` | Duration of a Hermes agent workflow. `gen_ai.operation.name` is `invoke_agent`. |
| `gen_ai.agent.operation.count` | Sum | Operation-specific tags plus `status` | Count of model, tool, and skill operations. Each emitted data point has value `1`. |
| `gen_ai.client.operation.duration` | Histogram | `s` | Common tags plus operation-specific tags | Duration of model, tool, and skill client operations. |
| `gen_ai.client.token.usage` | Histogram | `{token}` | Common tags plus `gen_ai.token.type` | Model-call token usage. Only `input` and `output` token types are emitted. |

## Common Tags

| tag | applies to | description |
| --- | --- | --- |
| `session_id` | all metrics | Hermes session id. |
| `gen_ai.conversation.id` | all metrics | Mirrors `session_id` for OpenTelemetry GenAI correlation. |
| `gen_ai.operation.name` | all metrics | `invoke_agent`, `chat`, `execute_tool`, or `skill`. |
| `gen_ai.provider.name` | model operation and token metrics | Model provider. |
| `gen_ai.request.model` | model operation and token metrics | Requested model. |
| `gen_ai.response.model` | model operation and token metrics | Response model when available. |
| `server.address` | model operation and token metrics | Provider endpoint host when available. |
| `server.port` | model operation and token metrics | Provider endpoint port when available. |
| `error.type` | model operation metrics | Low-cardinality error type when a model call fails. |
| `gen_ai.tool.name` | tool operation metrics | Tool name. |
| `status` | operation count metrics | Normalized result dimension. Operation count uses `ok` or `error`. |
| `tool_result_status` | tool operation metrics | Tool result status extracted from Hermes tool output. |
| `gen.ai.skill.name` | skill operation metrics | Skill name. |
| `gen_ai.token.type` | token metrics | `input` or `output`. |

`host` and `host.name` are expected to be supplied as OTLP resource attributes by the runtime/exporter configuration instead of per-point metric tags.

## Operation Semantics

### Workflow

`gen_ai.workflow.duration` is recorded when an `invoke_agent` workflow finishes.

Workflow metrics intentionally do not carry model, usage, tool, request type, or aggregated token tags. This keeps workflow latency stable and avoids mixing per-model dimensions into a request-level metric.

### Model Calls

Model calls record:

- `gen_ai.agent.operation.count` with `gen_ai.operation.name=chat` and `status=ok|error`
- `gen_ai.client.operation.duration` with `gen_ai.operation.name=chat`
- `gen_ai.client.token.usage` with `gen_ai.token.type=input`
- `gen_ai.client.token.usage` with `gen_ai.token.type=output`

Cache, total, and reasoning token values remain available as trace attributes on `llm` spans when Hermes can observe them, but they are not emitted as metrics.

### Tool and Skill Calls

Tool calls record `gen_ai.client.operation.duration` with:

- `gen_ai.agent.operation.count` with `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, and `status=ok|error`
- `gen_ai.operation.name=execute_tool`
- `gen_ai.tool.name`
- `tool_result_status` when available

Tool metrics do not carry model tags or token usage tags.

Skill calls are treated as a special tool operation and record `gen_ai.client.operation.duration` with:

- `gen_ai.agent.operation.count` with `gen_ai.operation.name=skill`, `gen.ai.skill.name`, and `status=ok|error`
- `gen_ai.operation.name=skill`
- `gen.ai.skill.name`

Skill metrics do not carry model tags or token usage tags.

## Removed Legacy Metrics

| removed metric | replacement |
| --- | --- |
| `gen_ai.agent.request.count` | Removed; no counter replacement. |
| `gen_ai.agent.request.duration` | `gen_ai.workflow.duration` |
| `gen_ai.agent.operation.duration` | `gen_ai.client.operation.duration` |
| `gen_ai.agent.token.usage` | `gen_ai.client.token.usage` |
| `gen_ai.agent.session.token.*` | Removed; no aggregated usage metric is emitted on `hermes_request` / `invoke_agent`. |
| `gen_ai.agent.skill.activation.count` | Removed; skill duration is represented by `gen_ai.client.operation.duration` with `gen_ai.operation.name=skill`. |
| `gen_ai.agent.subagent.*` | Removed. |
| `gen_ai.runtime.*` | Removed. |

## Migration Notes

- Duration units changed from milliseconds to seconds for workflow and client operation metrics.
- `session_id` is retained and also copied to `gen_ai.conversation.id`.
- `provider_name` moved to `gen_ai.provider.name`.
- `request_model` moved to `gen_ai.request.model`.
- `response_model` moved to `gen_ai.response.model`.
- `operation_name` moved to `gen_ai.operation.name`.
- `gen_ai.agent.operation.count` is retained and uses the normalized `status` tag instead of legacy result tags.
- `tool_name` moved to `gen_ai.tool.name`.
- `token_type` moved to `gen_ai.token.type`.
- `token_type=total`, cache token buckets, and reasoning token buckets are no longer emitted as metrics.
