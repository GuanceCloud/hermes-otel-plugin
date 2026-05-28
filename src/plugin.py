from __future__ import annotations

import logging
import threading
from typing import Any

from . import AGENT_RUNTIME, AGENT_VERSION
from .cli import print_json, setup_cli_parser
from .config import load_plugin_config, resolve_otlp_url
from .log_manager import LogManager
from .metric_manager import MetricManager
from .otel_runtime import OTelRuntime
from .trace_manager import TraceManager


LOGGER = logging.getLogger(__name__)


class HermesOtelPlugin:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bootstrapped = False
        self._config = None
        self._runtime = None
        self._metrics = None
        self._logs = None
        self._traces = None

    def _bootstrap(self) -> None:
        with self._lock:
            if self._bootstrapped:
                return
            self._config = load_plugin_config()
            self._runtime = OTelRuntime(self._config, LOGGER)
            self._metrics = MetricManager(self._runtime, LOGGER)
            self._logs = LogManager(self._runtime, self._config, LOGGER)
            self._traces = TraceManager(self._runtime, self._metrics, self._logs, self._config, LOGGER)
            self._bootstrapped = True

    def _enabled(self) -> bool:
        self._bootstrap()
        return bool(self._config and self._config.enabled)

    def current_config(self) -> dict[str, Any]:
        self._bootstrap()
        assert self._config is not None
        return self._config.to_dict()

    def status(self) -> dict[str, Any]:
        self._bootstrap()
        assert self._config is not None
        assert self._runtime is not None
        runtime_status = self._runtime.status()
        return {
            "name": "hermes-otel-plugin",
            "enabled": self._config.enabled,
            "runtime_started": runtime_status.started,
            "runtime_active": runtime_status.active,
            "runtime_error": runtime_status.error,
            "trace_url": resolve_otlp_url(self._config.endpoint, self._config.trace_path),
            "metrics_url": resolve_otlp_url(self._config.endpoint, self._config.metrics_path),
            "logs_url": resolve_otlp_url(self._config.endpoint, self._config.logs_path),
            "logs_enabled": self._config.logs_enabled,
        }

    def emit_test_telemetry(self) -> dict[str, Any]:
        self._bootstrap()
        assert self._config is not None
        assert self._runtime is not None
        assert self._metrics is not None
        assert self._logs is not None
        if not self._config.enabled:
            return {"ok": False, "reason": "plugin disabled"}
        self._runtime.ensure_started()
        span = self._runtime.start_span("hermes_otel_plugin.test")
        self._runtime.set_span_attributes(
            span,
            {
                "agent_runtime": AGENT_RUNTIME,
                "agent_version": AGENT_VERSION,
                "session_id": "test-session",
                "platform": "cli",
            },
        )
        self._runtime.add_span_event(span, "manual_test", {"source": "cli"})
        self._runtime.end_span(span, status_code="OK")
        self._metrics.record_session_start("test-session", "cli")
        self._metrics.record_turn_started("test-session", "cli", "test-model")
        self._metrics.record_turn_finished(
            session_id="test-session",
            platform="cli",
            model="test-model",
            provider_name="test-provider",
            response_model="test-model",
            outcome="completed",
            duration_ms=1.0,
        )
        self._logs.emit(
            "session",
            "Hermes OTel plugin test export",
            {
                "agent_runtime": AGENT_RUNTIME,
                "agent_version": AGENT_VERSION,
                "session_id": "test-session",
                "platform": "cli",
                "outcome": "completed",
            },
        )
        self._runtime.force_flush()
        return {"ok": True}

    def handle_cli_command(self, args: Any) -> None:
        action = getattr(args, "hermes_otel_action", None) or "status"
        if action == "status":
            print_json(self.status())
            return
        if action == "show-config":
            print_json(self.current_config())
            return
        if action == "test-export":
            print_json(self.emit_test_telemetry())
            return
        raise SystemExit(f"unknown action: {action}")

    def command_status(self, raw_args: str) -> str:
        del raw_args
        return __import__("json").dumps(self.status(), indent=2, ensure_ascii=False, sort_keys=True)

    def command_config(self, raw_args: str) -> str:
        del raw_args
        return __import__("json").dumps(self.current_config(), indent=2, ensure_ascii=False, sort_keys=True)

    def command_test_export(self, raw_args: str) -> str:
        del raw_args
        return __import__("json").dumps(self.emit_test_telemetry(), indent=2, ensure_ascii=False, sort_keys=True)

    def on_session_start(self, session_id: str, platform: str = "unknown", **_: Any) -> None:
        if not self._enabled():
            return
        assert self._metrics is not None
        assert self._logs is not None
        assert self._traces is not None
        if self._traces.is_child_session(session_id):
            return
        self._metrics.record_session_start(session_id, platform)
        self._logs.emit_session_event("Hermes session started", session_id, platform)

    def on_session_end(
        self,
        session_id: str,
        platform: str = "unknown",
        completed: bool = True,
        interrupted: bool = False,
        **kwargs: Any,
    ) -> None:
        if not self._enabled():
            return
        assert self._metrics is not None
        assert self._logs is not None
        assert self._traces is not None
        if self._traces.is_child_session(session_id):
            return
        outcome = "completed"
        if interrupted:
            outcome = "interrupted"
        elif not completed:
            outcome = "failed"
        self._metrics.record_session_end(session_id, platform, outcome)
        self._logs.emit_session_event("Hermes session ended", session_id, platform, outcome)
        self._traces.finalize_session(session_id, platform=platform, outcome=outcome)

    def on_session_finalize(self, session_id: str | None, platform: str = "unknown", **_: Any) -> None:
        if not self._enabled():
            return
        assert self._traces is not None
        assert self._logs is not None
        if session_id and self._traces.is_child_session(session_id):
            return
        self._traces.finalize_session(session_id, platform=platform, outcome="finalized")
        if session_id:
            self._logs.emit_session_event("Hermes session finalized", session_id, platform, "finalized")

    def on_session_reset(self, session_id: str, platform: str = "unknown", **_: Any) -> None:
        if not self._enabled():
            return
        assert self._metrics is not None
        assert self._logs is not None
        assert self._traces is not None
        if self._traces.is_child_session(session_id):
            return
        self._metrics.record_session_reset(session_id, platform)
        self._logs.emit_session_event("Hermes session reset", session_id, platform, "reset")

    def on_pre_llm_call(self, **kwargs: Any) -> None:
        if not self._enabled():
            return
        assert self._traces is not None
        self._traces.start_turn(**kwargs)

    def on_post_llm_call(self, **kwargs: Any) -> None:
        if not self._enabled():
            return
        assert self._traces is not None
        self._traces.finish_turn(**kwargs)

    def on_pre_api_request(self, **kwargs: Any) -> None:
        if not self._enabled():
            return
        assert self._traces is not None
        self._traces.start_api_request(**kwargs)

    def on_post_api_request(self, **kwargs: Any) -> None:
        if not self._enabled():
            return
        assert self._traces is not None
        self._traces.finish_api_request(**kwargs)

    def on_pre_tool_call(self, **kwargs: Any) -> None:
        if not self._enabled():
            return
        assert self._traces is not None
        self._traces.start_tool_call(**kwargs)

    def on_post_tool_call(self, **kwargs: Any) -> None:
        if not self._enabled():
            return
        assert self._traces is not None
        self._traces.finish_tool_call(**kwargs)

    def on_subagent_stop(self, **kwargs: Any) -> None:
        if not self._enabled():
            return
        assert self._traces is not None
        self._traces.record_subagent_stop(**kwargs)


