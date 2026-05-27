from __future__ import annotations

import os
import sqlite3
import threading
import time
import json
from dataclasses import dataclass, field
from datetime import datetime
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
    session_key: str | None = None
    session_namespace: str | None = None
    session_agent: str | None = None
    session_channel: str | None = None
    session_scope: str | None = None
    session_channel_target: str | None = None
    session_create_at: str | None = None
    session_updated_at: str | None = None
    session_chat_type: str | None = None
    session_file: str | None = None
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


@dataclass(slots=True)
class SessionMetadata:
    session_id: str
    session_key: str | None = None
    session_namespace: str | None = None
    session_agent: str | None = None
    session_channel: str | None = None
    session_scope: str | None = None
    session_channel_target: str | None = None
    session_create_at: str | None = None
    session_updated_at: str | None = None
    session_chat_type: str | None = None
    session_file: str | None = None


class SessionMetadataResolver:
    def __init__(self, sessions_dir: str | None = None) -> None:
        self._sessions_dir = Path(sessions_dir or os.path.expanduser("~/.hermes/sessions"))
        self._sessions_file = self._sessions_dir / "sessions.json"
        self._lock = threading.RLock()
        self._cache: dict[str, SessionMetadata | None] = {}
        self._index_mtime_ns: int | None = None
        self._entries_by_session_id: dict[str, dict[str, Any]] = {}

    def get_metadata(self, session_id: str | None) -> SessionMetadata | None:
        if not session_id:
            return None
        self._refresh_index_if_needed()
        with self._lock:
            if session_id in self._cache:
                return self._cache[session_id]
            entry = self._entries_by_session_id.get(session_id)
        if not entry:
            with self._lock:
                self._cache[session_id] = None
            return None
        metadata = self._build_metadata(session_id, entry)
        with self._lock:
            self._cache[session_id] = metadata
        return metadata

    def _refresh_index_if_needed(self) -> None:
        try:
            stat = self._sessions_file.stat()
        except Exception:
            return
        with self._lock:
            if self._index_mtime_ns == stat.st_mtime_ns:
                return
        payload = self._load_sessions_payload()
        if payload is None:
            return
        entries_by_session_id: dict[str, dict[str, Any]] = {}
        if isinstance(payload, dict):
            iterable = payload.values()
        else:
            iterable = ()
        for item in iterable:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or "").strip()
            if not session_id:
                continue
            entries_by_session_id[session_id] = item
        with self._lock:
            self._entries_by_session_id = entries_by_session_id
            self._cache.clear()
            self._index_mtime_ns = stat.st_mtime_ns

    def _load_sessions_payload(self) -> Any:
        if not self._sessions_file.exists():
            return None
        try:
            with self._sessions_file.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return None

    def _build_metadata(self, session_id: str, entry: dict[str, Any]) -> SessionMetadata:
        session_key = self._string_value(entry.get("session_key"))
        derived = self._parse_session_key(session_key)
        return SessionMetadata(
            session_id=session_id,
            session_key=session_key,
            session_namespace=derived.get("session_namespace"),
            session_agent=derived.get("session_agent"),
            session_channel=derived.get("session_channel"),
            session_scope=derived.get("session_scope"),
            session_channel_target=derived.get("session_channel_target"),
            session_create_at=self._normalize_iso_datetime(entry.get("created_at")),
            session_updated_at=self._normalize_iso_datetime(entry.get("updated_at")),
            session_chat_type=self._string_value(entry.get("chat_type")),
            session_file=str(self._sessions_dir / f"{session_id}.jsonl"),
        )

    def _parse_session_key(self, session_key: str | None) -> dict[str, str | None]:
        if not session_key:
            return {}
        parts = [part.strip() for part in session_key.split(":")]
        if len(parts) < 4:
            return {}
        namespace = parts[0]
        agent = parts[1] if len(parts) > 1 else None
        channel = parts[2] if len(parts) > 2 else None
        scope = parts[3] if len(parts) > 3 else None
        target_parts = [part for part in parts[4:] if part]
        return {
            "session_namespace": namespace or None,
            "session_agent": agent or None,
            "session_channel": channel or None,
            "session_scope": scope or None,
            "session_channel_target": ":".join(target_parts) or None,
        }

    def _string_value(self, value: Any) -> str | None:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return None

    def _normalize_iso_datetime(self, value: Any) -> str | None:
        raw = self._string_value(value)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw).isoformat()
        except Exception:
            return raw
