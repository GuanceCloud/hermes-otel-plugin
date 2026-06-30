# OpenTelemetry GenAI 语义字段映射

[English Version](../en/semantic-field-mapping.md)

## 说明

本页描述 `hermes-otel-plugin` 从历史 Hermes 字段到 OpenTelemetry GenAI semantic conventions 字段的兼容关系。

当前策略是在 span 字段上保持向后兼容，同时将 metrics 迁移到标准 GenAI 指标体系。插件仍会在 span 上输出标准 `gen_ai.*` / `error.type` 字段，但旧的 `gen_ai.agent.*` 和 `gen_ai.runtime.*` 指标已经移除。

## Span 字段

| 当前字段 | 新增标准字段 | 处理方式 | 说明 |
| --- | --- | --- | --- |
| `session_id` | `gen_ai.conversation.id` | 双写 | 用 Hermes session id 关联 trace、metric、log |
| `provider_name` | `gen_ai.provider.name` | 双写 | 当前保持宿主 provider 原值，例如 `openai-codex` |
| `request_model` | `gen_ai.request.model` | 双写 | 请求模型 |
| `response_model` | `gen_ai.response.model` | 双写 | 响应模型；缺失时回退到请求模型 |
| provider request messages | `gen_ai.input.messages` | 标准字段 | 从 Hermes 已脱敏的 request payload 生成，在 span 上写成 JSON string |
| provider assistant message | `gen_ai.output.messages` | 标准字段 | 从 Hermes 已脱敏的 response payload 生成，在 span 上写成 JSON string |
| provider response id | `gen_ai.response.id` | 标准字段 | provider response 暴露 id 时写入 |
| `finish_reason` | `gen_ai.response.finish_reasons` | 双写 | 标准字段为数组 |
| provider request parameters | `gen_ai.request.*` | 标准字段 | 输出可观测到的 `choice.count`、`max_tokens`、`temperature`、`top_p`、`top_k`、`frequency_penalty`、`presence_penalty`、`seed`、`stop_sequences`、`stream` |
| provider system instructions | `gen_ai.system_instructions` | 标准字段 | 当 instructions 与 chat history 分离时写入 |
| provider tool definitions | `gen_ai.tool.definitions` | 标准字段 | request payload 包含 tool definitions 时写入 |
| `operation_name=model` | `gen_ai.operation.name=chat` | 双写 | 用于模型调用 |
| `operation_name=tool` | `gen_ai.operation.name=execute_tool` | 双写 | 用于 tool 调用 |
| `invoke_agent` span | `gen_ai.operation.name=invoke_agent` | 同名 | span 名称使用标准 operation name |
| `tool_name` | `gen_ai.tool.name` | 双写 | 用于 `tool:*` span 和 tool operation metric |
| `tool_call_id` | `gen_ai.tool.call.id` | 双写 | tool call 标识 |
| `tool_args_preview` | `gen_ai.tool.call.arguments` | 双写 | 标准字段在 span 上写成 JSON string |
| `tool_result_preview` | `gen_ai.tool.call.result` | 双写 | 标准字段在 span 上写成 JSON string |
| `usage_input_tokens` | `gen_ai.usage.input_tokens` | 双写 | 仅在 `llm` span 双写 |
| `usage_output_tokens` | `gen_ai.usage.output_tokens` | 双写 | 仅在 `llm` span 双写 |
| `usage_total_tokens` | `gen_ai.usage.total_tokens` | 双写 | 仅在 `llm` span 双写 |
| `usage_cache_read_input_tokens` | `gen_ai.usage.cache_read.input_tokens` | 双写 | 仅在 `llm` span 双写；旧 `gen_ai.usage.cache_read_input_tokens` 仍保留 |
| `usage_cache_write_input_tokens` | `gen_ai.usage.cache_creation.input_tokens` | 双写 | 仅在 `llm` span 双写；旧 `gen_ai.usage.cache_write_input_tokens` 仍保留 |
| `usage_reasoning_tokens` | `gen_ai.usage.reasoning.output_tokens` | 双写 | 仅在 `llm` span 双写；旧 `gen_ai.usage.reasoning_tokens` 仍保留 |
| `error_type` / `error_code` | `error.type` | 双写 | `error.type` 优先使用低基数 `error_code`，其次 `error_type` |

## 指标

| 当前指标 | 新增标准指标 | 处理方式 | 说明 |
| --- | --- | --- | --- |
| `gen_ai.agent.token.usage` | `gen_ai.client.token.usage` | 替换 | 标准指标只写 `gen_ai.token.type=input|output`；total/cache/reasoning bucket 不再作为 metrics 输出 |
| `gen_ai.agent.operation.duration` with `operation_name=model` | `gen_ai.client.operation.duration` | 替换 | 标准指标单位为秒 |
| `gen_ai.agent.operation.duration` with `operation_name=tool` | `gen_ai.client.operation.duration` | 替换 | 工具调用使用 `gen_ai.operation.name=execute_tool` |
| `gen_ai.agent.operation.duration` with `operation_name=skill` | `gen_ai.client.operation.duration` | 替换 | skill 调用使用 `gen_ai.operation.name=skill` 和 `gen.ai.skill.name` |
| `gen_ai.agent.request.duration` | `gen_ai.workflow.duration` | 替换 | workflow duration 单位为秒，`gen_ai.operation.name=invoke_agent` |
| `gen_ai.agent.request.count` | 无替代 | 移除 | 不再输出 request counter |
| `gen_ai.agent.operation.count` | 无替代 | 移除 | 不再输出 operation counter |
| `gen_ai.agent.session.token.*` | 无替代 | 移除 | `hermes_request` / `invoke_agent` 不再输出聚合 usage 指标 |
| `gen_ai.agent.skill.activation.count` | 无直接 counter 替代 | 移除 | skill 耗时由 `gen_ai.operation.name=skill` 的 `gen_ai.client.operation.duration` 表达 |
| `gen_ai.agent.subagent.*` | 无替代 | 移除 | subagent 活动仍可通过 spans 查看 |
| `gen_ai.runtime.*` | 无替代 | 移除 | 不再输出 runtime hook counters/histograms |

## Span 名称

| 当前 span | 标准语义表达 | 处理方式 |
| --- | --- | --- |
| `hermes_request` | `gen_ai.conversation.id` + request/session 扩展字段 | 保留 span 名称 |
| `invoke_agent` | `gen_ai.operation.name=invoke_agent` | span 名称使用标准 operation name |
| `llm` | `gen_ai.operation.name=chat` | 保留 span 名称 |
| `tool:*` | `gen_ai.operation.name=execute_tool` + `gen_ai.tool.name` | 保留 span 名称 |
| `skill:*` | Hermes skill 扩展语义 | 保留 span 名称 |
| `subagent:*` | `gen_ai.operation.name=invoke_agent` + `subagent_role` | 保留 span 名称 |

## 保留的 Hermes 扩展字段

以下字段目前没有稳定的 GenAI 标准字段替代，继续作为 Hermes 扩展字段输出：

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
- `tool_outcome`
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

说明：

- `skill.*` 是当前插件对 skill 语义的兼容主字段。
- `gen_ai.skill.*` 是项目扩展字段，便于向社区提案方向对齐，但它们暂时不是 OpenTelemetry GenAI 正式标准字段。
- `skill_name`、`skill_description` 仍会继续保留，作为向后兼容字段输出。
