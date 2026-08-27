from __future__ import annotations

import json
from typing import Any

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
