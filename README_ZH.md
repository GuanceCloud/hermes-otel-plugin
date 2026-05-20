# hermes-otel-plugin

`hermes-otel-plugin` 用于把 Hermes 的 session、turn、模型请求、tool 调用和子代理执行过程导出到任意兼容 `OTLP HTTP/protobuf` 的接收端。

本仓库延续了 `openclaw-otel-plugin` 的设计规范，但实现方式完全对齐 Hermes 原生 Python 插件机制：

- 插件入口：`plugin.yaml` + `__init__.py`
- 指标命名空间：`gen_ai.client.*`、`gen_ai.agent.*`、`gen_ai.runtime.*`
- canonical tags：`agent_runtime`、`session_id`、`platform`、`provider_name`、`request_model`、`response_model`、`tool_name`
- 语义边界：以 Hermes 的 session / turn / API request / tool / subagent 生命周期为准

## 导出内容

### Traces

- `hermes_request`
- `agent_run`
- `llm`
- `skill:<name>`
- `tool:<name>`
- `subagent:<role>`

一条用户 turn 对应一条 trace；模型调用、tool 调用和子代理执行会挂到该 trace 下。`skill:<name>` span 会在 `skill_view` 成功加载后开始，并持续到当前 turn 结束，表示该 skill 在本次请求中的生效执行窗口。多个 skill 可以同时活跃，因此允许 span 重叠。相关 `llm` span 也会附带 `skills` 和 `skill_count`，用于标记该次模型调用受哪些 skill 影响。

架构与时序图见：[docs/architecture.zh.md](docs/architecture.zh.md)

Trace / span 描述见：[docs/trace-description.md](docs/trace-description.md)

Metrics 描述见：[docs/metrcis.zh.md](docs/metrcis.zh.md)

关于 skill 的两个时间语义：

- `tool:skill_view`：表示 skill 的加载动作和加载耗时，通常是短 span。
- `skill:<name>`：表示 skill 在当前请求中的生效窗口，不表示文件加载耗时。

关于子代理层级：

- `subagent:<role>` 默认挂在 `agent_run` 下。
- 如果本轮存在触发子代理的 `tool:delegate_task`，则 `subagent:<role>` 优先挂在该 `tool:delegate_task` 下，表示明确的因果关系。

### Metrics

- `gen_ai.agent.request.count`
- `gen_ai.agent.request.duration`
- `gen_ai.agent.token.usage`
- `gen_ai.agent.operation.count`
- `gen_ai.agent.operation.duration`
- `gen_ai.agent.session.token.input`
- `gen_ai.agent.session.token.output`
- `gen_ai.agent.session.token.total`
- `gen_ai.agent.session.token.usage`
- `gen_ai.agent.session.trace.count`
- `gen_ai.agent.skill.activation.count`
- `gen_ai.agent.subagent.count`
- `gen_ai.agent.subagent.duration`
- `gen_ai.runtime.tool.call.count`
- `gen_ai.runtime.tool.call.duration`
- `gen_ai.runtime.session.start.count`
- `gen_ai.runtime.session.end.count`
- `gen_ai.runtime.session.reset.count`
- `gen_ai.runtime.turn.interrupted.count`

补充说明：

- `gen_ai.agent.operation.*` 当前覆盖 `model`、`tool`、`skill`、`subagent`
- `gen_ai.agent.session.token.*` 表示按 Hermes request 聚合后，累计写入 session 级 token 计数
- request 类指标会额外带 `request_type` / `review_category`，用于区分普通用户请求和自动 review 请求

### Logs

当 `logs_enabled=true` 时，插件会把 session、API request、tool、subagent 的关键生命周期事件镜像到 OTEL logs。

## 安装位置

本仓库目标目录：

```text
/home/liurui/code/hermes-otel-plugin
```

Hermes 用户插件发现路径建议保持为：

```text
~/.hermes/plugins/hermes-otel-plugin -> /home/liurui/code/hermes-otel-plugin
```

并在 `~/.hermes/config.yaml` 中启用：

```yaml
plugins:
  enabled:
    - hermes-otel-plugin
```

## 配置

在 `~/.hermes/config.yaml` 中新增 `hermes_otel_plugin` 配置段：

```yaml
hermes_otel_plugin:
  enabled: true
  endpoint: http://127.0.0.1:9529/otel
  protocol: http/protobuf
  trace_path: v1/traces
  metrics_path: v1/metrics
  logs_enabled: false
  logs_path: v1/logs
  service_name: hermes-otel-plugin
  sample_rate: 1.0
  flush_interval_ms: 30000
  root_span_ttl_ms: 600000
  trace_payload_debug_enabled: false
  resource_attributes:
    app_name: hermes
  headers: {}
  log_events:
    - session
    - api_request
    - tool
    - subagent
```

默认会强制注入：

- `resource_attributes.agent_runtime=hermes`

## 命令入口

插件会注册以下会话 slash commands：

```text
/otel-status
/otel-config
/otel-test-export
```

仓库里也保留了 `register_cli_command()` 实现，对应目标命令为：

```bash
hermes hermes-otel-plugin status
hermes hermes-otel-plugin show-config
hermes hermes-otel-plugin test-export
```

但当前本机 Hermes 版本在 `main.py` 里只会自动装配 memory provider 类型的插件 CLI，所以真正可靠的控制入口是上面的 slash commands。

## 开发说明

见 [BUILDING.md](./BUILDING.md)。
