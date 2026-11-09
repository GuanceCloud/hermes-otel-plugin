from __future__ import annotations

import unittest
from unittest.mock import patch

from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from src.config import HermesOtelPluginConfig
from src.metric_manager import MetricManager
from src.otel_runtime import OTelRuntime


class RuntimeMetricResourceTests(unittest.TestCase):
    def test_all_exported_metrics_include_runtime_identity(self) -> None:
        reader = InMemoryMetricReader()
        config = HermesOtelPluginConfig(
            enabled=True,
            resource_attributes={"deployment.environment": "test", "agent_runtime": "other"},
        )
        runtime = OTelRuntime(config)
        with (
            patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"),
            patch("opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"),
            patch("opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader", return_value=reader),
        ):
            self.assertTrue(runtime.ensure_started(), runtime.status().error)
        try:
            metrics = MetricManager(runtime)
            metrics.record_turn_finished("session", "cli", "model", "completed", 100)
            metrics.record_api_request("session", "cli", "model", "provider", 50, "ok",
                                       usage={"input_tokens": 10, "output_tokens": 2})
            metrics.record_tool_call("session", "cli", "terminal", 10, "ok")
            metrics.record_skill_operation("session", "example", 10, "ok")
            payload = encode_metrics(reader.get_metrics_data())
            names = set()
            for resource_metrics in payload.resource_metrics:
                attrs = {item.key: item.value.string_value for item in resource_metrics.resource.attributes}
                self.assertEqual(attrs["agent_runtime"], "hermes")
                self.assertEqual(attrs["deployment.environment"], "test")
                for scope in resource_metrics.scope_metrics:
                    names.update(metric.name for metric in scope.metrics)
            self.assertEqual(names, {
                "gen_ai.workflow.duration", "gen_ai.agent.operation.count",
                "gen_ai.client.operation.duration", "gen_ai.client.token.usage",
            })
        finally:
            runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
