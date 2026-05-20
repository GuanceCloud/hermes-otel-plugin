from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


PLUGIN_PATH = pathlib.Path(__file__).resolve().parent.parent / "__init__.py"
SPEC = importlib.util.spec_from_file_location(
    "hermes_otel_plugin_under_test",
    PLUGIN_PATH,
    submodule_search_locations=[str(PLUGIN_PATH.parent)],
)
PLUGIN = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)


class FakeContext:
    def __init__(self) -> None:
        self.hooks = []
        self.cli_commands = []
        self.commands = []

    def register_hook(self, name, callback) -> None:
        self.hooks.append((name, callback))

    def register_cli_command(self, **kwargs) -> None:
        self.cli_commands.append(kwargs)

    def register_command(self, name, handler, description="") -> None:
        self.commands.append(
            {
                "name": name,
                "handler": handler,
                "description": description,
            }
        )


class PluginRegisterTests(unittest.TestCase):
    def test_register_wires_expected_hooks(self) -> None:
        ctx = FakeContext()
        PLUGIN.register(ctx)
        hook_names = {name for name, _ in ctx.hooks}
        self.assertEqual(
            hook_names,
            {
                "on_session_start",
                "on_session_end",
                "on_session_finalize",
                "on_session_reset",
                "pre_llm_call",
                "post_llm_call",
                "pre_api_request",
                "post_api_request",
                "pre_tool_call",
                "post_tool_call",
                "subagent_stop",
            },
        )
        self.assertEqual(len(ctx.cli_commands), 1)
        self.assertEqual(ctx.cli_commands[0]["name"], "hermes-otel-plugin")
        self.assertEqual(
            {item["name"] for item in ctx.commands},
            {"otel-status", "otel-config", "otel-test-export"},
        )

    def test_child_session_hooks_are_skipped(self) -> None:
        plugin = PLUGIN.HermesOtelPlugin()

        class FakeMetrics:
            def __init__(self) -> None:
                self.calls = []

            def record_session_start(self, session_id, platform) -> None:
                self.calls.append(("start", session_id, platform))

            def record_session_end(self, session_id, platform, outcome) -> None:
                self.calls.append(("end", session_id, platform, outcome))

            def record_session_reset(self, session_id, platform) -> None:
                self.calls.append(("reset", session_id, platform))

        class FakeLogs:
            def __init__(self) -> None:
                self.calls = []

            def emit_session_event(self, *args) -> None:
                self.calls.append(args)

        class FakeTraces:
            def __init__(self) -> None:
                self.finalized = []

            def is_child_session(self, session_id) -> bool:
                return session_id == "child-sess"

            def finalize_session(self, session_id, platform, outcome) -> None:
                self.finalized.append((session_id, platform, outcome))

        plugin._bootstrapped = True
        plugin._config = mock.Mock(enabled=True)
        plugin._metrics = FakeMetrics()
        plugin._logs = FakeLogs()
        plugin._traces = FakeTraces()

        plugin.on_session_start("child-sess", "cli")
        plugin.on_session_end("child-sess", "cli")
        plugin.on_session_finalize("child-sess", "cli")
        plugin.on_session_reset("child-sess", "cli")

        self.assertEqual(plugin._metrics.calls, [])
        self.assertEqual(plugin._logs.calls, [])
        self.assertEqual(plugin._traces.finalized, [])


if __name__ == "__main__":
    unittest.main()
