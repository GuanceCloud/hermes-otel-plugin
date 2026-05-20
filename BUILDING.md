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
