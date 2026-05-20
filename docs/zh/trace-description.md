# Hermes Trace 说明

[English Version](../en/trace-description.md)

## 说明

- 本文档只描述 `hermes-otel-plugin` 当前已经稳定落地的 trace / span 语义
- 不展开历史兼容映射
- 只写当前查询和排障时真正可依赖的 span 类型与字段
- 架构和时序可参考：[architecture.md](architecture.md)

## AI Agent 说明

- 这里的 `AI Agent` 指 Hermes 在一次用户输入下组织上下文、模型、工具、技能和子代理后完成任务的执行主体
- 在 Hermes 的 trace 语义里：
  - `hermes_request`：一条用户输入对应的一次完整请求
  - `agent_run`：这次请求里的 agent 主执行窗口
  - `llm`：执行过程中一次真实模型调用
  - `tool:*`：执行过程中一次工具调用
  - `skill:*`：skill 在本次请求中的生效执行窗口
  - `subagent:*`：由主代理委派出来的子代理执行窗口

因此：

- `llm` 不等于 agent
- `agent_run` 才是最接近 agent execution 的 span
- 多次 `llm`、`tool:*`、`skill:*`、`subagent:*` 共同构成一次 agent 执行

## 最终 Span 规范

### 保留的 Span

- `hermes_request`
- `agent_run`
- `llm`
- `tool:*`
- `skill:*`
- `subagent:*`

### 不单独拆出的流程节点

- Decision Router
- Tool Result
- Skill Result
- Final Answer
- Session Persist

说明：

- 这些节点当前通过已有 span 的走向、属性和结果表达，不额外创建独立 span
- `Final Answer` 由最后一个 `llm` 的 `output_kind=text` 和 `output_preview` 表达
- Tool / skill 的结果通过 `tool_result_*` 和 `skill_*` 字段表达

### 设计边界

- 一条用户输入对应一条 trace
- 只保留对排障稳定且有价值的 span
- 能用属性表达的，不额外拆 span
- 子代理如果以独立 session 执行，在 trace 上默认折叠成父请求下的 `subagent:*`

## 核心 Span

### `hermes_request`

表示“一条用户输入对应的一次完整请求”。

用途：

- 作为整条 trace 的 root span
- 表示从消息进入 Hermes 到本轮处理完成的总窗口
- 承载整轮请求级汇总信息，例如：
  - `session_id`
  - `final_status`
  - `provider_name`
  - `response_model`
  - 汇总 token
  - 最终输出预览

### `agent_run`

表示“一次 agent 实际执行窗口”。

用途：

- 作为 `hermes_request` 下的主执行 span
- 承载本轮所有 `llm`、`tool:*`、`skill:*`、`subagent:*` 的父级上下文
- 汇总本轮 agent 执行维度的信息，例如：
  - 本轮最终状态
  - 汇总 token
  - 最终输出预览

### `llm`

表示“一次模型请求”。

用途：

- 对应一次真实大模型调用
- 记录该次模型调用的：
  - `provider_name`
  - `request_model`
  - `response_model`
  - `input_preview`
  - `tool_context_preview`
  - `output_preview`
  - `output_kind`
  - token 使用量

补充说明：

- 当前 Hermes 宿主 hook 不直接把完整 prompt / response 文本传给插件
- 因此 `llm.output_preview` 仍然是插件侧摘要，不等于 provider 原始响应体
- `llm.input_preview` 当前仅用于首个模型调用，表示原始用户输入摘要
- 后续模型调用如果需要表达插件侧拼接的工具上下文，会写入 `tool_context_preview`

### `tool:*`

表示“一次工具调用”。

用途：

- 对应一次工具执行
- 记录该次调用的：
  - `tool_name`
  - `tool_call_id`
  - `tool_phase`
  - `tool_outcome`
  - `tool_arg_keys`
  - `tool_target`
  - `tool_command`
  - `tool_args_preview`
  - `tool_result_status`
  - `tool_result_preview`

和 `openclaw-otel-plugin` 的对齐说明：

- 已对齐的核心字段：
  - `tool_name`
  - `tool_call_id`
  - `tool_phase`
  - `tool_outcome`
  - `tool_arg_keys`
  - `tool_target`
  - `tool_command`
  - `tool_args_preview`
  - `tool_result_status`
  - `tool_result_preview`
- 当前 Hermes 插件无法稳定提供的字段：
  - `tool_meta_preview`
  - `tool_partial_result_preview`
  - `tool_loop_level`
  - `tool_loop_action`
  - `tool_loop_detector`
  - `tool_loop_count`
  - `tool_loop_paired_tool`
  - `tool_loop_message`
- 原因：
  - Hermes 当前 plugin hook 不直接暴露 tool meta / partial result / loop detector 事件
  - 因此这部分无法只靠插件侧可靠补齐

### `skill:*`

表示“skill 在当前请求中的生效执行窗口”。

用途：

- 不表示 `skill_view` 的加载耗时
- 表示该 skill 从被装载进入上下文后，在本次请求中持续生效的时间窗口

