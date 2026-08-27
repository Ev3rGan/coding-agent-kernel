from __future__ import annotations

import json
from typing import Any

import pytest

from coding_agent.cli import main


def _records(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines()]


def test_success_cli_renders_public_stream_and_result(capsys: Any) -> None:
    exit_code = main(["demo", "streamed-run"])
    records = _records(capsys.readouterr().out)

    assert exit_code == 0
    assert records[0]["event"] == "agent_start"
    assert records[-2] == {"event": "run_settled", "run_id": "run-1", "state": "settled"}
    assert records[-1]["result"]["state"] == "settled"
    assert records[-1]["result"]["message"]["text"] == "Hello from the Fake Provider."


def test_provider_error_cli_is_structured_without_a_traceback(capsys: Any) -> None:
    exit_code = main(["demo", "streamed-run", "--case", "provider-error"])
    captured = capsys.readouterr()
    records = _records(captured.out)

    assert exit_code == 1
    assert captured.err == ""
    assert records[-2]["event"] == "run_failed"
    assert records[-2]["run_id"] == "run-1"
    assert records[-2]["state"] == "failed"
    assert records[-2]["error"]["code"] == "scripted_provider_error"
    assert records[-1]["result"]["error"]["code"] == "scripted_provider_error"


def test_tool_loop_cli_edits_verifies_and_summarizes_workspace(capsys: Any) -> None:
    exit_code = main(["demo", "tool-loop"])
    records = _records(capsys.readouterr().out)

    assert exit_code == 0
    workspace = records[-1]["workspace"]
    assert workspace["before"] == "value = 1\n"
    assert workspace["after"] == "value = 2\n"
    assert "-value = 1" in workspace["diff"]
    assert "+value = 2" in workspace["diff"]
    assert records[-2]["result"]["message"]["text"].startswith("Updated")
    assert sum(record.get("event") == "turn_start" for record in records) == 2


def test_mixed_batch_cli_exposes_both_modes_and_ordered_results(capsys: Any) -> None:
    exit_code = main(["demo", "tool-loop", "--case", "mixed-batch"])
    records = _records(capsys.readouterr().out)
    endings = [record for record in records if record.get("event") == "tool_execution_end"]

    assert exit_code == 0
    assert {record["batch_mode"] for record in endings} == {"parallel", "sequential"}
    assert [record["tool_result"]["call_id"] for record in endings] == [
        "read-a",
        "read-b",
        "read-before",
        "write",
        "read-after",
    ]
    assert records[-1]["workspace"]["after"] == "mixed = 2\n"


def test_failure_tool_loop_cli_returns_structured_errors_then_settles(capsys: Any) -> None:
    exit_code = main(["demo", "tool-loop", "--case", "failure"])
    captured = capsys.readouterr()
    records = _records(captured.out)
    endings = [record for record in records if record.get("event") == "tool_execution_end"]

    assert exit_code == 0
    assert captured.err == ""
    assert [record["tool_result"]["error"]["code"] for record in endings] == [
        "unknown_tool",
        "invalid_arguments",
        "process_failed",
    ]
    assert records[-2]["result"]["state"] == "settled"


def test_session_tree_cli_reloads_forks_and_exposes_jsonl_without_transient_history(
    capsys: Any,
) -> None:
    exit_code = main(["demo", "session-tree"])
    records = _records(capsys.readouterr().out)
    summary = next(record["session_tree"] for record in records if "session_tree" in record)
    jsonl = next(record["jsonl"] for record in records if "jsonl" in record)

    assert exit_code == 0
    assert summary["session_id"] == "session-demo"
    assert summary["branches"] == [
        ["entry-0001", "entry-0002", "entry-0003"],
        ["entry-0001", "entry-0004", "entry-0005"],
    ]
    assert summary["active_branch"] == ["entry-0001", "entry-0004", "entry-0005"]
    assert summary["active_messages"] == [
        "Continue the deterministic Session.",
        "alternate route",
    ]
    assert len(jsonl["records"]) >= 9
    serialized = json.dumps(jsonl["records"])
    assert "original route" in serialized
    assert "alternate route" in serialized
    assert "message_update" not in serialized
    assert "thinking_delta" not in serialized
    assert "tool_progress" not in serialized


