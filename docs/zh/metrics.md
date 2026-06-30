# 指标说明

[English Version](../en/metrics.md)

## 概览

`hermes-otel-plugin` 当前输出三类与 OpenTelemetry GenAI 对齐的指标：

- `gen_ai.workflow.duration`
- `gen_ai.client.operation.duration`
- `gen_ai.client.token.usage`

插件不再输出旧的 `gen_ai.agent.*` 或 `gen_ai.runtime.*` 指标。请求级、工具级、skill 级耗时由标准 workflow/client operation 指标表达，token 指标只来自模型调用 usage。

## 指标清单

| metric | 类型 | 单位 | tags | 描述 |
| --- | --- | --- | --- | --- |
| `gen_ai.workflow.duration` | Histogram | `s` | `session_id`, `gen_ai.conversation.id`, `gen_ai.operation.name` | Hermes agent workflow 耗时。`gen_ai.operation.name` 固定为 `invoke_agent`。 |
| `gen_ai.client.operation.duration` | Histogram | `s` | 通用 tags 加操作特有 tags | 模型调用、工具调用和 skill 调用的 client operation 耗时。 |
| `gen_ai.client.token.usage` | Histogram | `{token}` | 通用 tags 加 `gen_ai.token.type` | 模型调用 token 用量，只输出 `input` 和 `output`。 |

## 通用 Tags

| tag | 适用范围 | 说明 |
| --- | --- | --- |
| `session_id` | 全部指标 | Hermes session id。 |
| `gen_ai.conversation.id` | 全部指标 | 镜像 `session_id`，用于 OpenTelemetry GenAI 关联。 |
| `gen_ai.operation.name` | 全部指标 | `invoke_agent`、`chat`、`execute_tool` 或 `skill`。 |
| `gen_ai.provider.name` | 模型 operation 和 token 指标 | 模型提供方。 |
| `gen_ai.request.model` | 模型 operation 和 token 指标 | 请求模型。 |
| `gen_ai.response.model` | 模型 operation 和 token 指标 | 响应模型。 |
| `server.address` | 模型 operation 和 token 指标 | 可观测到 provider endpoint 时写入 host。 |
| `server.port` | 模型 operation 和 token 指标 | 可观测到 provider endpoint 时写入 port。 |
| `error.type` | 模型 operation 指标 | 模型调用失败时写入低基数错误类型。 |
| `gen_ai.tool.name` | 工具 operation 指标 | 工具名称。 |
| `tool_result_status` | 工具 operation 指标 | 从 Hermes tool 输出中提取的结果状态。 |
| `gen.ai.skill.name` | skill operation 指标 | skill 名称。 |
| `gen_ai.token.type` | token 指标 | `input` 或 `output`。 |

`host` 和 `host.name` 应由 runtime/exporter 配置写入 OTLP resource attributes，不作为每个 metric point 的普通 tag 重复写入。

## Operation 语义

### Workflow

`invoke_agent` workflow 结束时记录 `gen_ai.workflow.duration`。

Workflow 指标不携带模型、usage、tool、request type 或聚合 token tags，避免在请求级指标中混入模型维度。

### 模型调用

模型调用记录：

- `gen_ai.client.operation.duration`，`gen_ai.operation.name=chat`
- `gen_ai.client.token.usage`，`gen_ai.token.type=input`
- `gen_ai.client.token.usage`，`gen_ai.token.type=output`

cache、total、reasoning token 在 Hermes 可观测到时仍保留在 `llm` span 的 trace attributes 上，但不再作为 metrics 输出。

### 工具和 Skill 调用

工具调用记录 `gen_ai.client.operation.duration`，并携带：

- `gen_ai.operation.name=execute_tool`
- `gen_ai.tool.name`
- 可用时携带 `tool_result_status`

工具指标不携带模型 tags，也不携带 token usage tags。

Skill 调用作为一类特殊工具调用记录 `gen_ai.client.operation.duration`，并携带：

- `gen_ai.operation.name=skill`
- `gen.ai.skill.name`

Skill 指标不携带模型 tags，也不携带 token usage tags。

## 已移除旧指标

| 已移除指标 | 替代方式 |
| --- | --- |
| `gen_ai.agent.request.count` | 移除，无 counter 替代。 |
| `gen_ai.agent.request.duration` | `gen_ai.workflow.duration` |
| `gen_ai.agent.operation.count` | 移除，无 counter 替代。 |
| `gen_ai.agent.operation.duration` | `gen_ai.client.operation.duration` |
| `gen_ai.agent.token.usage` | `gen_ai.client.token.usage` |
| `gen_ai.agent.session.token.*` | 移除；`hermes_request` / `invoke_agent` 不再输出聚合 usage 指标。 |
| `gen_ai.agent.skill.activation.count` | 移除；skill 耗时由 `gen_ai.operation.name=skill` 的 `gen_ai.client.operation.duration` 表达。 |
| `gen_ai.agent.subagent.*` | 移除。 |
| `gen_ai.runtime.*` | 移除。 |

## 迁移说明

- workflow 和 client operation 指标单位从毫秒改为秒。
- `session_id` 保留，并复制到 `gen_ai.conversation.id`。
- `provider_name` 改为 `gen_ai.provider.name`。
- `request_model` 改为 `gen_ai.request.model`。
- `response_model` 改为 `gen_ai.response.model`。
- `operation_name` 改为 `gen_ai.operation.name`。
- `tool_name` 改为 `gen_ai.tool.name`。
- `token_type` 改为 `gen_ai.token.type`。
- `token_type=total`、cache token bucket、reasoning token bucket 不再作为 metrics 输出。
