# Changelog

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
