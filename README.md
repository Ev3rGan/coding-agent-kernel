# Coding Agent Kernel

## 固定 Extension 合约

`AgentKernel` 接受调用方按顺序显式构造的普通 Python Extension 实例。Extension
只能通过固定 registry 注册 Tool、Provider、custom `SessionEntry` type 与 Hook
handler；没有目录扫描、entry point、自动发现或热重载。Hook 只接收不可变的类型化
快照，合法的 transform/supplement 会在交给下一 handler 前由 Kernel 重新验证。
Extension Tool 继续使用既有 `ToolRuntime` 的 schema、调度、取消与 structured
`ToolResult` 路径；custom entry 继续使用 append-only Session/Store 路径。

运行三个确定性场景：

```console
python -m coding_agent demo extensions
python -m coding_agent demo extensions --case ordering
python -m coding_agent demo extensions --case invalid-mutation
```

默认场景显式加载示例 Extension，执行自定义 Tool、向 canonical Model Context
补充资源、确定性阻断一个 ToolCall，并持久化已注册的 custom SessionEntry。
`ordering` 展示两个 Extension 按实例顺序和 handler 顺序组合，以及每次改变后的
revalidation；`invalid-mutation` 将 handler 异常转换为明确失败，不调用 Provider、
不污染 Session，也不打印 traceback。

`ExtensionEvent` 通过 `AgentKernel.drain_extension_events()` 独立消费；它记录
registration、dispatch、outcome/revalidation、block/rejection/failure，但绝不会自动
混入 `AgentRun` 的公开 `AgentSessionEvent` Event Stream。本能力借鉴 Pi/Tau 的
registration 与 Hook 思路，简化自动发现、TUI 与热重载，并深化固定状态所有权、
确定性组合和逐次重验证规则。

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
选择的可恢复历史；Model Context 是为单次 Provider 请求构造的有界投影。
本实现借鉴 Pi 的 tree 与确定性投影语义，但使用独立 Python 文件格式，并通过
双 SessionStore seam 强化恢复性与可测性。

## 确定性 Model Context 与 Compaction

每次 Provider 调用都经过唯一的 Context pipeline，固定按 system prompt、active
Tool 描述/guideline、项目资源、Active Branch 投影、当前 injected messages 和
ProviderRequest conversion 组装。`ModelContext` 是不可变值，不持有完整 Session
或 mutable queue。sibling branch 与尚未注入的 pending message 不进入请求。

运行成功与确定性摘要失败场景：

```console
python -m coding_agent demo context-compaction
python -m coding_agent demo context-compaction --case summary-error
```

成功场景展示 compaction 前后以 canonical JSON characters 计量的预算（不是精确
token 数）、持久化 checkpoint、`compaction_succeeded` 事件、两条 sibling
branches、pending/injected 排除与包含证据，以及仍保留的原始 entries。Active
Branch 使用最近有效 checkpoint 的 summary 加 checkpoint 后 entries；checkpoint
记录版本、覆盖范围和 summary，不删除旧历史。

失败场景在 Provider 调用前发出结构化 `compaction_failed`，退出码为 1；它不写入
无效 checkpoint、不删除或改写原始 entries，也不会降级到第二套 builder。Session
仍可关闭、恢复和导航。这里借鉴 Pi 的确定性投影与 compaction 语义，简化
provider-specific prompt 优化，并深化为可观察的 Python Context seam。长期记忆、
向量检索与 Extension registration 仍不在本能力内。

## 控制进行中的 Agent Run

`AgentRun` 是 steering、follow-up、cancel、result/wait 与 Event Stream 的唯一公开
入口。Steering Message 与 Follow-up Message 使用两个独立的 run-scoped FIFO queue：
steering 在完整 Tool batch 之后、下一次 Context/Provider request 之前注入；follow-up
在当前 agent work 自然结束且 steering 已清空后启动后续工作。pending message 不是
Session history，只有实际 injection 才产生权威 user `SessionEntry`。

```console
python -m coding_agent demo run-control --case steering
python -m coding_agent demo run-control --case follow-up
python -m coding_agent demo run-control --case cancel
python -m coding_agent demo run-control --case retry-success
python -m coding_agent demo run-control --case retry-failure
```

JSON Lines 会显示 queue、injection/drop、Provider retry、Session 和唯一终态证据。
取消传播到 Provider、Tool Execution 与 retry wait，并丢弃尚未注入的消息。有限 retry
只处理明确分类为 retryable 的 Provider failure，复用同一 ProviderRequest，失败 attempt
的 partial delta 不会成为权威 Session message。本实现直接借鉴 Pi 的 inner steering、
outer follow-up 与 settled 语义，以统一 Python `AgentRun` interface 表达；不增加 Step、
第二 AgentLoop、多 Agent 或长期后台服务。

## English

An independently implemented Python kernel for coding agents, focused on
runtime semantics, tool execution, sessions, and observable event streams.

## Fixed Extension contract

Callers pass explicitly constructed Python Extension instances to `AgentKernel` in a
defined order. The fixed registry accepts Tools, Providers, custom SessionEntry types,
and Hook handlers only. Every transform or supplement is revalidated before the next
handler, custom Tools remain inside ToolRuntime, and custom entries remain inside the
append-only Session/Store path. `ExtensionEvent` is drained independently from the
Kernel and is never inserted into the public AgentSessionEvent stream.

Run `python -m coding_agent demo extensions`, then use `--case ordering` and
`--case invalid-mutation` to inspect successful capability use, deterministic
composition, and explicit rejection without state damage. The design borrows the
registration and Hook ideas from Pi/Tau, omits discovery/TUI/hot reload, and deepens
Kernel-owned state and deterministic revalidation.

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
Real Providers and Permission Policy remain later-ticket work.
The implemented Context pipeline projects only the selected Active Branch and
explicitly injected messages before every Provider call.

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
Context is one bounded Provider projection. The design borrows Pi's tree
semantics, uses an independent Python persistence format, and deepens
recoverability and testability through matching in-memory and JSONL store
seams.

## Deterministic Model Context and Compaction

`python -m coding_agent demo context-compaction` shows the single Context
pipeline, a character-count budget, a persisted versioned checkpoint, two
sibling branches, pending-message exclusion, explicit injection, and preserved
raw history. The final Fake Provider request contains only the bounded Active
Branch projection. The `--case summary-error` case fails before the Provider,
emits a structured compaction failure, writes no invalid checkpoint, and leaves
the Session resumable. The estimator counts canonical JSON characters and does
not claim tokenizer-exact token counts.

## Controlling an active Agent Run

`python -m coding_agent demo run-control --case <case>` exposes five deterministic
steering, follow-up, cancellation, retry-recovery, and retry-exhaustion scenarios.
The thin CLI observes the public Event Stream and invokes only `AgentRun` controls.
Pending messages stay in two run-scoped FIFO queues and enter Session history only
when injected at their authoritative drain point. Cancellation drops uninjected
messages and converges Provider, Tool, and retry work on one terminal result.
