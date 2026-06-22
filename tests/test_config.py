from __future__ import annotations

import unittest

from src.config import DEFAULT_LOG_EVENTS, resolve_plugin_config, resolve_otlp_url


class ConfigTests(unittest.TestCase):
    def test_resolve_nested_section(self) -> None:
        cfg = resolve_plugin_config(
            {
                "hermes_otel_plugin": {
                    "endpoint": "http://collector.local/otel/",
                    "trace_path": "/custom/traces/",
                    "logs_enabled": True,
                    "resource_attributes": {
                        "app_name": "hermes-dev",
                    },
                    "headers": {
                        "Authorization": "Bearer token",
                    },
                    "log_events": ["session", "tool"],
                }
            }
        )
        self.assertEqual(cfg.endpoint, "http://collector.local/otel")
        self.assertEqual(cfg.trace_path, "custom/traces")
        self.assertTrue(cfg.logs_enabled)
        self.assertEqual(cfg.resource_attributes["app_name"], "hermes-dev")
        self.assertNotIn("agent_runtime", cfg.resource_attributes)
        self.assertEqual(cfg.headers["Authorization"], "Bearer token")
        self.assertEqual(cfg.log_events, ("session", "tool"))

    def test_reserved_resource_attributes_are_filtered(self) -> None:
        cfg = resolve_plugin_config(
            {
                "hermes_otel_plugin": {
                    "resource_attributes": {
                        "platform": "cli",
                        "session_id": "sess-1",
                        "tool_name": "terminal",
                        "gen_ai.request.model": "gpt-test",
                        "error.type": "token_invalidated",
                        "agent_version": "1.2.3",
                        "deployment.environment": "dev",
                    }
                }
            }
        )
        self.assertEqual(cfg.resource_attributes, {"deployment.environment": "dev"})

    def test_invalid_values_fall_back(self) -> None:
        cfg = resolve_plugin_config(
            {
                "hermes_otel_plugin": {
                    "sample_rate": 100,
                    "flush_interval_ms": 10,
                    "root_span_ttl_ms": 0,
                    "log_events": [],
                }
            }
        )
        self.assertEqual(cfg.sample_rate, 1.0)
        self.assertEqual(cfg.flush_interval_ms, 1_000)
        self.assertEqual(cfg.root_span_ttl_ms, 1_000)
        self.assertEqual(cfg.log_events, DEFAULT_LOG_EVENTS)

    def test_resolve_otlp_url(self) -> None:
        self.assertEqual(
            resolve_otlp_url("http://127.0.0.1:4318/otel/", "/v1/traces/"),
            "http://127.0.0.1:4318/otel/v1/traces",
        )


if __name__ == "__main__":
    unittest.main()