def test_invalid_session_entry_cli_returns_structured_rejection_without_traceback(
    capsys: Any,
) -> None:
    exit_code = main(["demo", "session-tree", "--case", "invalid-entry"])
    captured = capsys.readouterr()
    records = _records(captured.out)

    assert exit_code == 1
    assert captured.err == ""
    assert records[-1]["session_rejected"]["code"] == "session_illegal_relation"
    assert "parent" in records[-1]["session_rejected"]["message"].lower()


def test_context_compaction_cli_shows_bounded_active_branch_without_leakage(
    capsys: Any,
) -> None:
    exit_code = main(["demo", "context-compaction"])
    captured = capsys.readouterr()
    records = _records(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    before = next(record["context_before"] for record in records if "context_before" in record)
    after = next(record["context_after"] for record in records if "context_after" in record)
    provider = next(
        record["provider_request"] for record in records if "provider_request" in record
    )
    tree = next(record["session_tree"] for record in records if "session_tree" in record)

    assert before["estimated_characters"] > after["estimated_characters"]
    assert before["pending_provided"] is True
    assert before["request_contains_pending_marker"] is False
    assert before["injected_provided"] is True
    assert before["request_contains_injected_marker"] is True
    assert after["bounded"] is True
    assert provider["provider_calls"] == 1
    assert provider["contains_injected_marker"] is True
    assert provider["contains_pending_marker"] is False
    assert provider["contains_sibling_marker"] is False
    assert any(record.get("event") == "compaction_succeeded" for record in records)
    assert len(tree["branches"]) == 2
    assert tree["original_entries_preserved"] is True
    assert tree["checkpoint_count"] == 1


def test_context_compaction_summary_error_cli_fails_before_provider_and_recovers(
    capsys: Any,
) -> None:
    exit_code = main(["demo", "context-compaction", "--case", "summary-error"])
    captured = capsys.readouterr()
    records = _records(captured.out)

    assert exit_code == 1
    assert captured.err == ""
    failure = next(record["context_failure"] for record in records if "context_failure" in record)
    assert failure == {
        "code": "compaction_summary_failed",
        "provider_calls": 0,
        "checkpoint_count": 0,
        "original_entries_preserved": True,
        "session_resumable": True,
    }
    assert any(record.get("event") == "compaction_failed" for record in records)
    assert not any(record.get("event") == "message_start" for record in records)


@pytest.mark.parametrize(
    ("case", "terminal"),
    [
        ("steering", "run_settled"),
        ("follow-up", "run_settled"),
        ("cancel", "run_cancelled"),
        ("retry-success", "run_settled"),
        ("retry-failure", "run_failed"),
    ],
)
def test_run_control_cli_cases_expose_one_expected_terminal(
    case: str, terminal: str, capsys: Any
) -> None:
    exit_code = main(["demo", "run-control", "--case", case])
    captured = capsys.readouterr()
    records = _records(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    terminals = [
        record
        for record in records
        if record.get("event") in {"run_settled", "run_cancelled", "run_failed"}
    ]
    assert [record["event"] for record in terminals] == [terminal]
    evidence = records[-1]["run_control"]
    assert evidence["case"] == case
    assert evidence["terminal_count"] == 1
    assert evidence["provider_request_count"] >= 1


def test_run_control_cli_renders_queue_retry_and_session_evidence(capsys: Any) -> None:
    assert main(["demo", "run-control", "--case", "steering"]) == 0
    steering = _records(capsys.readouterr().out)
    assert any(record.get("event") == "message_queued" for record in steering)
    assert any(record.get("event") == "message_injected" for record in steering)
    evidence = steering[-1]["run_control"]
    assert evidence["injected_messages"] == ["inspect the tool result"]
    assert evidence["session_user_messages"].count("inspect the tool result") == 1

    assert main(["demo", "run-control", "--case", "cancel"]) == 0
    cancelled = _records(capsys.readouterr().out)
    assert sum(record.get("event") == "message_dropped" for record in cancelled) == 2
    cancel_evidence = cancelled[-1]["run_control"]
    assert "queued steering" not in cancel_evidence["session_user_messages"]
    assert "queued follow-up" not in cancel_evidence["session_user_messages"]

    assert main(["demo", "run-control", "--case", "retry-success"]) == 0
    retried = _records(capsys.readouterr().out)
    retry = next(record for record in retried if record.get("event") == "provider_retry")
    assert retry["attempt"] == 1
    assert retry["remaining"] == 1
    retry_evidence = retried[-1]["run_control"]
    assert retry_evidence["same_request_retried"] is True
