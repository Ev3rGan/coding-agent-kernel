"""Durable append-only Session trees and their persistence stores."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from coding_agent.events import (
    AgentError,
    AgentSessionEvent,
    AssistantMessage,
    assistant_message_record,
)

if TYPE_CHECKING:
    from coding_agent.context import CompactionPlan

SESSION_SCHEMA = "coding-agent-session"
SESSION_SCHEMA_VERSION = 1

SessionEntryKind = Literal["configuration", "message", "compaction"]
SessionRecordKind = Literal["entry", "active_leaf", "closed", "resumed"]


class SessionError(ValueError):
    """Base class for explicit Session persistence failures."""

    code = "session_error"


class SessionCorruptionError(SessionError):
    """Raised when persisted bytes do not match the versioned schema."""

    code = "session_corrupt"


class SessionRelationError(SessionError):
    """Raised when a persisted or requested tree relation is illegal."""

    code = "session_illegal_relation"


class SessionStateError(SessionError):
    """Raised when a lifecycle operation is invalid for the Session state."""

    code = "session_invalid_state"


@dataclass(frozen=True, slots=True)
class SessionEntry:
    """One immutable node in a Session tree."""

    entry_id: str
    session_id: str
    parent_id: str | None
    kind: SessionEntryKind
    payload_json: str

    @property
    def payload(self) -> dict[str, object]:
        """Return a fresh decoded payload so the entry itself stays immutable."""

        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):  # pragma: no cover - constructors enforce this
            raise SessionCorruptionError("SessionEntry payload must be an object.")
        return decoded


@dataclass(frozen=True, slots=True)
class PersistenceRecord:
    """One explicitly versioned append-only persistence record."""

    session_id: str
    sequence: int
    kind: SessionRecordKind
    entry: SessionEntry | None = None
    active_leaf_id: str | None = None


class SessionStore(Protocol):
    """Persistence seam shared by memory and JSONL implementations."""

    def append(self, record: PersistenceRecord) -> None: ...

    def load(self, session_id: str) -> tuple[PersistenceRecord, ...]: ...


class InMemorySessionStore:
    """Process-local SessionStore with the same record contract as JSONL."""

    def __init__(self) -> None:
        self._records: dict[str, list[PersistenceRecord]] = {}

    def append(self, record: PersistenceRecord) -> None:
        self._records.setdefault(record.session_id, []).append(record)

    def load(self, session_id: str) -> tuple[PersistenceRecord, ...]:
        return tuple(self._records.get(session_id, ()))


class JsonlSessionStore:
    """Append Session records to an inspectable, independently versioned JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: PersistenceRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_encode_record(record))
            stream.write("\n")

    def load(self, session_id: str) -> tuple[PersistenceRecord, ...]:
        if not self.path.exists():
            return ()
        records: list[PersistenceRecord] = []
        try:
            with self.path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        raise SessionCorruptionError(f"Blank JSONL record at line {line_number}.")
                    record = _decode_record(line, line_number=line_number)
                    if record.session_id == session_id:
                        records.append(record)
        except UnicodeDecodeError as exc:
            raise SessionCorruptionError("Session JSONL is not valid UTF-8.") from exc
        return tuple(records)


