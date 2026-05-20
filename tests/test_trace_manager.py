from __future__ import annotations

import unittest
from unittest.mock import patch

from src.config import HermesOtelPluginConfig
from src.log_manager import LogManager
from src.metric_manager import MetricManager
from src.trace_manager import TraceManager


class FakeSpan:
    def __init__(self, name: str, parent=None, start_time_ns=None) -> None:
        self.name = name
        self.parent = parent
        self.start_time_ns = start_time_ns
        self.end_time_ns = None
        self.attributes = {}
        self.events = []
        self.ended = False
        self.status_code = None
        self.description = None

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value

    def add_event(self, name, attributes=None) -> None:
        self.events.append((name, attributes or {}))

    def set_status(self, status) -> None:
        self.status_code = getattr(status, "status_code", None)
        self.description = getattr(status, "description", None)

    def end(self, end_time=None) -> None:
        self.ended = True
        self.end_time_ns = end_time


class FakeStatus:
    def __init__(self, status_code, description="") -> None:
        self.status_code = status_code
        self.description = description


class FakeStatusCode:
    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


class FakeTraceApi:
    StatusCode = FakeStatusCode
    Status = FakeStatus

    @staticmethod
    def set_span_in_context(span):
        return span


class FakeInstrument:
    def __init__(self) -> None:
        self.calls = []

    def add(self, value, attrs) -> None:
        self.calls.append(("add", value, attrs))

    def record(self, value, attrs) -> None:
        self.calls.append(("record", value, attrs))


class FakeMeter:
    def create_counter(self, *_, **__) -> FakeInstrument:
        return FakeInstrument()

    def create_histogram(self, *_, **__) -> FakeInstrument:
        return FakeInstrument()


class FakeRuntime:
    def __init__(self) -> None:
        self._otel_trace = FakeTraceApi()
        self._meter = FakeMeter()
        self.spans = []
        self.logs = []

    def get_meter(self):
        return self._meter

    def start_span(self, name, parent_span=None, start_time_ns=None):
        span = FakeSpan(name, parent=parent_span, start_time_ns=start_time_ns)
        self.spans.append(span)
        return span

    def set_span_attributes(self, span, attrs):
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, value)

    def add_span_event(self, span, name, attrs=None):
        span.add_event(name, attrs)

    def end_span(self, span, end_time_ns=None, status_code="UNSET", description=""):
        span.set_status(FakeStatus(status_code, description))
        span.end(end_time=end_time_ns)

    def emit_log(self, body, attributes=None, severity_text="INFO"):
        self.logs.append((body, attributes or {}, severity_text))


class FakeLineage:
    def __init__(self, parents=None) -> None:
        self.parents = parents or {}

    def get_parent_session_id(self, session_id):
        return self.parents.get(session_id)

    def is_child_session(self, session_id):
        return bool(self.get_parent_session_id(session_id))


class TraceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        config = HermesOtelPluginConfig(logs_enabled=True)
        runtime = FakeRuntime()
        metrics = MetricManager(runtime)
        logs = LogManager(runtime, config)
        self.lineage = FakeLineage()
        self.manager = TraceManager(runtime, metrics, logs, config, lineage=self.lineage)
        self.runtime = runtime

    def test_turn_api_and_tool_flow(self) -> None:
        self.manager.start_turn(
            session_id="sess-1",
            user_message="hello",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="sess-1",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            api_mode="responses",
        )
        self.manager.finish_api_request(
            session_id="sess-1",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            api_duration=0.25,
            finish_reason="stop",
            response_model="gpt-test",
            usage={
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_tokens": 2,
                "cache_write_tokens": 1,
                "total_tokens": 16,
            },
        )
        self.manager.start_tool_call(
            tool_name="terminal",
            args={"cmd": "echo hello"},
            session_id="sess-1",
            platform="cli",
            tool_call_id="tool-1",
        )
        self.manager.finish_tool_call(
            tool_name="terminal",
            args={"cmd": "echo hello"},
            result='{"output":"hello"}',
            session_id="sess-1",
            platform="cli",
            tool_call_id="tool-1",
        )
        self.manager.finish_turn(
            session_id="sess-1",
            assistant_response="done",
            completed=True,
            interrupted=False,
            platform="cli",
        )

        span_names = [span.name for span in self.runtime.spans]
        self.assertIn("hermes_request", span_names)
        self.assertIn("agent_run", span_names)
        self.assertIn("llm", span_names)
        self.assertIn("tool:terminal", span_names)
        self.assertTrue(all(span.ended for span in self.runtime.spans))
        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        self.assertEqual(llm_span.attributes["usage_input_tokens"], 10)
        self.assertEqual(llm_span.attributes["usage_output_tokens"], 3)
        self.assertEqual(llm_span.attributes["usage_total_tokens"], 13)
        self.assertEqual(llm_span.attributes["usage_cache_read_input_tokens"], 2)
        self.assertEqual(llm_span.attributes["usage_cache_write_input_tokens"], 1)
        self.assertEqual(llm_span.attributes["usage_cache_total_tokens"], 3)
        self.assertEqual(llm_span.attributes["input_preview"], "hello")
        self.assertEqual(llm_span.attributes["output_preview"], "toolCall:terminal")
        self.assertEqual(llm_span.attributes["output_kind"], "tool_call")
        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        self.assertEqual(root_span.attributes["output_preview"], "done")
        self.assertEqual(root_span.attributes["usage_input_tokens"], 10)
        self.assertEqual(root_span.attributes["usage_output_tokens"], 3)
        self.assertEqual(root_span.attributes["usage_total_tokens"], 13)
        tool_span = next(span for span in self.runtime.spans if span.name == "tool:terminal")
        self.assertEqual(tool_span.attributes["tool_phase"], "result")
        self.assertEqual(tool_span.attributes["tool_outcome"], "completed")
        self.assertEqual(tool_span.attributes["tool_command"], "echo hello")
        self.assertEqual(tool_span.attributes["tool_result_preview"], '{"output": "hello"}')
        self.assertNotIn("tool_result_status", tool_span.attributes)
        tool_metric_attrs = self.manager._metrics._tool_call_count.calls[0][2]
        self.assertEqual(tool_metric_attrs["tool_name"], "terminal")
        self.assertEqual(tool_metric_attrs["outcome"], "completed")
        self.assertNotIn("tool_result_status", tool_metric_attrs)
        tool_operation_call = next(
            call
            for call in self.manager._metrics._operation_count.calls
            if call[2].get("operation_name") == "tool"
        )
        self.assertEqual(tool_operation_call[2]["model_name"], "gpt-test")
        request_count_call = self.manager._metrics._request_count.calls[0]
        self.assertEqual(request_count_call[2]["request_type"], "user_request")
        self.assertEqual(request_count_call[2]["session_state"], "completed")
        session_token_input_call = self.manager._metrics._session_token_input.calls[0]
        session_token_total_call = self.manager._metrics._session_token_total.calls[0]
        self.assertEqual(session_token_input_call[1], 10.0)
        self.assertEqual(session_token_total_call[1], 13.0)
        session_token_usage_total_call = next(
            call
            for call in self.manager._metrics._session_token_usage.calls
            if call[2].get("token_type") == "total"
        )
        self.assertEqual(session_token_usage_total_call[1], 13.0)
        token_usage_calls = self.manager._metrics._token_usage.calls
        total_call = next(call for call in token_usage_calls if call[2]["token_type"] == "total")
        self.assertEqual(total_call[1], 13.0)

    def test_llm_previews_are_synthesized_without_host_changes(self) -> None:
        self.manager.start_turn(
            session_id="sess-preview",
            user_message="build dashboard",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="sess-preview",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
        )
        self.manager.finish_api_request(
            session_id="sess-preview",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            response_model="gpt-test",
            assistant_tool_call_count=2,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        self.manager.start_tool_call(
            tool_name="read_file",
            args={"path": "/tmp/a"},
            session_id="sess-preview",
            platform="cli",
            tool_call_id="tool-a",
        )
        self.manager.finish_tool_call(
            tool_name="read_file",
            args={"path": "/tmp/a"},
            result='{"content":"abc"}',
            session_id="sess-preview",
            platform="cli",
            tool_call_id="tool-a",
        )
        self.manager.start_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "x"},
            session_id="sess-preview",
            platform="cli",
            tool_call_id="tool-b",
        )
        self.manager.finish_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "x"},
            result='{"files":["/tmp/x"]}',
            session_id="sess-preview",
            platform="cli",
            tool_call_id="tool-b",
        )
        self.manager.start_api_request(
            session_id="sess-preview",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=2,
        )
        self.manager.finish_api_request(
            session_id="sess-preview",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=2,
            response_model="gpt-test",
            assistant_tool_call_count=0,
            usage={"input_tokens": 2, "output_tokens": 3},
        )
        self.manager.finish_turn(
            session_id="sess-preview",
            assistant_response="final answer",
            completed=True,
            platform="cli",
        )

        llm_spans = [span for span in self.runtime.spans if span.name == "llm"]
        self.assertEqual(llm_spans[0].attributes["input_preview"], "build dashboard")
        self.assertEqual(llm_spans[0].attributes["output_kind"], "tool_call")
        self.assertEqual(llm_spans[0].attributes["output_preview"], "toolCall:read_file,search_files")
        self.assertNotIn("input_preview", llm_spans[1].attributes)
        self.assertIn("read_file:", llm_spans[1].attributes["tool_context_preview"])
        self.assertIn("search_files:", llm_spans[1].attributes["tool_context_preview"])
        self.assertEqual(llm_spans[1].attributes["output_kind"], "text")
        self.assertEqual(llm_spans[1].attributes["output_preview"], "final answer")

    def test_llm_output_preview_deduplicates_repeated_tool_names(self) -> None:
        self.manager.start_turn(
            session_id="sess-dup-tools",
            user_message="dup tools",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="sess-dup-tools",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
        )
        self.manager.finish_api_request(
            session_id="sess-dup-tools",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            response_model="gpt-test",
            assistant_tool_call_count=2,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        self.manager.start_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "a"},
            session_id="sess-dup-tools",
            platform="cli",
            tool_call_id="tool-dup-1",
        )
        self.manager.start_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "b"},
            session_id="sess-dup-tools",
            platform="cli",
            tool_call_id="tool-dup-2",
        )
        self.manager.finish_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "a"},
            result='{"files":["/tmp/a"]}',
            session_id="sess-dup-tools",
            platform="cli",
            tool_call_id="tool-dup-1",
        )
        self.manager.finish_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "b"},
            result='{"files":["/tmp/b"]}',
            session_id="sess-dup-tools",
            platform="cli",
            tool_call_id="tool-dup-2",
        )
        self.manager.finish_turn(
            session_id="sess-dup-tools",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        self.assertEqual(llm_span.attributes["output_preview"], "toolCall:search_files")

    def test_auto_review_turn_is_marked_on_root_and_agent_spans(self) -> None:
        review_prompt = (
            "Review the conversation above and consider saving or updating a skill if appropriate. "
            "Focus on: was a non-trivial approach used to complete a task that required trial and error"
        )
        self.manager.start_turn(
            session_id="sess-review",
            user_message=review_prompt,
            conversation_history=["x"],
            is_first_turn=False,
            model="gpt-test",
            platform="cli",
        )
        self.manager.finish_turn(
            session_id="sess-review",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        agent_span = next(span for span in self.runtime.spans if span.name == "agent_run")
        for span in (root_span, agent_span):
            self.assertEqual(span.attributes["request_type"], "auto_review")
            self.assertEqual(span.attributes["review_category"], "skill")
            self.assertEqual(span.attributes["is_auto_review"], True)
        request_count_call = self.manager._metrics._request_count.calls[0]
        self.assertEqual(request_count_call[2]["request_type"], "auto_review")
        self.assertEqual(request_count_call[2]["review_category"], "skill")

    def test_llm_zero_usage_and_response_model_fallback_align_with_openclaw(self) -> None:
        self.manager.start_turn(
            session_id="sess-zero",
            user_message="hello",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="sess-zero",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            approx_input_tokens=123,
            request_char_count=5,
        )
        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        self.assertEqual(llm_span.attributes["approx_input_tokens"], 123)
        self.assertNotIn("usage_input_tokens", llm_span.attributes)

        self.manager.finish_api_request(
            session_id="sess-zero",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            response_model=None,
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 999,
            },
        )
        self.manager.finish_turn(
            session_id="sess-zero",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        self.assertEqual(llm_span.attributes["response_model"], "gpt-test")
        self.assertEqual(llm_span.attributes["usage_input_tokens"], 0)
        self.assertEqual(llm_span.attributes["usage_output_tokens"], 0)
        self.assertEqual(llm_span.attributes["usage_total_tokens"], 0)
        self.assertEqual(llm_span.attributes["usage_cache_read_input_tokens"], 0)
        self.assertEqual(llm_span.attributes["usage_cache_write_input_tokens"], 0)
        self.assertEqual(llm_span.attributes["usage_cache_total_tokens"], 0)

        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        self.assertEqual(root_span.attributes["response_model"], "gpt-test")
        self.assertEqual(root_span.attributes["usage_input_tokens"], 0)
        self.assertEqual(root_span.attributes["usage_output_tokens"], 0)
        self.assertEqual(root_span.attributes["usage_total_tokens"], 0)
        self.assertEqual(root_span.attributes["usage_cache_total_tokens"], 0)

        token_usage_calls = self.manager._metrics._token_usage.calls
        total_call = next(call for call in token_usage_calls if call[2]["token_type"] == "total")
        self.assertEqual(total_call[1], 0.0)

    def test_subagent_span(self) -> None:
        self.manager.start_turn(
            session_id="sess-2",
            user_message="delegate",
            conversation_history=[],
            is_first_turn=False,
            model="gpt-test",
            platform="cli",
        )
        self.manager.record_subagent_stop(
            parent_session_id="sess-2",
            child_role="coder",
            child_summary="finished",
            child_status="completed",
            duration_ms=123,
        )
        self.assertIn("subagent:coder", [span.name for span in self.runtime.spans])

    def test_subagent_span_attaches_to_delegate_task(self) -> None:
        self.manager.start_turn(
            session_id="sess-2b",
            user_message="delegate",
            conversation_history=[],
            is_first_turn=False,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="delegate_task",
            args={"goal": "research", "profile": "coder"},
            session_id="sess-2b",
            platform="cli",
            tool_call_id="call-delegate-1",
        )
        self.manager.finish_tool_call(
            tool_name="delegate_task",
            args={"goal": "research", "profile": "coder"},
            result='{"results":[{"task_index":0,"status":"completed","summary":"ok"}]}',
            session_id="sess-2b",
            platform="cli",
            tool_call_id="call-delegate-1",
        )
        delegate_span = next(span for span in self.runtime.spans if span.name == "tool:delegate_task")

        self.manager.record_subagent_stop(
            parent_session_id="sess-2b",
            child_role="leaf",
            child_summary="finished",
            child_status="completed",
            duration_ms=123,
        )

        subagent_span = next(span for span in self.runtime.spans if span.name == "subagent:coder")
        self.assertIs(subagent_span.parent, delegate_span)
        self.assertEqual(subagent_span.attributes["subagent_role"], "coder")
        self.assertEqual(subagent_span.attributes["subagent_runtime_role"], "leaf")

    def test_active_turn_does_not_expire_based_on_total_duration(self) -> None:
        self.manager._config.root_span_ttl_ms = 1_000
        self.manager.start_turn(
            session_id="sess-ttl",
            user_message="long request",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        turn = self.manager._store.get_turn("sess-ttl")
        self.assertIsNotNone(turn)
        turn.started_monotonic_ns = 0
        turn.last_activity_monotonic_ns = 900_000_000

        with (
            patch("src.state_store.time.monotonic_ns", return_value=1_500_000_000),
            patch("src.trace_manager._mono_ns", return_value=1_500_000_000),
        ):
            self.manager.start_api_request(
                session_id="sess-ttl",
                platform="cli",
                model="gpt-test",
                provider="openai",
                api_call_count=1,
            )

        root_spans = [span for span in self.runtime.spans if span.name == "hermes_request"]
        self.assertEqual(len(root_spans), 1)
        active_turn = self.manager._store.get_turn("sess-ttl")
        self.assertIsNotNone(active_turn)
        self.assertEqual(active_turn.last_activity_monotonic_ns, 1_500_000_000)

    def test_child_session_skips_root_turn_spans(self) -> None:
        self.lineage.parents["child-1"] = "parent-1"
        self.manager.start_turn(
            session_id="child-1",
            user_message="delegate",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="child-1",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
        )
        self.manager.start_tool_call(
            tool_name="terminal",
            args={"cmd": "echo child"},
            session_id="child-1",
            platform="cli",
            tool_call_id="tool-child-1",
        )
        self.manager.finish_turn(
            session_id="child-1",
            assistant_response="done",
            completed=True,
            platform="cli",
        )
        self.assertEqual(self.runtime.spans, [])

    def test_multiple_llm_spans_keep_per_call_token_usage(self) -> None:
        self.manager.start_turn(
            session_id="sess-3",
            user_message="multi",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="sess-3",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
        )
        self.manager.finish_api_request(
            session_id="sess-3",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            response_model="gpt-test",
            usage={
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 100,
                "cache_write_tokens": 0,
                "total_tokens": 118,
            },
        )
        self.manager.start_api_request(
            session_id="sess-3",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=2,
        )
        self.manager.finish_api_request(
            session_id="sess-3",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=2,
            response_model="gpt-test",
            usage={
                "input_tokens": 13,
                "output_tokens": 5,
                "cache_read_tokens": 200,
                "cache_write_tokens": 0,
                "total_tokens": 218,
            },
        )
        self.manager.finish_turn(
            session_id="sess-3",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        llm_spans = [span for span in self.runtime.spans if span.name == "llm"]
        self.assertEqual(len(llm_spans), 2)
        self.assertEqual(llm_spans[0].attributes["usage_input_tokens"], 11)
        self.assertEqual(llm_spans[0].attributes["usage_output_tokens"], 7)
        self.assertEqual(llm_spans[0].attributes["usage_total_tokens"], 18)
        self.assertEqual(llm_spans[0].attributes["usage_cache_total_tokens"], 100)
        self.assertEqual(llm_spans[1].attributes["usage_input_tokens"], 13)
        self.assertEqual(llm_spans[1].attributes["usage_output_tokens"], 5)
        self.assertEqual(llm_spans[1].attributes["usage_total_tokens"], 18)
        self.assertEqual(llm_spans[1].attributes["usage_cache_total_tokens"], 100)
        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        self.assertEqual(root_span.attributes["usage_input_tokens"], 24)
        self.assertEqual(root_span.attributes["usage_output_tokens"], 12)
        self.assertEqual(root_span.attributes["usage_total_tokens"], 36)
        self.assertEqual(root_span.attributes["usage_cache_total_tokens"], 200)

    def test_cache_usage_falls_back_to_raw_value_when_counter_resets(self) -> None:
        self.manager.start_turn(
            session_id="sess-cache-reset",
            user_message="cache reset",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="sess-cache-reset",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
        )
        self.manager.finish_api_request(
            session_id="sess-cache-reset",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            response_model="gpt-test",
            usage={
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 100,
                "cache_write_tokens": 10,
            },
        )
        self.manager.start_api_request(
            session_id="sess-cache-reset",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=2,
        )
        self.manager.finish_api_request(
            session_id="sess-cache-reset",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=2,
            response_model="gpt-test",
            usage={
                "input_tokens": 13,
                "output_tokens": 5,
                "cache_read_tokens": 20,
                "cache_write_tokens": 2,
            },
        )
        self.manager.finish_turn(
            session_id="sess-cache-reset",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        llm_spans = [span for span in self.runtime.spans if span.name == "llm"]
        self.assertEqual(llm_spans[0].attributes["usage_cache_read_input_tokens"], 100)
        self.assertEqual(llm_spans[0].attributes["usage_cache_write_input_tokens"], 10)
        self.assertEqual(llm_spans[1].attributes["usage_cache_read_input_tokens"], 20)
        self.assertEqual(llm_spans[1].attributes["usage_cache_write_input_tokens"], 2)
        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        self.assertEqual(root_span.attributes["usage_cache_read_input_tokens"], 120)
        self.assertEqual(root_span.attributes["usage_cache_write_input_tokens"], 12)

    def test_delegate_task_without_tool_call_id_records_ok(self) -> None:
        self.manager.start_turn(
            session_id="sess-4",
            user_message="delegate",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="delegate_task",
            args={"goal": "research", "profile": "researcher"},
            session_id="sess-4",
            platform="cli",
            tool_call_id="",
        )
        self.manager.finish_tool_call(
            tool_name="delegate_task",
            args={"goal": "research", "profile": "researcher"},
            result='{"results":[{"task_index":0,"status":"completed","summary":"ok","tool_trace":[{"tool":"x","status":"error"}]}],"total_duration_seconds":1.2}',
            session_id="sess-4",
            platform="cli",
            tool_call_id="",
        )
        self.manager.finish_turn(
            session_id="sess-4",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        tool_span = next(span for span in self.runtime.spans if span.name == "tool:delegate_task")
        self.assertEqual(tool_span.attributes["tool_outcome"], "completed")
        self.assertNotIn("tool_result_status", tool_span.attributes)
        self.assertEqual(tool_span.status_code, "OK")

    def test_duplicate_pre_tool_call_without_tool_call_id_is_ignored(self) -> None:
        self.manager.start_turn(
            session_id="sess-5",
            user_message="dup",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="skill_view",
            args={"name": "dashboard"},
            session_id="sess-5",
            platform="cli",
            tool_call_id="call-skill-1",
        )
        self.manager.start_tool_call(
            tool_name="skill_view",
            args={"name": "dashboard"},
            session_id="sess-5",
            platform="cli",
            tool_call_id="",
        )
        self.manager.finish_tool_call(
            tool_name="skill_view",
            args={"name": "dashboard"},
            result='{"success": true, "name": "dashboard", "description": "desc", "content": "body"}',
            session_id="sess-5",
            platform="cli",
            tool_call_id="call-skill-1",
        )
        self.manager.finish_turn(
            session_id="sess-5",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        tool_spans = [span for span in self.runtime.spans if span.name == "tool:skill_view"]
        self.assertEqual(len(tool_spans), 1)
        self.assertEqual(tool_spans[0].status_code, "OK")

    def test_skill_view_emits_skill_span(self) -> None:
        self.manager.start_turn(
            session_id="sess-6",
            user_message="skill",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="skill_view",
            args={"name": "dql"},
            session_id="sess-6",
            platform="cli",
            tool_call_id="call-skill-2",
        )
        self.manager.finish_tool_call(
            tool_name="skill_view",
            args={"name": "dql"},
            result='{"success": true, "name": "dql", "description": "DQL skill", "content": "---\\nname: dql\\n...", "tags": ["query"], "related_skills": ["dashboard"]}',
            session_id="sess-6",
            platform="cli",
            tool_call_id="call-skill-2",
        )
        self.manager.finish_turn(
            session_id="sess-6",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        skill_span = next(span for span in self.runtime.spans if span.name == "skill:dql")
        self.assertEqual(skill_span.status_code, "OK")
        self.assertEqual(skill_span.description, "completed")
        self.assertEqual(skill_span.attributes["skill_name"], "dql")
        self.assertEqual(skill_span.attributes["skill_source"], "runtime")
        self.assertEqual(skill_span.attributes["skill_description"], "DQL skill")
        self.assertEqual(skill_span.attributes["skill_tags"], "query")
        self.assertEqual(skill_span.attributes["skill_related_skills"], "dashboard")
        self.assertEqual(skill_span.attributes["skill_source_tool_call_id"], "call-skill-2")
        skill_activation_call = self.manager._metrics._skill_activation_count.calls[0]
        self.assertEqual(skill_activation_call[2]["skill_name"], "dql")
        self.assertEqual(skill_activation_call[2]["skill_source"], "runtime")
        skill_operation_call = next(
            call
            for call in self.manager._metrics._operation_count.calls
            if call[2].get("operation_name") == "skill"
        )
        self.assertEqual(skill_operation_call[2]["skill_name"], "dql")
        self.assertEqual(skill_operation_call[2]["skill_source"], "runtime")
        self.assertEqual(skill_operation_call[2]["outcome"], "completed")

    def test_skill_span_stays_open_across_llm_and_closes_on_turn_end(self) -> None:
        self.manager.start_turn(
            session_id="sess-6b",
            user_message="skill then llm",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="skill_view",
            args={"name": "dashboard"},
            session_id="sess-6b",
            platform="cli",
            tool_call_id="call-skill-2b",
        )
        self.manager.finish_tool_call(
            tool_name="skill_view",
            args={"name": "dashboard"},
            result='{"success": true, "name": "dashboard", "description": "Dash skill", "content": "---\\nname: dashboard\\n..."}',
            session_id="sess-6b",
            platform="cli",
            tool_call_id="call-skill-2b",
        )

        skill_span = next(span for span in self.runtime.spans if span.name == "skill:dashboard")
        self.assertFalse(skill_span.ended)

        self.manager.start_api_request(
            session_id="sess-6b",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
        )

        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        self.assertFalse(skill_span.ended)
        self.assertEqual(llm_span.attributes["skill_count"], 1)
        self.assertEqual(llm_span.attributes["skills"], "dashboard")

        self.manager.finish_turn(
            session_id="sess-6b",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        self.assertTrue(skill_span.ended)
        self.assertEqual(skill_span.status_code, "OK")
        self.assertEqual(skill_span.description, "completed")

    def test_multiple_skills_remain_active_for_same_llm(self) -> None:
        self.manager.start_turn(
            session_id="sess-6c",
            user_message="two skills",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="skill_view",
            args={"name": "dashboard"},
            session_id="sess-6c",
            platform="cli",
            tool_call_id="call-skill-a",
        )
        self.manager.finish_tool_call(
            tool_name="skill_view",
            args={"name": "dashboard"},
            result='{"success": true, "name": "dashboard", "description": "Dash skill", "content": "---\\nname: dashboard\\n..."}',
            session_id="sess-6c",
            platform="cli",
            tool_call_id="call-skill-a",
        )
        self.manager.start_tool_call(
            tool_name="skill_view",
            args={"name": "dql"},
            session_id="sess-6c",
            platform="cli",
            tool_call_id="call-skill-b",
        )
        self.manager.finish_tool_call(
            tool_name="skill_view",
            args={"name": "dql"},
            result='{"success": true, "name": "dql", "description": "DQL skill", "content": "---\\nname: dql\\n..."}',
            session_id="sess-6c",
            platform="cli",
            tool_call_id="call-skill-b",
        )

        dashboard_span = next(span for span in self.runtime.spans if span.name == "skill:dashboard")
        dql_span = next(span for span in self.runtime.spans if span.name == "skill:dql")
        self.assertFalse(dashboard_span.ended)
        self.assertFalse(dql_span.ended)

        self.manager.start_api_request(
            session_id="sess-6c",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
        )

        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        self.assertEqual(llm_span.attributes["skill_count"], 2)
        self.assertEqual(llm_span.attributes["skills"], "dashboard,dql")
        self.assertFalse(dashboard_span.ended)
        self.assertFalse(dql_span.ended)

    def test_empty_tool_call_id_is_upgraded_when_real_id_arrives(self) -> None:
        self.manager.start_turn(
            session_id="sess-7",
            user_message="upgrade",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "x", "target": "files"},
            session_id="sess-7",
            platform="cli",
            tool_call_id="",
        )
        self.manager.start_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "x", "target": "files"},
            session_id="sess-7",
            platform="cli",
            tool_call_id="call-real-1",
        )
        self.manager.finish_tool_call(
            tool_name="search_files",
            args={"path": "/tmp", "pattern": "x", "target": "files"},
            result='{"total_count": 1, "files": ["/tmp/x"]}',
            session_id="sess-7",
            platform="cli",
            tool_call_id="call-real-1",
        )
        self.manager.finish_turn(
            session_id="sess-7",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        tool_spans = [span for span in self.runtime.spans if span.name == "tool:search_files"]
        self.assertEqual(len(tool_spans), 1)
        self.assertEqual(tool_spans[0].attributes["tool_call_id"], "call-real-1")
        self.assertEqual(tool_spans[0].attributes["tool_phase"], "result")
        self.assertEqual(tool_spans[0].attributes["tool_target"], "/tmp")
        self.assertEqual(tool_spans[0].attributes["tool_outcome"], "completed")
        self.assertNotIn("tool_result_status", tool_spans[0].attributes)
        self.assertEqual(tool_spans[0].status_code, "OK")

    def test_tool_result_status_is_distinct_from_tool_outcome(self) -> None:
        self.manager.start_turn(
            session_id="sess-tool-status",
            user_message="run",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="terminal",
            args={"cmd": "python app.py"},
            session_id="sess-tool-status",
            platform="cli",
            tool_call_id="tool-status-1",
        )
        self.manager.finish_tool_call(
            tool_name="terminal",
            args={"cmd": "python app.py"},
            result='{"status":"success","details":{"status":"completed"},"output":"ok"}',
            session_id="sess-tool-status",
            platform="cli",
            tool_call_id="tool-status-1",
        )
        self.manager.finish_turn(
            session_id="sess-tool-status",
            assistant_response="done",
            completed=True,
            platform="cli",
        )

        tool_span = next(span for span in self.runtime.spans if span.name == "tool:terminal")
        self.assertEqual(tool_span.attributes["tool_outcome"], "completed")
        self.assertEqual(tool_span.attributes["tool_result_status"], "completed")
        self.assertEqual(tool_span.attributes["tool_command"], "python app.py")
        tool_metric_attrs = self.manager._metrics._tool_call_count.calls[0][2]
        self.assertEqual(tool_metric_attrs["tool_result_status"], "completed")
        self.assertEqual(tool_metric_attrs["outcome"], "completed")


if __name__ == "__main__":
    unittest.main()
