from __future__ import annotations

import logging
from typing import Any

from .config import HermesOtelPluginConfig
from .otel_runtime import OTelRuntime


class LogManager:
    def __init__(
        self,
        runtime: OTelRuntime,
        config: HermesOtelPluginConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self._runtime = runtime
        self._config = config
        self._logger = logger or logging.getLogger(__name__)

    def _enabled_for(self, category: str) -> bool:
        return self._config.logs_enabled and category in set(self._config.log_events)

    def emit(self, category: str, body: str, attrs: dict[str, Any] | None = None, severity: str = "INFO") -> None:
        if not self._enabled_for(category):
            return
        payload = {"log_category": category}
        if attrs:
            payload.update({key: value for key, value in attrs.items() if value is not None})
        self._runtime.emit_log(body, payload, severity_text=severity)

    def emit_session_event(self, body: str, session_id: str, platform: str, outcome: str | None = None) -> None:
        self.emit(
            "session",
            body,
            {
                "agent_runtime": "hermes",
                "session_id": session_id,
                "platform": platform,
                "outcome": outcome,
            },
        )

    def emit_api_request(self, body: str, attrs: dict[str, Any]) -> None:
        self.emit("api_request", body, {"agent_runtime": "hermes", **attrs})

    def emit_tool(self, body: str, attrs: dict[str, Any]) -> None:
        self.emit("tool", body, {"agent_runtime": "hermes", **attrs})

    def emit_subagent(self, body: str, attrs: dict[str, Any]) -> None:
        self.emit("subagent", body, {"agent_runtime": "hermes", **attrs})
