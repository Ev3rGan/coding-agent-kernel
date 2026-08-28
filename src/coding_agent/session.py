"""Durable append-only Session trees and their persistence stores."""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias, cast
from uuid import uuid4

from coding_agent.callout import dispose_awaitable, invoke_sync_callout
from coding_agent.events import (
    AgentError,
    AgentSessionEvent,
    AssistantMessage,
    assistant_message_record,
)
from coding_agent.json_contract import json_object_snapshot

if TYPE_CHECKING:
    from coding_agent.context import CompactionPlan

SESSION_SCHEMA = "coding-agent-session"
SESSION_SCHEMA_VERSION = 1

SessionEntryKind: TypeAlias = str
SessionRecordKind = Literal["entry", "active_leaf", "closed", "resumed"]
SessionEntryValidator: TypeAlias = Callable[[Mapping[str, object]], None]
_BUILTIN_ENTRY_KINDS = frozenset({"configuration", "message", "compaction"})


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
    """Persistence seam shared by memory and JSONL implementations.

    ``append_many`` is a transactional capability: returning means every record is
    durably visible to ``load``; raising means none of the records are visible.
    """

    def append(self, record: PersistenceRecord) -> None: ...

    def append_many(self, records: tuple[PersistenceRecord, ...]) -> None: ...

    def load(self, session_id: str) -> tuple[PersistenceRecord, ...]: ...


class InMemorySessionStore:
    """Process-local SessionStore with the same record contract as JSONL."""

    def __init__(self) -> None:
        self._records: dict[str, list[PersistenceRecord]] = {}

    def append(self, record: PersistenceRecord) -> None:
        self._records.setdefault(record.session_id, []).append(record)

    def append_many(self, records: tuple[PersistenceRecord, ...]) -> None:
        if not records:
            return
        session_id = records[0].session_id
        if any(record.session_id != session_id for record in records):
            raise SessionStateError("A SessionStore batch must target exactly one Session.")
        updated = [*self._records.get(session_id, ()), *records]
        self._records[session_id] = updated

    def load(self, session_id: str) -> tuple[PersistenceRecord, ...]:
        return tuple(self._records.get(session_id, ()))


