# hermes-otel-plugin

`hermes-otel-plugin` exports Hermes session, model, tool, and subagent telemetry to any `OTLP HTTP/protobuf` receiver.

This repository follows the design direction of `openclaw-otel-plugin`, but the implementation is native to the Hermes Python plugin system:

- plugin entrypoint: `plugin.yaml` + `__init__.py`
- telemetry naming: `gen_ai.client.*`, `gen_ai.agent.*`, `gen_ai.runtime.*`
- canonical tags: `agent_runtime`, `session_id`, `platform`, `provider_name`, `request_model`, `response_model`, `tool_name`
- runtime semantic model: Hermes sessions, turns, API requests, tools, and subagents

## Exported Telemetry

### Traces

- `hermes_request`
- `agent_run`
- `llm`
- `skill:<name>`
- `tool:<name>`
- `subagent:<role>`

One user turn maps to one trace. Model calls, tool calls, and delegated child agents become child spans under the turn. A `skill:<name>` span starts after a successful `skill_view` load and remains active until the current turn finishes, representing that skill's effective execution window for the request. Multiple skills may stay active at the same time, so overlapping skill spans are expected. Related `llm` spans also carry `skills` and `skill_count` to show which skills were active for that model call.

Architecture and timing diagrams: [docs/en/architecture.md](docs/en/architecture.md)

Trace / span description: [docs/en/trace-description.md](docs/en/trace-description.md)

Metrics description: [docs/en/metrics.md](docs/en/metrics.md)

Skill timing semantics:

- `tool:skill_view`: the load action and load latency for a skill, usually a short span.
- `skill:<name>`: the effective window during which that skill stays active for the current request, not the file-load latency itself.

Subagent hierarchy:

- `subagent:<role>` normally attaches under `agent_run`.
- When the turn contains a triggering `tool:delegate_task`, `subagent:<role>` attaches under that `tool:delegate_task` span instead to preserve the causal relationship.

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

Notes:

- `gen_ai.agent.operation.*` currently covers `model`, `tool`, `skill`, and `subagent`
- `gen_ai.agent.session.token.*` accumulates request-level token totals into session-level counters
- request metrics also include `request_type` / `review_category` so automatic review flows can be separated from normal user requests

### Logs

When `logs_enabled=true`, the plugin mirrors selected session, API request, tool, and subagent lifecycle events to OTEL logs.

## Install

The recommended path now follows the release-installer model used by `openclaw-otel-plugin`.

### Quick install from a release package

If you already have a built local release package:

```bash
python3 scripts/release.py
bash output/install.sh output/hermes-otel-plugin.tar.gz \
  --type otlp \
  --endpoint http://127.0.0.1:9529/otel
```

For GTrace:

```bash
python3 scripts/release.py
bash output/install.sh output/hermes-otel-plugin.tar.gz \
  --type gtrace \
  --endpoint https://llm-openway.guance.com \
  --x-token <TOKEN>
```

The installer will:

- install the plugin under `~/.hermes/plugins/hermes-otel-plugin`
- install runtime Python dependencies into the Hermes runtime python
- enable the plugin in `~/.hermes/config.yaml`
- write the `hermes_otel_plugin` config section
- try `hermes gateway restart` as a best-effort final step

Useful flags:

- `--no-config`
- `--no-deps`
- `--no-restart`
- `--tag KEY=VALUE`
- `--service-name NAME`

### Install from a published release endpoint

The installer also supports the same latest/version pattern as `openclaw-otel-plugin`:

```bash
OSS_ENDPOINT=https://example.com \
bash scripts/install.sh latest \
  --type gtrace \
  --endpoint https://llm-openway.guance.com \
  --x-token <TOKEN>
```

or:

```bash
OSS_ENDPOINT=https://example.com \
bash scripts/install.sh v0.1.0 \
  --type otlp \
  --endpoint http://127.0.0.1:9529/otel
```

The script automatically appends `/hermes-otel-plugin` to `OSS_ENDPOINT` when needed.

### Source install

Manual source install is still supported when you are actively developing the plugin:

```text
~/.hermes/plugins/hermes-otel-plugin -> /path/to/hermes-otel-plugin
```

You then need to:

- ensure `plugins.enabled` contains `hermes-otel-plugin`
- ensure `hermes_otel_plugin.enabled=true`
- install runtime dependencies in the Hermes Python environment

## Configuration

Configure the plugin in `~/.hermes/config.yaml` under `hermes_otel_plugin`:

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

## Commands

The plugin registers three session slash commands:

```text
/otel-status
/otel-config
/otel-test-export
```

The repository also includes a `register_cli_command()` implementation for:

```bash
hermes hermes-otel-plugin status
hermes hermes-otel-plugin show-config
hermes hermes-otel-plugin test-export
```

but the Hermes build currently installed on this machine only auto-wires memory-provider plugin CLI trees in `main.py`, so the slash commands are the reliable control path today.

## Development

See [BUILDING.md](./BUILDING.md).
