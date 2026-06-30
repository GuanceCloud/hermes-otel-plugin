from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import HermesOtelPluginConfig
from src.log_manager import LogManager
from src.metric_manager import MetricManager
from src import AGENT_RUNTIME, AGENT_VERSION
from src.state_store import SessionMetadata
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


class FakeSessionMetadataResolver:
    def __init__(self, items=None) -> None:
        self.items = items or {}

    def get_metadata(self, session_id):
        return self.items.get(session_id)


class FakeSessionPromptResolver:
    def __init__(self, items=None) -> None:
        self.items = items or {}

    def get_prompt_diagnostics(self, session_id):
        return self.items.get(session_id)


class TraceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        config = HermesOtelPluginConfig(logs_enabled=True)
        runtime = FakeRuntime()
        metrics = MetricManager(runtime)
        logs = LogManager(runtime, config)
        self.lineage = FakeLineage()
        self.session_metadata = FakeSessionMetadataResolver()
        self.session_prompt = FakeSessionPromptResolver()
        self.manager = TraceManager(
            runtime,
            metrics,
            logs,
            config,
            lineage=self.lineage,
            session_metadata=self.session_metadata,
            session_prompt=self.session_prompt,
        )
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
            base_url="https://api.openai.com/v1",
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
            response={
                "id": "resp-1",
                "model": "gpt-test",
                "assistant_message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tool-1",
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": '{"cmd":"echo hello"}',
                            },
                        }
                    ],
                },
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
        self.assertIn("invoke_agent", span_names)
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
        self.assertEqual(llm_span.attributes["gen_ai.operation.name"], "chat")
        self.assertEqual(llm_span.attributes["gen_ai.provider.name"], "openai")
        self.assertEqual(llm_span.attributes["gen_ai.request.model"], "gpt-test")
        self.assertEqual(llm_span.attributes["gen_ai.response.model"], "gpt-test")
        self.assertEqual(llm_span.attributes["gen_ai.conversation.id"], "sess-1")
        self.assertEqual(llm_span.attributes["gen_ai.usage.input_tokens"], 10)
        self.assertEqual(llm_span.attributes["gen_ai.usage.output_tokens"], 3)
        self.assertEqual(llm_span.attributes["gen_ai.usage.total_tokens"], 13)
        self.assertEqual(llm_span.attributes["gen_ai.usage.cache_read.input_tokens"], 2)
        self.assertEqual(llm_span.attributes["gen_ai.usage.cache_creation.input_tokens"], 1)
        self.assertEqual(llm_span.attributes["gen_ai.usage.reasoning.output_tokens"], 0)
        self.assertEqual(llm_span.attributes["gen_ai.response.id"], "resp-1")
        self.assertEqual(llm_span.attributes["gen_ai.response.finish_reasons"], ["stop"])
        output_messages = json.loads(llm_span.attributes["gen_ai.output.messages"])
        self.assertEqual(output_messages[0]["role"], "assistant")
        self.assertEqual(output_messages[0]["finish_reason"], "stop")
        self.assertEqual(output_messages[0]["parts"][0]["type"], "tool_call")
        self.assertEqual(output_messages[0]["parts"][0]["id"], "tool-1")
        self.assertEqual(output_messages[0]["parts"][0]["name"], "terminal")
        self.assertEqual(output_messages[0]["parts"][0]["arguments"], {"cmd": "echo hello"})
        self.assertEqual(llm_span.attributes["input_preview"], "hello")
        self.assertEqual(llm_span.attributes["output_preview"], "toolCall:terminal")
        self.assertEqual(llm_span.attributes["output_kind"], "tool_call")
        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        agent_span = next(span for span in self.runtime.spans if span.name == "invoke_agent")
        self.assertEqual(root_span.attributes["span_kind"], "request")
        self.assertEqual(agent_span.attributes["span_kind"], "agent")
        self.assertEqual(root_span.attributes["agent_runtime"], AGENT_RUNTIME)
        self.assertEqual(agent_span.attributes["agent_runtime"], AGENT_RUNTIME)
        self.assertEqual(root_span.attributes["agent_version"], AGENT_VERSION)
        self.assertEqual(agent_span.attributes["agent_version"], AGENT_VERSION)
        self.assertEqual(agent_span.attributes["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(agent_span.attributes["gen_ai.conversation.id"], "sess-1")
        self.assertEqual(root_span.attributes["gen_ai.conversation.id"], "sess-1")
        self.assertEqual(root_span.attributes["output_preview"], "done")
        self.assertNotIn("usage_input_tokens", root_span.attributes)
        self.assertNotIn("usage_output_tokens", root_span.attributes)
        self.assertNotIn("usage_total_tokens", root_span.attributes)
        self.assertNotIn("usage_cache_read_input_tokens", root_span.attributes)
        self.assertNotIn("usage_cache_write_input_tokens", root_span.attributes)
        self.assertNotIn("usage_cache_total_tokens", root_span.attributes)
        self.assertNotIn("usage_reasoning_tokens", root_span.attributes)
        self.assertNotIn("usage_input_tokens", agent_span.attributes)
        self.assertNotIn("usage_output_tokens", agent_span.attributes)
        self.assertNotIn("usage_total_tokens", agent_span.attributes)
        self.assertNotIn("usage_cache_read_input_tokens", agent_span.attributes)
        self.assertNotIn("usage_cache_write_input_tokens", agent_span.attributes)
        self.assertNotIn("usage_cache_total_tokens", agent_span.attributes)
        self.assertNotIn("usage_reasoning_tokens", agent_span.attributes)
        self.assertNotIn("gen_ai.usage.input_tokens", root_span.attributes)
        self.assertNotIn("gen_ai.usage.output_tokens", root_span.attributes)
        self.assertNotIn("gen_ai.usage.total_tokens", root_span.attributes)
        self.assertNotIn("gen_ai.usage.input_tokens", agent_span.attributes)
        self.assertNotIn("gen_ai.usage.output_tokens", agent_span.attributes)
        self.assertNotIn("gen_ai.usage.total_tokens", agent_span.attributes)
        tool_span = next(span for span in self.runtime.spans if span.name == "tool:terminal")
        self.assertIs(tool_span.parent, llm_span)
        self.assertEqual(llm_span.attributes["span_kind"], "llm")
        self.assertEqual(llm_span.attributes["agent_runtime"], AGENT_RUNTIME)
        self.assertEqual(tool_span.attributes["agent_runtime"], AGENT_RUNTIME)
        self.assertEqual(llm_span.attributes["agent_version"], AGENT_VERSION)
        self.assertEqual(tool_span.attributes["agent_version"], AGENT_VERSION)
        self.assertEqual(tool_span.attributes["span_kind"], "tool")
        self.assertEqual(tool_span.attributes["tool_phase"], "result")
        self.assertEqual(tool_span.attributes["tool_outcome"], "completed")
        self.assertEqual(tool_span.attributes["tool_command"], "echo hello")
        self.assertEqual(tool_span.attributes["tool_result_preview"], '{"output": "hello"}')
        self.assertEqual(tool_span.attributes["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(tool_span.attributes["gen_ai.tool.name"], "terminal")
        self.assertEqual(tool_span.attributes["gen_ai.tool.call.id"], "tool-1")
        self.assertEqual(json.loads(tool_span.attributes["gen_ai.tool.call.arguments"]), {"cmd": "echo hello"})
        self.assertEqual(json.loads(tool_span.attributes["gen_ai.tool.call.result"]), {"output": "hello"})
        self.assertNotIn("tool_result_status", tool_span.attributes)
        self.assertNotIn("usage_input_tokens", tool_span.attributes)
        self.assertNotIn("usage_output_tokens", tool_span.attributes)
        self.assertNotIn("usage_total_tokens", tool_span.attributes)
        self.assertNotIn("usage_cache_read_input_tokens", tool_span.attributes)
        self.assertNotIn("usage_cache_write_input_tokens", tool_span.attributes)
        self.assertNotIn("usage_cache_total_tokens", tool_span.attributes)
        self.assertNotIn("usage_reasoning_tokens", tool_span.attributes)
        tool_operation_call = next(
            call
            for call in self.manager._metrics._client_operation_duration.calls
            if call[2].get("gen_ai.operation.name") == "execute_tool"
        )
        self.assertGreaterEqual(tool_operation_call[1], 0.0)
        self.assertEqual(tool_operation_call[2]["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(tool_operation_call[2]["gen_ai.tool.name"], "terminal")
        self.assertNotIn("tool_name", tool_operation_call[2])
        self.assertFalse(hasattr(self.manager._metrics, "_tool_call_count"))
        workflow_call = self.manager._metrics._workflow_duration.calls[0]
        self.assertEqual(workflow_call[2]["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(workflow_call[2]["gen_ai.conversation.id"], "sess-1")
        self.assertFalse(hasattr(self.manager._metrics, "_request_count"))
        self.assertFalse(hasattr(self.manager._metrics, "_session_token_input"))
        client_token_calls = self.manager._metrics._client_token_usage.calls
        self.assertEqual({call[2]["gen_ai.token.type"] for call in client_token_calls}, {"input", "output"})
        input_client_call = next(call for call in client_token_calls if call[2]["gen_ai.token.type"] == "input")
        self.assertEqual(input_client_call[1], 10.0)
        self.assertEqual(input_client_call[2]["server.address"], "api.openai.com")
        self.assertNotIn("token_type", input_client_call[2])
        self.assertNotIn("provider_name", input_client_call[2])
        client_duration_call = self.manager._metrics._client_operation_duration.calls[0]
        self.assertEqual(client_duration_call[1], 0.25)
        self.assertEqual(client_duration_call[2]["gen_ai.request.model"], "gpt-test")
        self.assertEqual(client_duration_call[2]["gen_ai.operation.name"], "chat")

    def test_session_metadata_fields_are_attached_to_root_and_agent_spans(self) -> None:
        self.session_metadata.items["sess-meta"] = SessionMetadata(
            session_id="sess-meta",
            session_key="agent:main:telegram:group:chat-1:thread-2:user-3",
            session_namespace="agent",
            session_agent="main",
            session_channel="telegram",
            session_scope="group",
            session_channel_target="chat-1:thread-2:user-3",
            session_create_at="2026-05-27T15:00:00+08:00",
            session_updated_at="2026-05-27T15:05:00+08:00",
            session_chat_type="group",
            session_file="/tmp/sess-meta.jsonl",
        )
        self.manager.start_turn(
            session_id="sess-meta",
            user_message="hello",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="telegram",
        )
        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        agent_span = next(span for span in self.runtime.spans if span.name == "invoke_agent")
        for span in (root_span, agent_span):
            self.assertEqual(span.attributes["session_key"], "agent:main:telegram:group:chat-1:thread-2:user-3")
            self.assertEqual(span.attributes["agent_runtime"], AGENT_RUNTIME)
            self.assertEqual(span.attributes["agent_version"], AGENT_VERSION)
            self.assertEqual(span.attributes["session_namespace"], "agent")
            self.assertEqual(span.attributes["session_agent"], "main")
            self.assertEqual(span.attributes["session_channel"], "telegram")
            self.assertEqual(span.attributes["session_scope"], "group")
            self.assertEqual(span.attributes["session_channel_target"], "chat-1:thread-2:user-3")
            self.assertEqual(span.attributes["session_create_at"], "2026-05-27T15:00:00+08:00")
            self.assertEqual(span.attributes["session_updated_at"], "2026-05-27T15:05:00+08:00")
            self.assertEqual(span.attributes["session_chat_type"], "group")
            self.assertEqual(span.attributes["session_file"], "/tmp/sess-meta.jsonl")

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
        agent_span = next(span for span in self.runtime.spans if span.name == "invoke_agent")
        for span in (root_span, agent_span):
            self.assertEqual(span.attributes["request_type"], "auto_review")
            self.assertEqual(span.attributes["review_category"], "skill")
            self.assertEqual(span.attributes["is_auto_review"], True)
        workflow_call = self.manager._metrics._workflow_duration.calls[0]
        self.assertEqual(workflow_call[2]["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(workflow_call[2]["gen_ai.conversation.id"], "sess-review")
        self.assertNotIn("request_type", workflow_call[2])
        self.assertNotIn("review_category", workflow_call[2])

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
        self.assertNotIn("approx_input_tokens_scope", llm_span.attributes)
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
        self.assertNotIn("usage_input_tokens", root_span.attributes)
        self.assertNotIn("usage_output_tokens", root_span.attributes)
        self.assertNotIn("usage_total_tokens", root_span.attributes)
        self.assertNotIn("usage_cache_total_tokens", root_span.attributes)
        self.assertNotIn("gen_ai.usage.input_tokens", root_span.attributes)
        self.assertNotIn("gen_ai.usage.total_tokens", root_span.attributes)

        client_token_calls = self.manager._metrics._client_token_usage.calls
        self.assertEqual({call[2]["gen_ai.token.type"] for call in client_token_calls}, {"input", "output"})
        self.assertEqual([call[1] for call in client_token_calls], [0.0, 0.0])
        self.assertFalse(hasattr(self.manager._metrics, "_token_usage"))

    def test_llm_request_diagnostics_include_payload_and_system_prompt_metadata(self) -> None:
        class PromptDiagnostics:
            system_prompt_chars = 23024
            system_prompt_bytes = 25904
            system_prompt_hash = "abc123def4567890"

        self.session_prompt.items["sess-diag"] = PromptDiagnostics()
        self.manager.start_turn(
            session_id="sess-diag",
            user_message="当前是什么模型",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        request_messages = [
            {"role": "user", "content": [{"type": "input_text", "text": "当前是什么模型"}]},
        ]
        request_payload = {
            "method": "POST",
            "body": {
                "input": request_messages,
                "instructions": "Be concise.",
                "tools": [
                    {
                        "type": "function",
                        "name": "read_file",
                        "description": "Read a file",
                    }
                ],
                "temperature": 0.2,
                "top_p": 0.9,
                "top_k": 40,
                "n": 2,
                "seed": 123,
                "frequency_penalty": 0.1,
                "presence_penalty": 0.3,
                "max_tokens": 1024,
                "stream": False,
                "response_format": {"type": "json_object"},
            },
        }
        self.manager.start_api_request(
            session_id="sess-diag",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            api_mode="codex_responses",
            approx_input_tokens=16675,
            request_char_count=7,
            request_messages=request_messages,
            request=request_payload,
            message_count=1,
            tool_count=28,
        )

        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        self.assertEqual(llm_span.attributes["request_message_count"], 1)
        self.assertEqual(llm_span.attributes["request_tool_count"], 28)
        self.assertEqual(llm_span.attributes["request_payload_item_count"], 1)
        self.assertGreater(llm_span.attributes["request_payload_chars"], 0)
        self.assertGreater(llm_span.attributes["request_payload_bytes"], 0)
        self.assertNotIn("request_user_prompt_estimated_tokens", llm_span.attributes)
        self.assertNotIn("approx_input_tokens_scope", llm_span.attributes)
        self.assertEqual(llm_span.attributes["system_prompt_chars"], 23024)
        self.assertEqual(llm_span.attributes["system_prompt_bytes"], 25904)
        self.assertEqual(llm_span.attributes["system_prompt_hash"], "abc123def4567890")
        input_messages = json.loads(llm_span.attributes["gen_ai.input.messages"])
        self.assertEqual(input_messages[0]["role"], "user")
        self.assertEqual(input_messages[0]["parts"][0]["type"], "text")
        self.assertEqual(input_messages[0]["parts"][0]["content"], "当前是什么模型")
        self.assertEqual(
            json.loads(llm_span.attributes["gen_ai.system_instructions"]),
            [{"type": "text", "content": "Be concise."}],
        )
        tool_definitions = json.loads(llm_span.attributes["gen_ai.tool.definitions"])
        self.assertEqual(tool_definitions[0]["name"], "read_file")
        self.assertEqual(llm_span.attributes["gen_ai.request.temperature"], 0.2)
        self.assertEqual(llm_span.attributes["gen_ai.request.top_p"], 0.9)
        self.assertEqual(llm_span.attributes["gen_ai.request.top_k"], 40)
        self.assertEqual(llm_span.attributes["gen_ai.request.choice.count"], 2)
        self.assertEqual(llm_span.attributes["gen_ai.request.seed"], 123)
        self.assertEqual(llm_span.attributes["gen_ai.request.frequency_penalty"], 0.1)
        self.assertEqual(llm_span.attributes["gen_ai.request.presence_penalty"], 0.3)
        self.assertEqual(llm_span.attributes["gen_ai.request.max_tokens"], 1024)
        self.assertEqual(llm_span.attributes["gen_ai.request.stream"], False)
        self.assertEqual(llm_span.attributes["gen_ai.output.type"], "json_object")
        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        agent_span = next(span for span in self.runtime.spans if span.name == "invoke_agent")
        self.assertGreater(root_span.attributes["request_user_prompt_estimated_tokens"], 0)
        self.assertEqual(
            agent_span.attributes["request_user_prompt_estimated_tokens"],
            root_span.attributes["request_user_prompt_estimated_tokens"],
        )

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
        self.assertEqual(delegate_span.attributes["span_kind"], "tool")
        self.assertEqual(subagent_span.attributes["span_kind"], "subagent")
        self.assertEqual(subagent_span.attributes["subagent_role"], "coder")
        self.assertEqual(subagent_span.attributes["subagent_runtime_role"], "leaf")
        self.assertEqual(subagent_span.attributes["agent_runtime"], AGENT_RUNTIME)
        self.assertEqual(subagent_span.attributes["agent_version"], AGENT_VERSION)

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
        self.assertNotIn("usage_input_tokens", root_span.attributes)
        self.assertNotIn("usage_output_tokens", root_span.attributes)
        self.assertNotIn("usage_total_tokens", root_span.attributes)
        self.assertNotIn("usage_cache_total_tokens", root_span.attributes)
        self.assertNotIn("gen_ai.usage.input_tokens", root_span.attributes)
        self.assertNotIn("gen_ai.usage.total_tokens", root_span.attributes)

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
        self.assertNotIn("usage_cache_read_input_tokens", root_span.attributes)
        self.assertNotIn("usage_cache_write_input_tokens", root_span.attributes)

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
        skill_spans = [span for span in self.runtime.spans if span.name == "skill:dashboard"]
        self.assertEqual(len(tool_spans), 1)
        self.assertEqual(tool_spans[0].status_code, "OK")
        self.assertEqual(tool_spans[0].attributes["span_kind"], "tool")
        self.assertEqual(len(skill_spans), 1)
        self.assertEqual(skill_spans[0].attributes["span_kind"], "skill")

    def test_skill_view_emits_skill_span(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            skill_dir = Path(tempdir) / "skill_manage"
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_path = str(skill_dir / "SKILL.md")
            with open(skill_dir / "package.json", "w", encoding="utf-8") as handle:
                handle.write('{"version": "9.8.7"}')

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
                args={"name": "wrong-name"},
                session_id="sess-6",
                platform="cli",
                tool_call_id="call-skill-2",
            )
            self.manager.finish_tool_call(
                tool_name="skill_view",
                args={"name": "wrong-name"},
                result=json.dumps(
                    {
                        "success": True,
                        "name": "ignored-name",
                        "path": skill_path,
                        "content": "---\ndescription: Workspace skill doc\n---\n# skill_manage\n\nBody paragraph\n",
                        "tags": ["query"],
                        "related_skills": ["dashboard"],
                    }
                ),
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

        tool_span = next(span for span in self.runtime.spans if span.name == "tool:skill_view")
        skill_span = next(span for span in self.runtime.spans if span.name == "skill:skill_manage")
        self.assertIs(skill_span.parent, tool_span)
        self.assertEqual(skill_span.status_code, "OK")
        self.assertEqual(skill_span.description, "completed")
        for span in (tool_span, skill_span):
            self.assertEqual(span.attributes["skill_name"], "skill_manage")
            self.assertEqual(span.attributes["skill_description"], "Workspace skill doc")
            self.assertEqual(span.attributes["skill.name"], "skill_manage")
            self.assertEqual(span.attributes["skill.description"], "Workspace skill doc")
            self.assertEqual(span.attributes["skill.path"], skill_path)
            self.assertEqual(span.attributes["skill.source.type"], "workspace")
            self.assertEqual(span.attributes["skill.result_status"], "completed")
            self.assertEqual(span.attributes["gen_ai.skill.name"], "skill_manage")
            self.assertEqual(span.attributes["gen_ai.skill.description"], "Workspace skill doc")
            self.assertEqual(span.attributes["gen_ai.skill.path"], skill_path)
            self.assertEqual(span.attributes["gen_ai.skill.source.type"], "workspace")
            self.assertEqual(span.attributes["gen_ai.skill.result_status"], "completed")
            self.assertEqual(span.attributes["gen_ai.skill.version"], "9.8.7")
            self.assertEqual(span.attributes["skill_call_id"], "call-skill-2")
        self.assertEqual(skill_span.attributes["skill_tags"], "query")
        self.assertEqual(skill_span.attributes["skill_related_skills"], "dashboard")
        self.assertFalse(hasattr(self.manager._metrics, "_skill_activation_count"))
        skill_operation_calls = [
            call
            for call in self.manager._metrics._client_operation_duration.calls
            if call[2].get("gen_ai.operation.name") == "skill"
        ]
        self.assertEqual(len(skill_operation_calls), 1)
        self.assertGreaterEqual(skill_operation_calls[0][1], 0.0)
        self.assertEqual(skill_operation_calls[0][2]["gen.ai.skill.name"], "skill_manage")
        self.assertEqual(skill_operation_calls[0][2]["gen_ai.conversation.id"], "sess-6")
        self.assertNotIn("skill_name", skill_operation_calls[0][2])

    def test_failed_skill_view_marks_tool_result_status_without_emitting_skill_span(self) -> None:
        self.manager.start_turn(
            session_id="sess-6err",
            user_message="skill error",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_tool_call(
            tool_name="skill_view",
            args={"name": "broken_skill"},
            session_id="sess-6err",
            platform="cli",
            tool_call_id="call-skill-err",
        )
        self.manager.finish_tool_call(
            tool_name="skill_view",
            args={"name": "broken_skill"},
            result='{"success": false, "error": "not found"}',
            session_id="sess-6err",
            platform="cli",
            tool_call_id="call-skill-err",
        )

        tool_span = next(span for span in self.runtime.spans if span.name == "tool:skill_view")
        self.assertEqual(tool_span.attributes["skill_name"], "broken_skill")
        self.assertEqual(tool_span.attributes["skill.name"], "broken_skill")
        self.assertEqual(tool_span.attributes["skill.result_status"], "error")
        self.assertEqual(tool_span.attributes["gen_ai.skill.result_status"], "error")
        self.assertFalse(any(span.name == "skill:broken_skill" for span in self.runtime.spans))

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

    def test_tool_and_skill_attach_under_pending_llm_chain(self) -> None:
        self.manager.start_turn(
            session_id="sess-skill-chain",
            user_message="review and update skill",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="sess-skill-chain",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
        )
        self.manager.finish_api_request(
            session_id="sess-skill-chain",
            platform="cli",
            model="gpt-test",
            provider="openai",
            api_call_count=1,
            api_duration=0.25,
            finish_reason="stop",
            response_model="gpt-test",
            usage={"input_tokens": 10, "output_tokens": 3},
        )
        self.manager.start_tool_call(
            tool_name="skill_view",
            args={"name": "skill_manage"},
            session_id="sess-skill-chain",
            platform="cli",
            tool_call_id="call-skill-chain",
        )
        self.manager.finish_tool_call(
            tool_name="skill_view",
            args={"name": "skill_manage"},
            result='{"success": true, "name": "skill_manage", "description": "Skill manage", "content": "body"}',
            session_id="sess-skill-chain",
            platform="cli",
            tool_call_id="call-skill-chain",
        )

        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        tool_span = next(span for span in self.runtime.spans if span.name == "tool:skill_view")
        skill_span = next(span for span in self.runtime.spans if span.name == "skill:skill_manage")

        self.assertIs(tool_span.parent, llm_span)
        self.assertIs(skill_span.parent, tool_span)
        self.assertEqual(skill_span.attributes["skill_call_id"], "call-skill-chain")

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
        tool_metric_attrs = self.manager._metrics._client_operation_duration.calls[0][2]
        self.assertEqual(tool_metric_attrs["tool_result_status"], "completed")
        self.assertEqual(tool_metric_attrs["gen_ai.operation.name"], "execute_tool")
        self.assertNotIn("outcome", tool_metric_attrs)

    def test_api_request_error_marks_llm_and_parent_spans(self) -> None:
        self.manager.start_turn(
            session_id="sess-auth-error",
            user_message="today weather",
            conversation_history=[],
            is_first_turn=True,
            model="gpt-test",
            platform="cli",
        )
        self.manager.start_api_request(
            session_id="sess-auth-error",
            platform="cli",
            model="gpt-test",
            provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            api_call_count=1,
            api_mode="responses",
        )
        self.manager.record_api_request_error(
            session_id="sess-auth-error",
            platform="cli",
            model="gpt-test",
            provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            api_call_count=1,
            api_duration=3.32,
            status_code=401,
            retry_count=1,
            max_retries=3,
            retryable=False,
            reason="token_invalidated",
            error={
                "type": "AuthenticationError",
                "message": "HTTP 401: Your authentication token has been invalidated.",
                "code": "token_invalidated",
            },
        )
        self.manager.finalize_session("sess-auth-error", platform="cli", outcome="finalized")

        llm_span = next(span for span in self.runtime.spans if span.name == "llm")
        root_span = next(span for span in self.runtime.spans if span.name == "hermes_request")
        agent_span = next(span for span in self.runtime.spans if span.name == "invoke_agent")

        self.assertEqual(llm_span.status_code, "ERROR")
        self.assertEqual(llm_span.attributes["error_type"], "AuthenticationError")
        self.assertEqual(llm_span.attributes["error_code"], "token_invalidated")
        self.assertEqual(llm_span.attributes["error.type"], "token_invalidated")
        self.assertEqual(llm_span.attributes["http_status_code"], 401)
        self.assertEqual(llm_span.attributes["retryable"], False)
        self.assertEqual(llm_span.attributes["base_url"], "https://chatgpt.com/backend-api/codex")
        self.assertEqual(llm_span.attributes["gen_ai.provider.name"], "openai-codex")
        self.assertEqual(llm_span.attributes["gen_ai.operation.name"], "chat")
        self.assertEqual(root_span.attributes["error_type"], "AuthenticationError")
        self.assertEqual(root_span.attributes["error.type"], "token_invalidated")
        self.assertEqual(agent_span.attributes["error_reason"], "token_invalidated")
        self.assertEqual(root_span.attributes["final_status"], "failed")
        self.assertEqual(agent_span.status_code, "ERROR")
        self.assertEqual(root_span.status_code, "ERROR")

        request_error_call = self.manager._metrics._client_operation_duration.calls[0]
        self.assertEqual(request_error_call[2]["gen_ai.operation.name"], "chat")
        self.assertNotIn("outcome", request_error_call[2])
        self.assertEqual(request_error_call[2]["error.type"], "token_invalidated")
        self.assertEqual(request_error_call[2]["gen_ai.provider.name"], "openai-codex")
        self.assertEqual(request_error_call[2]["server.address"], "chatgpt.com")
        self.assertEqual(self.manager._metrics._client_token_usage.calls, [])
        workflow_call = self.manager._metrics._workflow_duration.calls[0]
        self.assertEqual(workflow_call[2]["gen_ai.operation.name"], "invoke_agent")
        self.assertNotIn("session_state", workflow_call[2])
        self.assertEqual(self.runtime.logs[-1][1]["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