def _canonical_payload(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise SessionCorruptionError("SessionEntry payload must be JSON serializable.") from exc


def _validate_compaction_values(
    *,
    version: object,
    summary: object,
    covered_entry_ids: object,
    expected_entry_ids: tuple[str, ...],
    checkpoint_label: str,
) -> None:
    if (
        version != 1
        or not isinstance(summary, str)
        or not summary
        or not isinstance(covered_entry_ids, (list, tuple))
        or not all(isinstance(item, str) for item in covered_entry_ids)
        or tuple(covered_entry_ids) != expected_entry_ids
    ):
        raise SessionRelationError(
            f"Compaction checkpoint {checkpoint_label} has illegal coverage."
        )


def _encode_record(record: PersistenceRecord) -> str:
    value: dict[str, object] = {
        "schema": SESSION_SCHEMA,
        "version": SESSION_SCHEMA_VERSION,
        "session_id": record.session_id,
        "sequence": record.sequence,
        "record_type": record.kind,
    }
    if record.entry is not None:
        value["entry"] = {
            "entry_id": record.entry.entry_id,
            "parent_id": record.entry.parent_id,
            "kind": record.entry.kind,
            "payload": record.entry.payload,
        }
    if record.active_leaf_id is not None:
        value["active_leaf_id"] = record.active_leaf_id
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required(value: Mapping[str, object], key: str, expected: type[object]) -> object:
    item = value.get(key)
    if not isinstance(item, expected):
        raise SessionCorruptionError(f"Session record field {key!r} has an invalid type.")
    return item


def _decode_record(line: str, *, line_number: int) -> PersistenceRecord:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SessionCorruptionError(f"Invalid JSON at line {line_number}.") from exc
    if not isinstance(value, dict):
        raise SessionCorruptionError(f"Session record at line {line_number} must be an object.")
    if value.get("schema") != SESSION_SCHEMA or value.get("version") != SESSION_SCHEMA_VERSION:
        raise SessionCorruptionError(f"Unsupported Session schema at line {line_number}.")
    session_id = _required(value, "session_id", str)
    raw_sequence = value.get("sequence")
    if type(raw_sequence) is not int:
        raise SessionCorruptionError("Session record field 'sequence' has an invalid type.")
    sequence = raw_sequence
    record_type = value.get("record_type")
    if record_type not in {"entry", "active_leaf", "closed", "resumed"}:
        raise SessionCorruptionError(f"Unknown Session record type at line {line_number}.")

    entry = None
    if record_type == "entry":
        raw_entry = value.get("entry")
        if not isinstance(raw_entry, dict):
            raise SessionCorruptionError(f"Missing SessionEntry at line {line_number}.")
        entry_id = _required(raw_entry, "entry_id", str)
        parent_id = raw_entry.get("parent_id")
        if parent_id is not None and not isinstance(parent_id, str):
            raise SessionCorruptionError(f"Invalid parent at line {line_number}.")
        entry_kind = raw_entry.get("kind")
        if entry_kind not in {"configuration", "message", "compaction"}:
            raise SessionCorruptionError(f"Invalid SessionEntry kind at line {line_number}.")
        payload = raw_entry.get("payload")
        if not isinstance(payload, dict):
            raise SessionCorruptionError(f"Invalid SessionEntry payload at line {line_number}.")
        entry = SessionEntry(
            entry_id=str(entry_id),
            session_id=str(session_id),
            parent_id=parent_id,
            kind=entry_kind,
            payload_json=_canonical_payload(payload),
        )

    active_leaf_id = value.get("active_leaf_id")
    if active_leaf_id is not None and not isinstance(active_leaf_id, str):
        raise SessionCorruptionError(f"Invalid active leaf at line {line_number}.")
    return PersistenceRecord(
        session_id=str(session_id),
        sequence=sequence,
        kind=record_type,
        entry=entry,
        active_leaf_id=active_leaf_id,
    )


class Session:
    """Mutable coordinator over an immutable append-only Session tree."""

    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        *,
        entry_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self.session_id = session_id
        self._entry_id_factory = entry_id_factory or (lambda: f"entry-{uuid4().hex}")
        self._entries: dict[str, SessionEntry] = {}
        self._active_leaf_id: str | None = None
        self._next_sequence = 1
        self._closed = False
        self._events: list[AgentSessionEvent] = []

    @classmethod
    def create(
        cls,
        store: SessionStore,
        *,
        configuration: Mapping[str, object],
        session_id: str | None = None,
        entry_id_factory: Callable[[], str] | None = None,
    ) -> Session:
        actual_session_id = session_id or f"session-{uuid4().hex}"
        if store.load(actual_session_id):
            raise SessionStateError(f"Session {actual_session_id!r} already exists.")
        session = cls(store, actual_session_id, entry_id_factory=entry_id_factory)
        session._append_entry("configuration", configuration)
        return session

    @classmethod
    def resume(
        cls,
        store: SessionStore,
        session_id: str,
        *,
        entry_id_factory: Callable[[], str] | None = None,
    ) -> Session:
        """Reload a closed Session, validate all history, and resume its active leaf."""

        records = store.load(session_id)
        if not records:
            raise SessionStateError(f"Session {session_id!r} does not exist.")
        session = cls(store, session_id, entry_id_factory=entry_id_factory)
        session._replay(records)
        if not session._closed:
            raise SessionStateError("Session recovery is incomplete: no close record.")
        session._append_record("resumed")
        session._closed = False
        session._events.extend(
            (
                AgentSessionEvent.from_session_resumed(session_id),
                AgentSessionEvent.from_session_configuration(
                    session_id, session._configuration_json
                ),
                session._active_branch_event(),
            )
        )
        return session

    @property
    def active_leaf_id(self) -> str:
        if self._active_leaf_id is None:
            raise SessionStateError("Session has no active leaf.")
        return self._active_leaf_id

    @property
    def active_branch(self) -> tuple[SessionEntry, ...]:
        return self._branch_to(self.active_leaf_id)

    @property
    def branches(self) -> tuple[tuple[SessionEntry, ...], ...]:
        """Return every root-to-leaf branch without selecting a sibling implicitly."""

        parents = {entry.parent_id for entry in self._entries.values() if entry.parent_id}
        leaves = sorted(entry_id for entry_id in self._entries if entry_id not in parents)
        return tuple(self._branch_to(entry_id) for entry_id in leaves)

    @property
    def _configuration_json(self) -> str:
        root = next(entry for entry in self._entries.values() if entry.parent_id is None)
        return root.payload_json

    def record_authoritative_message(
        self, message: AssistantMessage, *, run_id: str | None = None
    ) -> SessionEntry:
        """Persist one complete assistant message after its authoritative message_end."""

        entry = self._append_entry("message", assistant_message_record(message), run_id=run_id)
        return entry

    def record_user_message(self, text: str, *, run_id: str | None = None) -> SessionEntry:
        """Persist Host input once it has been accepted for an Agent Run."""

        return self._append_entry(
            "message",
            {"role": "user", "text": text},
            run_id=run_id,
        )

    def record_compaction(self, plan: CompactionPlan, *, run_id: str | None = None) -> SessionEntry:
        """Persist one validated Active Branch checkpoint without rewriting history."""

        expected = tuple(
            entry.entry_id for entry in self.active_branch if entry.kind != "configuration"
        )
        _validate_compaction_values(
            version=plan.version,
            summary=plan.summary,
            covered_entry_ids=plan.covered_entry_ids,
            expected_entry_ids=expected,
            checkpoint_label="proposal",
        )
        entry = self._append_entry(
            "compaction",
            {
                "version": plan.version,
                "covered_entry_ids": list(plan.covered_entry_ids),
                "summary": plan.summary,
            },
            run_id=run_id,
        )
        self._events.append(
            AgentSessionEvent.from_compaction_succeeded(
                entry,
                tuple(item.entry_id for item in self.active_branch),
                run_id=run_id,
            )
        )
        return entry

    def record_context_failure(
        self,
        error: AgentError,
        *,
        stage: str,
        run_id: str | None = None,
    ) -> None:
        """Queue an observable failure without appending an invalid checkpoint."""

        self._events.append(
            AgentSessionEvent.from_context_failure(
                self.session_id,
                tuple(entry.entry_id for entry in self.active_branch),
                error,
                stage=stage,
                run_id=run_id,
            )
        )

    def drain_events(self) -> tuple[AgentSessionEvent, ...]:
        """Return pending Host observations exactly once."""

        events = tuple(self._events)
        self._events.clear()
        return events

    def fork(self, entry_id: str) -> None:
        """Select an existing entry as the parent of the next appended route."""

        if self._closed:
            raise SessionStateError("Cannot navigate a closed Session.")
        if entry_id not in self._entries:
            raise SessionRelationError(f"Unknown fork parent {entry_id!r}.")
        self._append_record("active_leaf", active_leaf_id=entry_id)
        self._active_leaf_id = entry_id
        self._events.append(self._active_branch_event())

    def close(self) -> None:
        """Mark the current process-boundary lifecycle as closed."""

        if self._closed:
            raise SessionStateError("Session is already closed.")
        self._append_record("closed")
        self._closed = True

    def _append_entry(
        self,
        kind: SessionEntryKind,
        payload: Mapping[str, object],
        *,
        run_id: str | None = None,
    ) -> SessionEntry:
        if self._closed:
            raise SessionStateError("Cannot append to a closed Session.")
        entry_id = self._entry_id_factory()
        if not entry_id or entry_id in self._entries:
            raise SessionRelationError("SessionEntry IDs must be non-empty and unique.")
        entry = SessionEntry(
            entry_id=entry_id,
            session_id=self.session_id,
            parent_id=self._active_leaf_id,
            kind=kind,
            payload_json=_canonical_payload(payload),
        )
        self._store.append(
            PersistenceRecord(
                session_id=self.session_id,
                sequence=self._next_sequence,
                kind="entry",
                entry=entry,
            )
        )
        self._entries[entry_id] = entry
        self._active_leaf_id = entry_id
        self._next_sequence += 1
        if kind == "configuration":
            self._events.append(
                AgentSessionEvent.from_session_configuration(self.session_id, entry.payload_json)
            )
        self._events.append(AgentSessionEvent.from_session_entry(entry, run_id=run_id))
        self._events.append(self._active_branch_event(run_id=run_id))
        return entry

    def _active_branch_event(self, *, run_id: str | None = None) -> AgentSessionEvent:
        return AgentSessionEvent.from_active_branch(
            self.session_id,
            tuple(entry.entry_id for entry in self.active_branch),
            run_id=run_id,
        )

    def _append_record(self, kind: SessionRecordKind, *, active_leaf_id: str | None = None) -> None:
        self._store.append(
            PersistenceRecord(
                session_id=self.session_id,
                sequence=self._next_sequence,
                kind=kind,
                active_leaf_id=active_leaf_id,
            )
        )
        self._next_sequence += 1

    def _branch_to(self, leaf_id: str) -> tuple[SessionEntry, ...]:
        current = leaf_id
        reverse: list[SessionEntry] = []
        while True:
            entry = self._entries[current]
            reverse.append(entry)
            if entry.parent_id is None:
                return tuple(reversed(reverse))
            current = entry.parent_id

    def _replay(self, records: tuple[PersistenceRecord, ...]) -> None:
        for expected_sequence, record in enumerate(records, 1):
            if record.sequence != expected_sequence:
                raise SessionCorruptionError(
                    f"Session sequence must be contiguous at {expected_sequence}."
                )
            if record.session_id != self.session_id:
                raise SessionCorruptionError("Session record has the wrong session ID.")
            if record.kind == "entry":
                entry = record.entry
                if entry is None:
                    raise SessionCorruptionError("Entry record is missing its SessionEntry.")
                if self._closed:
                    raise SessionStateError("Closed Session contains an unresumed entry.")
                if entry.session_id != self.session_id:
                    raise SessionCorruptionError("SessionEntry has the wrong session ID.")
                if entry.entry_id in self._entries:
                    raise SessionRelationError("Duplicate SessionEntry ID.")
                if not self._entries:
                    if entry.parent_id is not None or entry.kind != "configuration":
                        raise SessionRelationError(
                            "The first SessionEntry must be a root configuration."
                        )
                elif entry.parent_id not in self._entries:
                    raise SessionRelationError(
                        f"SessionEntry {entry.entry_id!r} has an unknown parent."
                    )
                if entry.kind == "compaction":
                    self._validate_compaction_entry(entry)
                self._entries[entry.entry_id] = entry
                self._active_leaf_id = entry.entry_id
            elif record.kind == "active_leaf":
                if self._closed:
                    raise SessionStateError("Closed Session contains branch navigation.")
                if record.active_leaf_id not in self._entries:
                    raise SessionRelationError("Active leaf refers to an unknown SessionEntry.")
                self._active_leaf_id = record.active_leaf_id
            elif record.kind == "closed":
                if self._closed:
                    raise SessionStateError("Session contains consecutive close records.")
                self._closed = True
            elif record.kind == "resumed":
                if not self._closed:
                    raise SessionStateError("Session resume does not follow a close record.")
                self._closed = False
        if self._active_leaf_id is None:
            raise SessionCorruptionError("Session has no recoverable active leaf.")
        self._next_sequence = len(records) + 1

    def _validate_compaction_entry(self, entry: SessionEntry) -> None:
        if entry.parent_id is None:
            raise SessionRelationError("Compaction checkpoint cannot be the Session root.")
        payload = entry.payload
        raw_coverage = payload.get("covered_entry_ids")
        summary = payload.get("summary")
        expected = tuple(
            item.entry_id
            for item in self._branch_to(entry.parent_id)
            if item.kind != "configuration"
        )
        _validate_compaction_values(
            version=payload.get("version"),
            summary=summary,
            covered_entry_ids=raw_coverage,
            expected_entry_ids=expected,
            checkpoint_label=repr(entry.entry_id),
        )
