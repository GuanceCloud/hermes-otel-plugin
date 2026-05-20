from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ActiveSpanState:
    key: str
    span: Any
    started_at_ns: int
    started_monotonic_ns: int
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PendingLlmState:
    span: Any
    end_time_ns: int
    output_kind: str | None = None
    tool_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TurnState:
    session_id: str
    platform: str
    model: str
    user_message: str
    conversation_length: int
    is_first_turn: bool
    root_span: Any
    agent_span: Any
    started_at_ns: int
    started_monotonic_ns: int
    last_activity_monotonic_ns: int
    request_type: str = "user_request"
    is_auto_review: bool = False
    review_category: str | None = None
    provider_name: str | None = None
    request_model: str | None = None
    response_model: str | None = None
    aggregate_input_tokens: int = 0
    aggregate_output_tokens: int = 0
    aggregate_cache_read_tokens: int = 0
    aggregate_cache_write_tokens: int = 0
    aggregate_reasoning_tokens: int = 0
    last_raw_cache_read_tokens: int | None = None
    last_raw_cache_write_tokens: int | None = None
    pending_llm: PendingLlmState | None = None
    tool_context_since_last_llm: list[str] = field(default_factory=list)
    active_requests: dict[str, ActiveSpanState] = field(default_factory=dict)
    active_tools: dict[str, ActiveSpanState] = field(default_factory=dict)
    active_skills: dict[str, ActiveSpanState] = field(default_factory=dict)
    last_delegate_tool_span: Any | None = None
    last_delegate_profile: str | None = None
    next_tool_index: int = 1


class TurnStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._turns: dict[str, TurnState] = {}

    def replace_turn(self, session_id: str, state: TurnState) -> TurnState | None:
        with self._lock:
            previous = self._turns.get(session_id)
            self._turns[session_id] = state
            return previous

    def get_turn(self, session_id: str) -> TurnState | None:
        with self._lock:
            return self._turns.get(session_id)

    def pop_turn(self, session_id: str) -> TurnState | None:
        with self._lock:
            return self._turns.pop(session_id, None)

    def allocate_tool_key(self, session_id: str, explicit_key: str | None = None) -> str | None:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                return explicit_key
            if explicit_key:
                return explicit_key
            key = f"tool-{turn.next_tool_index}"
            turn.next_tool_index += 1
            return key

    def touch_turn(self, session_id: str, monotonic_ns: int) -> None:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is not None:
                turn.last_activity_monotonic_ns = monotonic_ns

    def expire(self, ttl_ms: int) -> list[TurnState]:
        now_monotonic_ns = time.monotonic_ns()
        ttl_ns = ttl_ms * 1_000_000
        expired: list[TurnState] = []
        with self._lock:
            expired_keys = [
                key
                for key, turn in self._turns.items()
                if now_monotonic_ns - turn.last_activity_monotonic_ns >= ttl_ns
            ]
            for key in expired_keys:
                expired_turn = self._turns.pop(key, None)
                if expired_turn is not None:
                    expired.append(expired_turn)
        return expired


class SessionLineageResolver:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = Path(db_path or os.path.expanduser("~/.hermes/state.db"))
        self._lock = threading.RLock()
        self._cache: dict[str, str] = {}

    def get_parent_session_id(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        with self._lock:
            if session_id in self._cache:
                cached = self._cache[session_id]
                return cached or None

        row = self._query_parent_session_id(session_id)
        if row is None:
            return None

        parent_session_id = str(row or "")
        with self._lock:
            self._cache[session_id] = parent_session_id
        return parent_session_id or None

    def is_child_session(self, session_id: str | None) -> bool:
        return bool(self.get_parent_session_id(session_id))

    def _query_parent_session_id(self, session_id: str) -> str | None | object:
        if not self._db_path.exists():
            return None
        connection = None
        try:
            connection = sqlite3.connect(str(self._db_path))
            row = connection.execute(
                "SELECT parent_session_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return row[0]
        except Exception:
            return None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
