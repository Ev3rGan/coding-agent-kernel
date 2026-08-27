from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from coding_agent import (
    AgentSessionEventKind,
    AssistantMessage,
    ContextInput,
    ContextPipeline,
    ContextSettings,
    InMemorySessionStore,
    JsonlSessionStore,
    Session,
    SessionCorruptionError,
    SessionRelationError,
    SessionStateError,
    SessionStore,
    UserMessage,
)


@pytest.fixture(params=("memory", "jsonl"))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> SessionStore:
    factories: dict[str, Callable[[], SessionStore]] = {
        "memory": InMemorySessionStore,
        "jsonl": lambda: JsonlSessionStore(tmp_path / "session.jsonl"),
    }
    return factories[str(request.param)]()


def test_store_contract_appends_entries_and_projects_the_active_branch(
    store: SessionStore,
) -> None:
    ids = iter(("config-entry", "first-message"))
    session = Session.create(
        store,
        session_id="session-contract",
        configuration={"provider": "fake", "model": "deterministic"},
        entry_id_factory=lambda: next(ids),
    )

    message = session.record_authoritative_message(AssistantMessage(text="authoritative answer"))

    assert message.entry_id == "first-message"
    assert message.parent_id == "config-entry"
    assert session.active_leaf_id == "first-message"
    assert [entry.entry_id for entry in session.active_branch] == [
        "config-entry",
        "first-message",
    ]


def test_store_contract_reloads_then_forks_from_an_old_entry_without_sibling_leakage(
    store: SessionStore,
) -> None:
    first_ids = iter(("root", "original-leaf"))
    original = Session.create(
        store,
        session_id="session-fork",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(first_ids),
    )
    root = original.active_leaf_id
    original.record_authoritative_message(AssistantMessage(text="original route"))
    original.close()

    resumed = Session.resume(
        store,
        "session-fork",
        entry_id_factory=lambda: "fork-leaf",
    )
    resumed.fork(root)
    resumed.record_authoritative_message(AssistantMessage(text="alternate route"))

    assert [[entry.entry_id for entry in branch] for branch in resumed.branches] == [
        ["root", "fork-leaf"],
        ["root", "original-leaf"],
    ]
    assert [entry.payload.get("text") for entry in resumed.active_branch] == [
        None,
        "alternate route",
    ]


def test_session_operations_expose_configuration_entry_resume_and_active_branch_events(
    store: SessionStore,
) -> None:
    ids = iter(("root", "message"))
    session = Session.create(
        store,
        session_id="session-events",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    assert [event.kind for event in session.drain_events()] == [
        AgentSessionEventKind.SESSION_CONFIGURATION,
        AgentSessionEventKind.SESSION_ENTRY,
        AgentSessionEventKind.ACTIVE_BRANCH,
    ]
    session.record_authoritative_message(AssistantMessage(text="done"))
    assert [event.kind for event in session.drain_events()] == [
        AgentSessionEventKind.SESSION_ENTRY,
        AgentSessionEventKind.ACTIVE_BRANCH,
    ]
    session.close()
    resumed = Session.resume(store, "session-events")
    assert [event.kind for event in resumed.drain_events()] == [
        AgentSessionEventKind.SESSION_RESUMED,
        AgentSessionEventKind.SESSION_CONFIGURATION,
        AgentSessionEventKind.ACTIVE_BRANCH,
    ]


def test_jsonl_rejects_corrupt_illegal_and_incomplete_recovery(tmp_path: Path) -> None:
    corrupt_path = tmp_path / "corrupt.jsonl"
    corrupt_path.write_text('{"schema":', encoding="utf-8")
    with pytest.raises(SessionCorruptionError, match="Invalid JSON"):
        Session.resume(JsonlSessionStore(corrupt_path), "broken")

    illegal_path = tmp_path / "illegal.jsonl"
    illegal_path.write_text(
        '{"entry":{"entry_id":"orphan","kind":"message","parent_id":"missing",'
        '"payload":{"role":"assistant","text":"bad"}},"record_type":"entry",'
        '"schema":"coding-agent-session","sequence":1,"session_id":"illegal",'
        '"version":1}\n'
        '{"record_type":"closed","schema":"coding-agent-session","sequence":2,'
        '"session_id":"illegal","version":1}\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionRelationError, match="root configuration"):
        Session.resume(JsonlSessionStore(illegal_path), "illegal")

    incomplete = InMemorySessionStore()
    Session.create(incomplete, session_id="active", configuration={"provider": "fake"})
    with pytest.raises(SessionStateError, match="no close record"):
        Session.resume(incomplete, "active")


def test_compaction_checkpoint_reloads_as_summary_plus_entries_after_checkpoint(
    store: SessionStore,
) -> None:
    ids = iter(("root", "old-user", "old-answer", "checkpoint", "new-user"))
    session = Session.create(
        store,
        session_id="session-checkpoint",
        configuration={"provider": "fake"},
        entry_id_factory=lambda: next(ids),
    )
    session.record_user_message("old question " * 80)
    session.record_authoritative_message(AssistantMessage(text="old answer " * 80))
    built = ContextPipeline().build(
        ContextInput(
            settings=ContextSettings(max_characters=500),
            active_branch=session.active_branch,
            injected_messages=(UserMessage(text="current"),),
        )
    )
    assert built.compaction is not None
    session.record_compaction(built.compaction)
    session.record_user_message("new after checkpoint")
    session.close()

    resumed = Session.resume(store, "session-checkpoint")
    projected = (
        ContextPipeline()
        .build(
            ContextInput(
                settings=ContextSettings(max_characters=2_000),
                active_branch=resumed.active_branch,
            )
        )
        .context.provider_request.messages
    )

    assert [message.role for message in projected] == ["summary", "user"]
    assert getattr(projected[-1], "text", None) == "new after checkpoint"
    assert [entry.entry_id for entry in resumed.active_branch] == [
        "root",
        "old-user",
        "old-answer",
        "checkpoint",
        "new-user",
    ]


def test_jsonl_rejects_compaction_checkpoint_with_illegal_coverage(tmp_path: Path) -> None:
    path = tmp_path / "illegal-compaction.jsonl"
    common = {
        "schema": "coding-agent-session",
        "version": 1,
        "session_id": "illegal-compaction",
    }
    records = [
        {
            **common,
            "sequence": 1,
            "record_type": "entry",
            "entry": {
                "entry_id": "root",
                "parent_id": None,
                "kind": "configuration",
                "payload": {"provider": "fake"},
            },
        },
        {
            **common,
            "sequence": 2,
            "record_type": "entry",
            "entry": {
                "entry_id": "message",
                "parent_id": "root",
                "kind": "message",
                "payload": {"role": "user", "text": "preserve me"},
            },
        },
        {
            **common,
            "sequence": 3,
            "record_type": "entry",
            "entry": {
                "entry_id": "checkpoint",
                "parent_id": "message",
                "kind": "compaction",
                "payload": {
                    "version": 1,
                    "covered_entry_ids": ["sibling-entry"],
                    "summary": "invalid coverage",
                },
            },
        },
        {**common, "sequence": 4, "record_type": "closed"},
    ]
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )

    with pytest.raises(SessionRelationError, match="illegal coverage"):
        Session.resume(JsonlSessionStore(path), "illegal-compaction")
