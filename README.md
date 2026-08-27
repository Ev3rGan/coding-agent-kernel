# Coding Agent Kernel

## 可恢复、可分支的 Session

Kernel 现在提供持久化的 append-only Session tree。每个不可变
`SessionEntry` 都有稳定 ID 和 parent；当前 `Active Branch` 是从根到所选
leaf 的可恢复路径。`InMemorySessionStore` 与 `JsonlSessionStore` 共享同一
契约，JSONL 使用本项目独立且显式版本化的 schema。

运行确定性的本地演示：

```console
python -m coding_agent demo session-tree
python -m coding_agent demo session-tree --case invalid-entry
```

成功场景实际创建 Session，在权威 `message_end` 后持久化消息，关闭并通过新
store 实例重新加载，从旧 entry fork，再展示两条 sibling branch、当前
Active Branch 和可检查的 JSONL 路径及记录。失败场景以结构化错误拒绝非法
parent，退出码为 1，不静默改写历史或选择其他 branch。

Session 是持久、权威的树；Agent Run 是一次活跃执行；Active Branch 是当前
选择的可恢复历史；Model Context 则是未来 Ticket 04 为单次 Provider 请求组装
的投影。本实现借鉴 Pi 的 tree 语义，但使用独立 Python 文件格式，并通过双
SessionStore seam 强化恢复性与可测性。本 Ticket 不实现 Model Context、
Compaction、branch summary、长期记忆、向量检索或自动记忆。

## English

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
`AgentRun.cancel()` cancels active Provider or Tool Execution work. Model
Context, real Providers, Permission Policy, and Extensions remain later-ticket
work; the implemented Session projection exposes only the selected Active
Branch and deliberately does not assemble Provider context early.

The Run/Turn and layered event semantics follow the selected Pi behavioral
baseline. This project keeps only a thin CLI product shell, while the
asynchronously iterable `AgentRun` facade is the Python-facing public boundary.

## Recoverable, branching Sessions

`python -m coding_agent demo session-tree` creates a versioned JSONL Session,
persists only authoritative messages after `message_end`, closes and reloads it,
forks from an old entry, and prints both sibling branches plus the selected
Active Branch. The `--case invalid-entry` case rejects an illegal parent with a
structured error. Session is durable authoritative history; Agent Run is one
active execution; Active Branch is one recoverable root-to-leaf path; Model
Context remains a later projection. The design borrows Pi's tree semantics,
uses an independent Python persistence format, and deepens recoverability and
testability through matching in-memory and JSONL store seams.