class JsonlSessionStore:
    """Append Session records to an inspectable, independently versioned JSONL file.

    Transactional batches use begin/commit frames. Recovery exposes a batch only
    after its matching commit frame and ignores an interrupted tail; the next write
    truncates that tail before appending new authoritative records.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: PersistenceRecord) -> None:
        self._repair_interrupted_tail()
        self._append_transactionally(f"{_encode_record(record)}\n")

    def append_many(self, records: tuple[PersistenceRecord, ...]) -> None:
        if not records:
            return
        session_id = records[0].session_id
        if any(record.session_id != session_id for record in records):
            raise SessionStateError("A SessionStore batch must target exactly one Session.")
        encoded_records = tuple(_encode_record(record) for record in records)
        record_payload = "".join(f"{encoded}\n" for encoded in encoded_records)
        digest = sha256(record_payload.encode("utf-8")).hexdigest()
        batch_id = f"batch-{uuid4().hex}"
        begin = _encode_batch_marker(
            "batch_begin",
            session_id=session_id,
            batch_id=batch_id,
            count=len(records),
            digest=digest,
        )
        commit = _encode_batch_marker(
            "batch_commit",
            session_id=session_id,
            batch_id=batch_id,
            count=len(records),
            digest=digest,
        )
        payload = f"{begin}\n{record_payload}{commit}\n"
        self._repair_interrupted_tail()
        self._append_transactionally(payload)

    def _append_transactionally(self, payload: str) -> None:
        previous_size = self.path.stat().st_size if self.path.exists() else 0
        try:
            self._append_text(payload)
        except Exception:
            try:
                self._truncate_to(previous_size)
            except Exception as rollback_error:
                raise SessionStateError(
                    "Session persistence failed and its rollback could not be made durable."
                ) from rollback_error
            raise

    def _append_text(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            written = stream.write(payload)
            if written != len(payload):
                raise OSError("Session JSONL append was incomplete.")
            stream.flush()
            os.fsync(stream.fileno())

    def load(self, session_id: str) -> tuple[PersistenceRecord, ...]:
        records, _ = self._scan()
        return tuple(record for record in records if record.session_id == session_id)

    def _repair_interrupted_tail(self) -> None:
        if not self.path.exists():
            return
        _, safe_offset = self._scan()
        if safe_offset == self.path.stat().st_size:
            return
        self._truncate_to(safe_offset)

    def _truncate_to(self, offset: int) -> None:
        if not self.path.exists():
            if offset == 0:
                return
            raise SessionStateError("Session persistence disappeared during rollback.")
        with self.path.open("r+b") as stream:
            stream.truncate(offset)
            stream.flush()
            os.fsync(stream.fileno())

    def _scan(self) -> tuple[list[PersistenceRecord], int]:
        if not self.path.exists():
            return [], 0
        data = self.path.read_bytes()
        lines = data.splitlines(keepends=True)
        records: list[PersistenceRecord] = []
        safe_offset = 0
        offset = 0
        pending: tuple[str, str, int, str, list[PersistenceRecord], int] | None = None
        for line_number, raw_line in enumerate(lines, 1):
            line_end = offset + len(raw_line)
            has_newline = raw_line.endswith((b"\n", b"\r"))
            if not has_newline:
                return records, pending[5] if pending is not None else safe_offset
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SessionCorruptionError("Session JSONL is not valid UTF-8.") from exc
            if not line.strip():
                if pending is not None:
                    return records, safe_offset
                raise SessionCorruptionError(f"Blank JSONL record at line {line_number}.")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                if pending is not None:
                    return records, safe_offset
                raise SessionCorruptionError(f"Invalid JSON at line {line_number}.") from exc
            if not isinstance(value, dict):
                if pending is not None:
                    return records, safe_offset
                raise SessionCorruptionError(
                    f"Session record at line {line_number} must be an object."
                )
            record_type = value.get("record_type")
            if pending is None:
                if record_type == "batch_begin":
                    session_id, batch_id, count, digest = _decode_batch_marker(
                        value,
                        expected_type="batch_begin",
                        line_number=line_number,
                    )
                    pending = (session_id, batch_id, count, digest, [], safe_offset)
                elif record_type == "batch_commit":
                    raise SessionCorruptionError(
                        f"Unexpected Session batch commit at line {line_number}."
                    )
                else:
                    if not has_newline:
                        return records, safe_offset
                    records.append(_decode_record(line, line_number=line_number))
                    safe_offset = line_end
            else:
                session_id, batch_id, count, digest, batch_records, batch_start = pending
                if record_type == "batch_commit":
                    if not has_newline:
                        return records, batch_start
                    committed_session, committed_id, committed_count, committed_digest = (
                        _decode_batch_marker(
                            value,
                            expected_type="batch_commit",
                            line_number=line_number,
                        )
                    )
                    encoded = "".join(f"{_encode_record(record)}\n" for record in batch_records)
                    if (
                        committed_session != session_id
                        or committed_id != batch_id
                        or committed_count != count
                        or committed_digest != digest
                        or len(batch_records) != count
                        or sha256(encoded.encode("utf-8")).hexdigest() != digest
                    ):
                        raise SessionCorruptionError(
                            f"Invalid Session batch commit at line {line_number}."
                        )
                    records.extend(batch_records)
                    safe_offset = line_end
                    pending = None
                elif record_type == "batch_begin":
                    raise SessionCorruptionError(f"Nested Session batch at line {line_number}.")
                else:
                    record = _decode_record(line, line_number=line_number)
                    if record.session_id != session_id:
                        raise SessionCorruptionError(
                            f"Session batch changed session_id at line {line_number}."
                        )
                    batch_records.append(record)
                    pending = (session_id, batch_id, count, digest, batch_records, batch_start)
            offset = line_end
        if pending is not None:
            return records, pending[5]
        return records, safe_offset


def _canonical_payload(payload: Mapping[str, object]) -> str:
    try:
        snapshot = json_object_snapshot(payload, label="SessionEntry payload")
        return json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except ValueError as exc:
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


def _encode_batch_marker(
    record_type: Literal["batch_begin", "batch_commit"],
    *,
    session_id: str,
    batch_id: str,
    count: int,
    digest: str,
) -> str:
    return json.dumps(
        {
            "schema": SESSION_SCHEMA,
            "version": SESSION_SCHEMA_VERSION,
            "record_type": record_type,
            "session_id": session_id,
            "batch_id": batch_id,
            "count": count,
            "digest": digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_batch_marker(
    value: Mapping[str, object],
    *,
    expected_type: Literal["batch_begin", "batch_commit"],
    line_number: int,
) -> tuple[str, str, int, str]:
    if (
        value.get("schema") != SESSION_SCHEMA
        or value.get("version") != SESSION_SCHEMA_VERSION
        or value.get("record_type") != expected_type
    ):
        raise SessionCorruptionError(f"Unsupported Session batch marker at line {line_number}.")
    session_id = value.get("session_id")
    batch_id = value.get("batch_id")
    count = value.get("count")
    digest = value.get("digest")
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(batch_id, str)
        or not batch_id
        or type(count) is not int
        or count < 1
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise SessionCorruptionError(f"Invalid Session batch marker at line {line_number}.")
    return session_id, batch_id, count, digest


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
        if not isinstance(entry_kind, str) or not entry_kind:
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
        entry_types: Mapping[str, SessionEntryValidator] | None = None,
    ) -> None:
        self._store = store
        self.session_id = session_id
        self._entry_id_factory = entry_id_factory or (lambda: f"entry-{uuid4().hex}")
        self._entries: dict[str, SessionEntry] = {}
        self._active_leaf_id: str | None = None
        self._next_sequence = 1
        self._closed = False
        self._events: list[AgentSessionEvent] = []
        self._entry_types: dict[str, SessionEntryValidator] = {}
        self.register_entry_types(entry_types or {})

    @classmethod
    def create(
        cls,
        store: SessionStore,
        *,
        configuration: Mapping[str, object],
        session_id: str | None = None,
        entry_id_factory: Callable[[], str] | None = None,
        entry_types: Mapping[str, SessionEntryValidator] | None = None,
    ) -> Session:
        actual_session_id = session_id or f"session-{uuid4().hex}"
        if store.load(actual_session_id):
            raise SessionStateError(f"Session {actual_session_id!r} already exists.")
        session = cls(
            store,
            actual_session_id,
            entry_id_factory=entry_id_factory,
            entry_types=entry_types,
        )
        session._append_entry("configuration", configuration)
        return session

    @classmethod
    def resume(
        cls,
        store: SessionStore,
        session_id: str,
        *,
        entry_id_factory: Callable[[], str] | None = None,
        entry_types: Mapping[str, SessionEntryValidator] | None = None,
    ) -> Session:
        """Reload a closed Session, validate all history, and resume its active leaf."""

        records = store.load(session_id)
        if not records:
            raise SessionStateError(f"Session {session_id!r} does not exist.")
        session = cls(
            store,
            session_id,
            entry_id_factory=entry_id_factory,
            entry_types=entry_types,
        )
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

    def append_custom(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        run_id: str | None = None,
    ) -> SessionEntry:
        """Validate and append one explicitly registered custom SessionEntry."""

        return self.append_custom_many(((kind, payload),), run_id=run_id)[0]

    def append_custom_many(
        self,
        drafts: tuple[tuple[str, Mapping[str, object]], ...],
        *,
        run_id: str | None = None,
    ) -> tuple[SessionEntry, ...]:
        """Validate and persist a custom-entry batch without partial Session mutation."""

        if not drafts:
            return ()
        if self._closed:
            raise SessionStateError("Cannot append to a closed Session.")
        for kind, payload in drafts:
            self._validate_custom_payload(kind, payload, persisted=False)

        prepared: list[SessionEntry] = []
        records: list[PersistenceRecord] = []
        used_ids = set(self._entries)
        parent_id = self._active_leaf_id
        for offset, (kind, payload) in enumerate(drafts):
            entry_id = self._entry_id_factory()
            if not entry_id or entry_id in used_ids:
                raise SessionRelationError("SessionEntry IDs must be non-empty and unique.")
            used_ids.add(entry_id)
            entry = SessionEntry(
                entry_id=entry_id,
                session_id=self.session_id,
                parent_id=parent_id,
                kind=kind,
                payload_json=_canonical_payload(payload),
            )
            prepared.append(entry)
            records.append(
                PersistenceRecord(
                    session_id=self.session_id,
                    sequence=self._next_sequence + offset,
                    kind="entry",
                    entry=entry,
                )
            )
            parent_id = entry_id

        self._store.append_many(tuple(records))
        for entry in prepared:
            self._entries[entry.entry_id] = entry
            self._active_leaf_id = entry.entry_id
            self._next_sequence += 1
            self._events.append(AgentSessionEvent.from_session_entry(entry, run_id=run_id))
            self._events.append(self._active_branch_event(run_id=run_id))
        return tuple(prepared)

    def register_entry_types(
        self,
        entry_types: Mapping[str, SessionEntryValidator],
    ) -> None:
        """Install validated custom types without partially changing the registry."""

        additions = dict(entry_types)
        if _BUILTIN_ENTRY_KINDS.intersection(additions):
            raise SessionStateError("Custom SessionEntry types cannot replace built-in types.")
        for kind, validator in additions.items():
            if not isinstance(kind, str) or not kind or not callable(validator):
                raise SessionStateError(
                    "Custom SessionEntry registrations must be named callables."
                )
            if _is_async_callable(validator):
                raise SessionStateError("Custom SessionEntry validators must be synchronous.")
            existing = self._entry_types.get(kind)
            if existing is not None and existing is not validator:
                raise SessionStateError(f"Custom SessionEntry type already registered: {kind}")
        self._entry_types.update(additions)

    def validate_custom_entry(self, kind: str, payload: Mapping[str, object]) -> None:
        """Validate a custom entry proposal without changing Session state."""

        self._validate_custom_payload(kind, payload, persisted=False)

    def record_compaction(self, plan: CompactionPlan, *, run_id: str | None = None) -> SessionEntry:
        """Persist one validated Active Branch checkpoint without rewriting history."""

        self.validate_compaction(plan)
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

    def validate_compaction(self, plan: CompactionPlan) -> None:
        """Validate a checkpoint proposal without changing Session or Store state."""

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
                elif entry.kind not in _BUILTIN_ENTRY_KINDS:
                    self._validate_custom_payload(entry.kind, entry.payload, persisted=True)
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

    def _validate_custom_payload(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        persisted: bool,
    ) -> None:
        validator = self._entry_types.get(kind)
        if validator is None:
            error = f"Unregistered custom SessionEntry type: {kind}"
            if persisted:
                raise SessionCorruptionError(error)
            raise SessionStateError(error)
        try:
            snapshot = json.loads(_canonical_payload(payload))
            if not isinstance(snapshot, dict):  # pragma: no cover - canonical payload is an object
                raise SessionStateError("Custom SessionEntry payload must be an object.")
            result = invoke_sync_callout(
                cast(Callable[[Mapping[str, object]], object], validator),
                snapshot,
            )
            if inspect.isawaitable(result):
                dispose_awaitable(result)
                raise ValueError("Custom SessionEntry validator must return None synchronously")
            if result is not None:
                raise ValueError("Custom SessionEntry validator must return None synchronously")
        except SessionError:
            raise
        except Exception as exc:
            if persisted:
                raise SessionCorruptionError(
                    f"Custom SessionEntry {kind!r} is invalid: {type(exc).__name__}: {exc}"
                ) from exc
            raise ValueError(
                f"Custom SessionEntry {kind!r} is invalid: {type(exc).__name__}: {exc}"
            ) from exc


def _is_async_callable(value: object) -> bool:
    if inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value):
        return True
    if not callable(value):
        return False
    call = type(value).__call__
    return inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)