说明：

- skill 的加载动作由 `tool:skill_view` 表达
- `skill:*` 可以重叠，表示多个 skill 同时对后续推理生效

### `subagent:*`

表示“子代理执行窗口”。

用途：

- 表示一次由主代理委派出去的子代理执行
- 优先挂到触发它的 `tool:delegate_task` 下
- 如果找不到 delegate tool，则回退挂到 `agent_run` 下

## Status

`status` 表示当前 span 自身的执行状态。

用途：

- 判断某个具体 span 是否执行成功
- 适合定位技术错误和执行异常
- 常见于：
  - `llm`
  - `tool:*`
  - `skill:*`
  - `subagent:*`

推荐理解：

| 值 | 含义 |
| --- | --- |
| `ok` | 当前 span 执行成功 |
| `error` | 当前 span 执行失败 |
| `unset` / 空 | 当前 span 没有显式设置状态 |

使用建议：

- 看单个 `tool:*` 是否失败，优先看 `status`
- 看单个 `llm` 是否异常结束，优先看 `status`

## Final Status

`final_status` 表示一条 `hermes_request` 或 `agent_run` 的最终业务结果。

用途：

- 判断一次 agent 请求最终是成功完成、失败、中断、重置还是被覆盖
- 主要用于请求级 / run 级结果分析
- 不用于判断某个子 span 的单点技术错误

推荐理解：

| 值 | 含义 |
| --- | --- |
| `completed` | 本轮请求完成并产出结果 |
| `failed` | 本轮请求最终失败 |
| `interrupted` | 本轮请求被中断 |
| `reset` | 本轮请求因 session reset 收尾 |
| `expired` | 本轮请求超 TTL 被插件兜底收尾 |
| `superseded` | 同 session 新请求覆盖了旧请求 |

使用建议：

- 看一次 Hermes 请求最后是否完成，优先看 `final_status`
- 即使某个 `tool:*` 的 `status=error`，只要请求最终产出有效结果，`final_status` 仍可能是 `completed`

## Span 通用字段

| 字段 | 描述 |
| --- | --- |
| `agent_runtime` | 当前 runtime，固定为 `hermes` |
| `session_id` | 当前 Hermes session id |
| `platform` | 平台，例如 `cli` |
| `final_status` | 请求最终状态 |
| `provider_name` | 模型提供方 |
| `request_model` | 请求模型 |
| `response_model` | 响应模型 |
| `request_type` | 请求类型，当前包括 `user_request`、`auto_review` |
| `is_auto_review` | 是否为 Hermes 自动触发的 review 类请求 |
| `review_category` | review 类型，当前自动 review 会标记为 `skill` |
| `skills` | 当前相关 skill 列表 |
| `skill_count` | 当前 skill 数量 |
| `output_preview` | 输出摘要 |
| `output_length` | 输出长度 |

### Request Type

`request_type` 用于区分这轮请求是普通用户请求，还是 Hermes 在主任务之后自动触发的内部 review 请求。

当前口径如下：

| 值 | 含义 |
| --- | --- |
| `user_request` | 默认值。普通用户发起的请求都会标记为这个值。 |
| `auto_review` | Hermes 自动触发的 review 类请求。当前主要用于 skill review / skill patch 场景。 |

补充说明：

- 默认情况下，`request_type=user_request`
- 只有当用户输入命中内部 review 提示词模式时，才会标记为 `auto_review`
- 当 `request_type=auto_review` 时，还会同时带：
  - `is_auto_review=true`
  - `review_category=skill`
- 当 `request_type=user_request` 时：
  - `is_auto_review=false`
  - `review_category` 通常为空

## Model 相关字段

| 字段 | 描述 |
| --- | --- |
| `api_mode` | 模型 API 模式 |
| `api_call_count` | 当前请求内第几次模型调用 |
| `input_preview` | 首个模型调用的输入摘要，当前通常是用户输入摘要 |
| `tool_context_preview` | 插件侧归纳的工具上下文摘要，仅用于后续模型调用 |
| `input_length` | 输入长度 |
| `output_preview` | 输出摘要 |
| `output_length` | 输出长度 |
| `output_kind` | 输出类型，例如 `text`、`tool_call` |
| `assistant_tool_call_count` | 该次模型输出的工具调用数量 |
| `finish_reason` | provider 返回的 finish reason |
| `usage_input_tokens` | 输入 token |
| `usage_output_tokens` | 输出 token |
| `usage_total_tokens` | 总 token，当前口径为 `input + output` |
| `usage_cache_read_input_tokens` | cache read token |
| `usage_cache_write_input_tokens` | cache write token |
| `usage_cache_total_tokens` | cache read + cache write |
| `usage_reasoning_tokens` | reasoning token，只有 Hermes / provider 提供时才会有值 |

## Tool 相关字段

