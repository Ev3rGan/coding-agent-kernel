# Coding Agent Kernel

An independently implemented Python kernel for coding agents, focused on
runtime semantics, tool execution, sessions, and observable event streams.

The Kernel now provides a deterministic, observable model-tool-model loop. A
thin Terminal CLI drives the same public `AgentKernel`/`AgentRun` seam that
later Hosts will reuse.

## Development setup

Python 3.11 or newer is required.

```console
python -m pip install -e ".[dev]"
```

## Observable Headless Agent Run

Run the successful scripted Fake Provider case:

```console
python -m coding_agent demo streamed-run
```

The CLI prints JSON Lines in lifecycle order. Provider increments appear as
`message_update` events containing both the cumulative `AssistantMessage` and
the current Provider Stream Event. The later `message_end` contains the
authoritative message, followed by one `run_settled` terminal event and a final
result record.

Run the deterministic failure case:

```console
python -m coding_agent demo streamed-run --case provider-error
```

This case prints the provider's incremental error, a normalized Agent error,
and one `run_failed` terminal event. It exits with status 1 without an unhandled
exception or traceback.

## Model-tool-model loop

Run the disposable coding task:

```console
python -m coding_agent demo tool-loop
```

The Fake Provider incrementally constructs `read`, `edit`, and `bash`
ToolCalls. The core ToolRuntime executes them in a temporary
LocalCodingEnvironment, sends ordered ToolResults into the next Turn, and the
Provider summarizes the result. JSON Lines expose Provider events, Tool
Execution events, the authoritative final message, and a workspace
before/after/diff record.

Two additional deterministic cases expose scheduling and failure behavior:

```console
python -m coding_agent demo tool-loop --case mixed-batch
python -m coding_agent demo tool-loop --case failure
```

`mixed-batch` demonstrates a pure parallel read batch followed by a mixed batch
that runs entirely sequentially. `failure` normalizes an unknown Tool, invalid
arguments, and a non-zero command into ToolResults before the next Turn ends
normally.

The built-in Tool set is `read`, `write`, `edit`, `bash`, `grep`, `find`, and
`ls`. The first four are enabled by default; search and listing Tools require
explicit opt-in. LocalCodingEnvironment normalizes workspace paths and manages
local processes, incremental stdout/stderr, exit status, timeout, and
cancellation. It is not a production sandbox and makes no security promise for
malicious workspaces.

## Public runtime seam

```python
from coding_agent import AgentKernel, FakeProvider


async def observe_run() -> None:
    kernel = AgentKernel(FakeProvider.streamed_run())
    run = kernel.create_run("Demonstrate an observable run.")

    async for event in run:
        ...  # consume AgentSessionEvent values

    result = await run.result()
```

`AgentRun.state` is one of `active`, `settled`, `cancelled`, or `failed`.
`AgentRun.cancel()` cancels active Provider or Tool Execution work. Persistent
Session, real Providers, Permission Policy, and Extensions remain later-ticket
work; this Ticket deliberately leaves stable ToolRuntime and CodingEnvironment
seams for those additions without implementing them early.

The Run/Turn and layered event semantics follow the selected Pi behavioral
baseline. This project keeps only a thin CLI product shell, while the
asynchronously iterable `AgentRun` facade is the Python-facing public boundary.