_PLUGIN = HermesOtelPlugin()


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_start", _PLUGIN.on_session_start)
    ctx.register_hook("on_session_end", _PLUGIN.on_session_end)
    ctx.register_hook("on_session_finalize", _PLUGIN.on_session_finalize)
    ctx.register_hook("on_session_reset", _PLUGIN.on_session_reset)
    ctx.register_hook("pre_llm_call", _PLUGIN.on_pre_llm_call)
    ctx.register_hook("post_llm_call", _PLUGIN.on_post_llm_call)
    ctx.register_hook("pre_api_request", _PLUGIN.on_pre_api_request)
    ctx.register_hook("post_api_request", _PLUGIN.on_post_api_request)
    ctx.register_hook("pre_tool_call", _PLUGIN.on_pre_tool_call)
    ctx.register_hook("post_tool_call", _PLUGIN.on_post_tool_call)
    ctx.register_hook("subagent_stop", _PLUGIN.on_subagent_stop)
    ctx.register_cli_command(
        name="hermes-otel-plugin",
        help="Manage Hermes OTLP telemetry plugin",
        setup_fn=setup_cli_parser,
        handler_fn=_PLUGIN.handle_cli_command,
        description="Inspect config, status, and emit a synthetic OTLP export",
    )
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            "otel-status",
            handler=_PLUGIN.command_status,
            description="Show Hermes OTEL plugin runtime status",
        )
        ctx.register_command(
            "otel-config",
            handler=_PLUGIN.command_config,
            description="Show the resolved Hermes OTEL plugin config",
        )
        ctx.register_command(
            "otel-test-export",
            handler=_PLUGIN.command_test_export,
            description="Emit a synthetic Hermes OTEL export and flush",
        )