| 字段 | 描述 |
| --- | --- |
| `tool_name` | tool 名称 |
| `tool_call_id` | tool call 标识 |
| `tool_phase` | tool 当前阶段，当前为 `call` 或 `result` |
| `tool_outcome` | 插件统一归一后的 tool 结果，当前口径为 `completed` / `error` |
| `tool_arg_keys` | tool 参数 key 列表 |
| `tool_target` | tool 操作目标，例如文件路径、搜索目录、skill 名称 |
| `tool_command` | tool 执行命令，目前主要用于 `terminal` |
| `tool_args_preview` | tool 参数摘要 |
| `tool_result_status` | tool 返回体中的显式状态字段，优先取 `result.details.status`，其次取 `result.status` |
| `tool_result_preview` | tool 结果摘要 |

### Tool Tag Standard

以下内容用于和 `openclaw-otel-plugin` 对齐后的标准产品口径。

#### Required

这些字段建议作为标准产品里的必选 tool tag。

| 字段 | 要求 | 说明 |
| --- | --- | --- |
| `tool_name` | 必选 | tool 名称 |
| `tool_call_id` | 必选 | tool call 标识；如果宿主未提供，可为空 |
| `tool_phase` | 必选 | tool 当前阶段；当前口径为 `call` / `result` |
| `tool_outcome` | 必选 | 插件统一归一后的结果；当前口径为 `completed` / `error` |
| `tool_arg_keys` | 必选 | tool 参数 key 列表 |
| `tool_args_preview` | 必选 | tool 参数摘要 |
| `tool_result_preview` | 必选 | tool 结果摘要 |
| `tool_result_status` | 必选 | tool 返回体中的显式状态，不等于 `tool_outcome` |

#### Conditional

这些字段建议纳入标准，但只有在宿主或插件能稳定推导时才上报。

| 字段 | 当前 Hermes 状态 | 说明 |
| --- | --- | --- |
| `skill_name` | 已支持 | 当前主要用于 `skill_view` 及 skill 相关 tool |
| `tool_target` | 已支持 | 当前按常见工具推导，如文件路径、搜索目录、skill 名称 |
| `tool_command` | 已支持 | 当前主要用于 `terminal` |

#### Not Currently Available In Hermes

这些字段在 OpenClaw 里存在，但 Hermes 当前 plugin hook 无法稳定提供，因此不建议伪造。

| 字段 | 原因 |
| --- | --- |
| `tool_meta_preview` | Hermes hook 不直接暴露 tool meta |
| `tool_partial_result_preview` | Hermes hook 不提供 partial result 生命周期 |
| `tool_loop_level` | Hermes hook 不暴露 loop detector 事件 |
| `tool_loop_action` | 同上 |
| `tool_loop_detector` | 同上 |
| `tool_loop_count` | 同上 |
| `tool_loop_paired_tool` | 同上 |
| `tool_loop_message` | 同上 |

#### Normalization Rules

- `tool_outcome` 表示插件统一归一后的执行结果，用于跨工具做统一聚合。
- `tool_result_status` 只表示 tool 返回体里的显式状态，不应回退为 `tool_outcome`。
- `tool_target` 和 `tool_command` 允许按 tool 类型做有限推导，但不应为了“字段齐全”写入不可靠值。
- 对标准产品来说，优先保证字段语义稳定，其次才是字段数量完整。

## Skill 相关字段

| 字段 | 描述 |
| --- | --- |
| `skill_name` | skill 名称 |
| `skill_description` | skill 描述 |
| `skill_tags` | skill tags |
| `skill_related_skills` | 关联 skill |
| `skill_content_length` | skill 内容长度 |
| `skill_source_tool_call_id` | 触发该 skill span 的 `skill_view` tool call id |

## Subagent 相关字段

| 字段 | 描述 |
| --- | --- |
| `subagent_role` | 子代理展示角色；当 Hermes 仅返回通用角色如 `leaf` 时，插件会优先回退到 `delegate_task.profile` |
| `subagent_runtime_role` | Hermes hook 返回的原始子代理角色，例如 `leaf` |
| `outcome` | 子代理结果 |
| `output_preview` | 子代理摘要输出 |
| `output_length` | 子代理输出长度 |

## Token 统计原则

- `usage_total_tokens = usage_input_tokens + usage_output_tokens`
- cache token 不并入 `usage_total_tokens`
- cache token 单独写入 `usage_cache_*`
- 如果 provider 返回的是累计 cache 计数，插件会归一成单次 `llm` 调用的增量
- 根 span 和 `agent_run` 会聚合整轮请求内所有 `llm` 的 token

## 已知边界

- `llm.input_preview` 当前不是 provider 原始请求正文，只是首个模型调用的用户输入摘要
- `llm.tool_context_preview` 是插件侧拼接的工具上下文摘要，不代表 provider 原始 prompt
- `llm.output_preview` 当前不是 provider 原始响应正文，而是插件侧摘要
- `usage_reasoning_tokens` 只有 Hermes 或 provider 已透传时才会有值
- `skill:*` 表示生效执行窗口，不表示加载耗时
- `tool:skill_view` 才表示 skill 加载动作本身
