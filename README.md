# Coding Agent Kernel

An independently implemented Python kernel for coding agents, focused on
runtime semantics, tool execution, sessions, and observable event streams.

The first vertical slice provides one deterministic, observable Headless Agent
Run. A thin Terminal CLI drives the same public `AgentKernel`/`AgentRun` seam
that later Hosts will reuse.

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
The Run contains one Turn for this slice; Tool Execution, persistent Session,
real Providers, Permission Policy, and Extensions remain later-ticket work.
ADR 0002 defines the final `AgentRun` control surface, but Ticket 01 exposes only
event iteration and final-result waiting. Steering, follow-up, cancellation, and
permission resolution become public controls in their owning later tickets;
`cancelled` is reserved now so those additions do not change the state model.

The Run/Turn and layered event semantics follow the selected Pi behavioral
baseline. This project keeps only a thin CLI product shell, while the
asynchronously iterable `AgentRun` facade is the Python-facing public boundary.
