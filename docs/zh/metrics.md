# 指标说明

[English Version](../en/metrics.md)

## 概览

本文档描述 `hermes-otel-plugin` 当前会输出的 metrics。

命名模型与 `openclaw-otel-plugin` 保持相同的产品方向：

- `gen_ai.client.*`
- `gen_ai.agent.*`
- `gen_ai.runtime.*`

当前边界：

- `gen_ai.client.*`
  用于插件主动输出的 OpenTelemetry GenAI client 模型调用指标。
  兼容过渡期内，Hermes 仍会保留旧的 `gen_ai.agent.*` 模型指标。
- `gen_ai.agent.*`
  用于 Hermes 的 request、旧模型 operation/token、session token、skill、subagent 指标。
- `gen_ai.runtime.*`
  用于当前 Hermes plugin hooks 可以稳定观察到的运行时过程指标。

## 通用 Tags

### 共享 Tags

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
- `skill_source`
- `model_name`
- `subagent_role`
- `outcome`
- `session_state`
- `request_type`
- `review_category`

### Tag 说明

| tag | 含义 |
| --- | --- |
| `agent_runtime` | 固定为 `hermes` |
| `session_id` | Hermes session id |
| `platform` | 当前运行平台，例如 `cli` |
| `provider_name` | 模型提供方 |
| `gen_ai.provider.name` | OpenTelemetry GenAI provider name，当前镜像 `provider_name` |
| `request_model` | 请求模型 |
| `gen_ai.request.model` | OpenTelemetry GenAI 请求模型，镜像 `request_model` |
| `response_model` | 响应模型 |
| `gen_ai.response.model` | OpenTelemetry GenAI 响应模型，镜像 `response_model` |
| `operation_name` | 操作分类，例如 `model`、`tool`、`skill`、`subagent` |
| `gen_ai.operation.name` | OpenTelemetry GenAI 操作名称，例如 `chat`、`execute_tool`、`invoke_agent` |
| `token_type` | token 桶，例如 `input`、`output`、`total` |
| `gen_ai.token.type` | OpenTelemetry GenAI client token 桶，当前为 `input` 或 `output` |
| `tool_name` | 工具名称 |
| `tool_result_status` | 从 tool 返回体中提取的显式状态 |
| `skill_name` | skill 名称 |
| `skill_source` | skill 来源，当前为 `runtime` |
| `model_name` | tool operation metrics 用到的模型归因 tag |
| `subagent_role` | 子代理角色 |
| `outcome` | 归一化结果，例如 `completed`、`error`、`failed`、`interrupted` |
| `session_state` | request metrics 使用的请求最终状态 |
| `request_type` | 请求分类，例如 `user_request`、`auto_review` |
| `review_category` | review 子类型，当前自动 review 流程主要是 `skill` |

## 指标清单

### GenAI Client

Hermes 会为模型调用输出 OpenTelemetry GenAI client 指标，同时保留旧的 agent 指标。

| metric | 类型 | 单位 | tags | 描述 |
| --- | --- | --- | --- | --- |
| `gen_ai.client.operation.duration` | Histogram | `s` | `agent_runtime`, `agent_version`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `outcome`, `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.conversation.id`, `error.type`, `server.address`, `server.port` | 使用 OpenTelemetry GenAI client 语义记录模型调用耗时 |
| `gen_ai.client.token.usage` | Histogram | `{token}` | `agent_runtime`, `agent_version`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `token_type`, `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.conversation.id`, `gen_ai.token.type` | 使用 OpenTelemetry GenAI client 语义记录模型调用 input/output token |

### GenAI Agent

