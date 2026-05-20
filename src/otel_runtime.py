from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .config import HermesOtelPluginConfig, resolve_otlp_url


@dataclass(slots=True)
class RuntimeStatus:
    started: bool
    active: bool
    error: str | None


class OTelRuntime:
    def __init__(self, config: HermesOtelPluginConfig, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._started = False
        self._active = False
        self._error: str | None = None
        self._tracer = None
        self._meter = None
        self._otel_trace = None
        self._tracer_provider = None
        self._meter_provider = None
        self._metric_reader = None
        self._otel_logs_api = None
        self._logger_provider = None
        self._otel_logger = None

    @property
    def config(self) -> HermesOtelPluginConfig:
        return self._config

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            started=self._started,
            active=self._active,
            error=self._error,
        )

    def ensure_started(self) -> bool:
        if self._started:
            return self._active

        self._started = True
        if not self._config.enabled:
            self._active = False
            return False

        try:
            from opentelemetry import _logs as otel_logs_api
            from opentelemetry import trace as otel_trace
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource_attrs = {
                "service.name": self._config.service_name,
                **self._config.resource_attributes,
            }
            resource = Resource.create(resource_attrs)

            trace_exporter = OTLPSpanExporter(
                endpoint=resolve_otlp_url(self._config.endpoint, self._config.trace_path),
                headers=self._config.headers,
            )
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))

            metric_exporter = OTLPMetricExporter(
                endpoint=resolve_otlp_url(self._config.endpoint, self._config.metrics_path),
                headers=self._config.headers,
            )
            metric_reader = PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=self._config.flush_interval_ms,
            )
            meter_provider = MeterProvider(
                resource=resource,
                metric_readers=[metric_reader],
            )

            logger_provider = None
            otel_logger = None
            if self._config.logs_enabled:
                log_exporter = OTLPLogExporter(
                    endpoint=resolve_otlp_url(self._config.endpoint, self._config.logs_path),
                    headers=self._config.headers,
                )
                logger_provider = LoggerProvider(resource=resource)
                logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
                otel_logger = logger_provider.get_logger(self._config.service_name)

            self._otel_trace = otel_trace
            self._tracer_provider = tracer_provider
            self._meter_provider = meter_provider
            self._metric_reader = metric_reader
            self._tracer = tracer_provider.get_tracer(self._config.service_name)
            self._meter = meter_provider.get_meter(self._config.service_name)
            self._otel_logs_api = otel_logs_api
            self._logger_provider = logger_provider
            self._otel_logger = otel_logger
            self._active = True
            self._logger.info(
                "[hermes-otel-plugin] exporters enabled -> trace=%s metrics=%s logs=%s",
                resolve_otlp_url(self._config.endpoint, self._config.trace_path),
                resolve_otlp_url(self._config.endpoint, self._config.metrics_path),
                resolve_otlp_url(self._config.endpoint, self._config.logs_path)
                if self._config.logs_enabled
                else "disabled",
            )
            return True
        except Exception as exc:
            self._error = str(exc)
            self._active = False
            self._logger.warning(
                "[hermes-otel-plugin] failed to initialize OTEL runtime: %s",
                exc,
            )
            return False

    def get_tracer(self) -> Any:
        if not self.ensure_started():
            return None
        return self._tracer

    def get_meter(self) -> Any:
        if not self.ensure_started():
            return None
        return self._meter

    def start_span(self, name: str, parent_span: Any = None, start_time_ns: int | None = None) -> Any:
        tracer = self.get_tracer()
        if tracer is None or self._otel_trace is None:
            return None
        context = None
        if parent_span is not None:
            context = self._otel_trace.set_span_in_context(parent_span)
        kwargs: dict[str, Any] = {}
        if context is not None:
            kwargs["context"] = context
        if start_time_ns is not None:
            kwargs["start_time"] = start_time_ns
        return tracer.start_span(name, **kwargs)

    def set_span_attributes(self, span: Any, attrs: dict[str, Any]) -> None:
        if span is None:
            return
        for key, value in attrs.items():
            if value is None:
                continue
            span.set_attribute(key, value)

    def add_span_event(self, span: Any, name: str, attrs: dict[str, Any] | None = None) -> None:
        if span is None:
            return
        span.add_event(name, attributes={k: v for k, v in (attrs or {}).items() if v is not None})

    def end_span(
        self,
        span: Any,
        end_time_ns: int | None = None,
        status_code: str = "UNSET",
        description: str = "",
    ) -> None:
        if span is None or self._otel_trace is None:
            return
        status_code = status_code.upper()
        status_map = {
            "OK": self._otel_trace.StatusCode.OK,
            "ERROR": self._otel_trace.StatusCode.ERROR,
            "UNSET": self._otel_trace.StatusCode.UNSET,
        }
        status_value = status_map.get(status_code, self._otel_trace.StatusCode.UNSET)
        status_description = description if status_value == self._otel_trace.StatusCode.ERROR else None
        span.set_status(
            self._otel_trace.Status(
                status_value,
                status_description,
            )
        )
        if end_time_ns is not None:
            span.end(end_time=end_time_ns)
        else:
            span.end()

    def emit_log(
        self,
        body: str,
        attributes: dict[str, Any] | None = None,
        severity_text: str = "INFO",
    ) -> None:
        if not self.ensure_started() or self._otel_logger is None:
            return
        try:
            severity_name = severity_text.upper()
            severity_number = getattr(
                self._otel_logs_api.SeverityNumber,
                severity_name,
                self._otel_logs_api.SeverityNumber.INFO,
            )
            self._otel_logger.emit(
                timestamp=int(time.time() * 1_000_000_000),
                observed_timestamp=int(time.time() * 1_000_000_000),
                severity_text=severity_name,
                severity_number=severity_number,
                body=body,
                attributes={k: v for k, v in (attributes or {}).items() if v is not None},
            )
        except Exception as exc:
            self._logger.debug("[hermes-otel-plugin] emit_log failed: %s", exc)

    def force_flush(self) -> None:
        if self._tracer_provider is not None:
            try:
                self._tracer_provider.force_flush()
            except Exception:
                pass
        if self._meter_provider is not None:
            try:
                self._meter_provider.force_flush()
            except Exception:
                pass
        if self._logger_provider is not None:
            try:
                self._logger_provider.force_flush()
            except Exception:
                pass

    def shutdown(self) -> None:
        if self._logger_provider is not None:
            try:
                self._logger_provider.shutdown()
            except Exception:
                pass
        if self._meter_provider is not None:
            try:
                self._meter_provider.shutdown()
            except Exception:
                pass
        if self._tracer_provider is not None:
            try:
                self._tracer_provider.shutdown()
            except Exception:
                pass
