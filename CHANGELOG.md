# Changelog

## 0.1.5

- Unified skill trace tags on both `tool:skill_view` and `skill:*`, including `skill.*` and `gen_ai.skill.*`.
- Fixed the span hierarchy so skill spans attach under the triggering tool span, producing `llm -> tool -> skill`.
- Renamed `skill_source_tool_call_id` to `skill_call_id` and removed obsolete `skill_source` trace and metric fields.
- Updated Chinese and English trace, metric, and semantic mapping documentation for the new skill field model.

## 0.1.4

- Added OpenTelemetry GenAI semantic attributes for model input/output messages, request parameters, response metadata, system instructions, tool definitions, and tool call arguments/results.
- Added `gen_ai.client.operation.duration` and `gen_ai.client.token.usage` metrics while retaining the existing `gen_ai.agent.*` model metrics for compatibility.
- Added `api_request_error` hook handling so terminal provider failures are exported on `llm`, `invoke_agent`, and `hermes_request` spans and finalized error sessions are marked `failed`.
- Renamed the primary agent execution span from `agent_run` to `invoke_agent`.
- Updated the installer to resolve GitHub Releases from the repository `.../releases` root for both `latest` and versioned installs.
- Added Chinese and English semantic field mapping documentation.

## 0.1.2

- Fixed `agent_version` so it now reports the real Hermes Agent version instead of the plugin version.
- Added runtime resolution for Hermes Agent version using `hermes_cli.__version__`, with a local source-file fallback.
- Added regression coverage to ensure `agent_runtime=hermes` and the resolved `agent_version` are attached consistently across spans.

## 0.1.1

- Added session metadata export on `hermes_request` and `agent_run`, including `session_key`, structured `session_*` derived fields, gateway timestamps, chat type, and legacy transcript path.
- Updated Chinese and English trace documentation to describe the new session metadata fields and clarify the current `session_file` and `session_cwd` boundaries.

## 0.1.0

- Initialized `hermes-otel-plugin` as a native Hermes Python plugin.
- Added OTLP trace, metric, and optional log export runtime.
- Added Hermes hook mappings for turns, API requests, tools, sessions, and subagents.
- Added plugin CLI commands for status, config inspection, and synthetic export testing.
- Added release packaging output with versioned archive, latest archive, sha256 files, and installer sidecar.
- Added `scripts/install.sh` to install the plugin, install runtime dependencies into Hermes Python, update `~/.hermes/config.yaml`, and best-effort restart the gateway.
- Updated README and BUILDING to make the release installer the primary installation path and keep source linking as a development-only fallback.
- Added GitHub Actions to run tests, build Python distribution artifacts, generate release bundles, and publish tagged GitHub Releases automatically.