| metric | 类型 | 单位 | tags | 描述 |
| --- | --- | --- | --- | --- |
| `gen_ai.agent.request.count` | Counter | - | `agent_runtime`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `session_state`, `outcome`, `request_type`, `review_category` | 已完成 Hermes 请求计数 |
| `gen_ai.agent.request.duration` | Histogram | `ms` | `agent_runtime`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `session_state`, `outcome`, `request_type`, `review_category` | 一次 Hermes 请求总耗时 |
| `gen_ai.agent.token.usage` | Histogram | `{token}` | `agent_runtime`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `token_type` | 单次模型调用 token 用量 |
| `gen_ai.agent.operation.count` | Counter | - | Base: `agent_runtime`, `session_id`, `outcome`, `operation_name`<br>`operation_name=model`: `platform`, `provider_name`, `request_model`, `response_model`<br>`operation_name=tool`: `platform`, `tool_name`, `skill_name`, `model_name`, `tool_result_status`<br>`operation_name=skill`: `skill_name`, `skill_source`<br>`operation_name=subagent`: `subagent_role` | model、tool、skill、subagent 维度的操作计数 |
| `gen_ai.agent.operation.duration` | Histogram | `ms` | Base: `agent_runtime`, `session_id`, `outcome`, `operation_name`<br>`operation_name=model`: `platform`, `provider_name`, `request_model`, `response_model`<br>`operation_name=tool`: `platform`, `tool_name`, `skill_name`, `model_name`, `tool_result_status`<br>`operation_name=skill`: `skill_name`, `skill_source`<br>`operation_name=subagent`: `subagent_role` | model、tool、skill、subagent 维度的操作耗时 |
| `gen_ai.agent.session.token.input` | Counter | `{token}` | `agent_runtime`, `session_id`, `provider_name`, `request_model` | 请求结束时一次性写入的聚合 input tokens |
| `gen_ai.agent.session.token.output` | Counter | `{token}` | `agent_runtime`, `session_id`, `provider_name`, `request_model` | 请求结束时一次性写入的聚合 output tokens |
| `gen_ai.agent.session.token.total` | Counter | `{token}` | `agent_runtime`, `session_id`, `provider_name`, `request_model` | 请求结束时一次性写入的聚合 total tokens |
| `gen_ai.agent.session.token.usage` | Counter | `{token}` | `agent_runtime`, `session_id`, `provider_name`, `request_model`, `token_type` | 按 token type 聚合的 session token 计数器 |
| `gen_ai.agent.session.trace.count` | Counter | - | `agent_runtime`, `session_id`, `platform`, `request_model`, `request_type` | 插件启动的 trace 数 |
| `gen_ai.agent.skill.activation.count` | Counter | - | `agent_runtime`, `session_id`, `skill_name`, `skill_source` | 成功激活的 skill 次数 |
| `gen_ai.agent.subagent.count` | Counter | - | `agent_runtime`, `session_id`, `subagent_role`, `outcome`, `operation_name` | 观察到的子代理完成次数 |
| `gen_ai.agent.subagent.duration` | Histogram | `ms` | `agent_runtime`, `session_id`, `subagent_role`, `outcome`, `operation_name` | 观察到的子代理执行耗时 |

### GenAI Runtime

| metric | 类型 | 单位 | tags | 描述 |
| --- | --- | --- | --- | --- |
| `gen_ai.runtime.tool.call.count` | Counter | - | `agent_runtime`, `session_id`, `platform`, `tool_name`, `skill_name`, `tool_result_status`, `outcome` | 观察到的 tool call 次数 |
| `gen_ai.runtime.tool.call.duration` | Histogram | `ms` | `agent_runtime`, `session_id`, `platform`, `tool_name`, `skill_name`, `tool_result_status`, `outcome` | 观察到的 tool call 耗时 |
| `gen_ai.runtime.session.start.count` | Counter | - | `agent_runtime`, `session_id`, `platform` | Hermes session start 事件计数 |
| `gen_ai.runtime.session.end.count` | Counter | - | `agent_runtime`, `session_id`, `platform`, `outcome` | Hermes session end 事件计数 |
| `gen_ai.runtime.session.reset.count` | Counter | - | `agent_runtime`, `session_id`, `platform` | Hermes session reset 事件计数 |
| `gen_ai.runtime.turn.interrupted.count` | Counter | - | `agent_runtime`, `session_id`, `platform` | 被中断的 Hermes 请求计数 |

## 当前与 OpenClaw 的对齐情况

以下部分已经在原则上对齐：

- request metrics
- operation metrics
- tool runtime metrics
- session trace count
- session token aggregation
- skill activation metric

以下部分当前 Hermes 还不会输出，因为 plugin hook 模型没有暴露所需的运行时信号：

- `gen_ai.runtime.message.*`
- `gen_ai.runtime.queue.*`
- `gen_ai.runtime.session.stuck.*`
- `gen_ai.runtime.webhook.*`

## 说明

- `gen_ai.agent.token.usage` 记录单次模型调用的 token 用量。
- `gen_ai.client.token.usage` 使用标准 GenAI 属性记录同一批模型调用 token，并且只写入 `input` / `output` token 桶。
- `gen_ai.agent.session.token.*` 在请求收尾时记录整轮聚合 token。
- `tool_result_status` 与归一化后的 `outcome` 保持分离。
- `request_type=user_request` 是默认分类。
- `request_type=auto_review` 用于 Hermes 自动 review 流程，必要时还会带 `review_category=skill`。
- 旧字段到标准字段的关系见：[语义字段映射](semantic-field-mapping.md)。
