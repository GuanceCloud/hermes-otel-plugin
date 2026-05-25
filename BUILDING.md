# hermes-otel-plugin Building

## Requirements

- Python `3.10+`
- Hermes with plugin support
- OTLP receiver for end-to-end validation

## Local Checks

Run unit tests:

```bash
python -m unittest discover -s tests -v
```

Optional packaging metadata check:

```bash
python -m compileall .
```

## Release Packaging

Create release artifacts:

```bash
python3 scripts/release.py
```

Release output:

- `output/hermes-otel-plugin-v0.1.0.tar.gz`
- `output/hermes-otel-plugin-v0.1.0.tar.gz.sha256`
- `output/hermes-otel-plugin.tar.gz`
- `output/hermes-otel-plugin.tar.gz.sha256`
- `output/install.sh`

The versioned archive is immutable release output.
`hermes-otel-plugin.tar.gz` is the latest archive that can be overwritten per release.

The installer is the primary operational entrypoint for end users.

## Manual Validation

1. Confirm the plugin is discoverable:

```bash
hermes plugins list
```

2. Confirm the plugin is discoverable and enabled:

```bash
hermes plugins list
```

3. Start a Hermes session and use:

```text
/otel-status
/otel-config
/otel-test-export
```

The repo also implements `register_cli_command()`, but the currently installed Hermes CLI only auto-loads memory-provider plugin subcommands, so `hermes hermes-otel-plugin ...` is not expected to work until Hermes expands general plugin CLI discovery.

## Source Install

For active development, source install is still valid:

```bash
mkdir -p ~/.hermes/plugins
ln -snf /path/to/hermes-otel-plugin ~/.hermes/plugins/hermes-otel-plugin
```

You must then install dependencies into the Hermes Python runtime and update `~/.hermes/config.yaml` manually.

## Release Install

Local release install:

```bash
python3 scripts/release.py
bash output/install.sh output/hermes-otel-plugin.tar.gz --type otlp --endpoint http://127.0.0.1:9529/otel
```

Published release install:

```bash
OSS_ENDPOINT=https://example.com bash scripts/install.sh latest --type gtrace --endpoint https://llm-openway.guance.com --x-token <TOKEN>
```
