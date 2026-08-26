# Coding Agent Kernel

This context defines the language used to describe the headless Coding Agent runtime, its observable execution, persistent history, and controlled extensions.

## Runtime

**Coding Agent Kernel**:
The headless runtime that coordinates model calls, tool execution, context construction, and persistent sessions.
_Avoid_: Coding Agent product, CLI, IDE

**Host**:
A user-facing or integration-facing program that drives the Kernel through its public interface.
_Avoid_: Kernel, AgentLoop

**Agent Run**:
One active execution started from user input and continued until the Agent settles, is cancelled, or fails.
_Avoid_: Session, Step

**Turn**:
One model response together with the tool calls and tool results produced from that response.
_Avoid_: Step, user interaction

## Model and tools

**Provider Stream Event**:
A raw incremental model event representing text, thinking, tool-call construction, completion, or failure.
_Avoid_: AgentEvent, text delta

**ToolCall**:
A model-authored request identifying a tool, a call ID, and structured arguments.
_Avoid_: command, Tool Execution

**Tool Execution**:
One runtime attempt to prepare and execute a ToolCall.
_Avoid_: ToolCall, ToolResult

**ToolResult**:
The authoritative success or error result associated with a ToolCall and returned to the model.
_Avoid_: tool progress, console output

## Events

**AgentEvent**:
A low-level AgentLoop lifecycle event for an agent, turn, message, or tool execution.
_Avoid_: AgentSessionEvent, ExtensionEvent

**AgentSessionEvent**:
A product-orchestration event that combines AgentEvent with queue, retry, compaction, persistence, and settled-state changes.
_Avoid_: SessionEntry, ExtensionEvent

**ExtensionEvent**:
A lifecycle notification or controlled interception request dispatched to the Extension runtime.
_Avoid_: AgentSessionEvent, public event stream

**Event Stream**:
The ordered asynchronous sequence of AgentSessionEvents exposed by an active Agent Run.
_Avoid_: Session log, ExtensionEvent bus

## Session

**Session**:
A durable append-only tree containing the recoverable history and configuration of Coding Agent interactions.
_Avoid_: Agent Run, model context

**SessionEntry**:
One immutable persisted record in a Session tree.
_Avoid_: AgentSessionEvent, streaming update

**Active Branch**:
The root-to-leaf SessionEntry path selected as the current recoverable history.
_Avoid_: entire Session, all branches

**Model Context**:
The messages, system prompt, and active tools projected for one model request.
_Avoid_: Session, complete transcript

**Compaction**:
A Session checkpoint that represents older Active Branch history with a summary while retaining newer entries.
_Avoid_: alternate ContextBuilder, history deletion

## Runtime control

**Steering Message**:
User input queued for injection after the current tool batch and before the next model request.
_Avoid_: Follow-up Message

**Follow-up Message**:
User input queued until the current Agent work has naturally settled and no Steering Message remains.
_Avoid_: Steering Message

**Permission Mode**:
Run-scoped authority selected by a Host that determines whether an Operation Intent is allowed, denied, or requires a Permission Request.
_Avoid_: Execution Mode, Sandbox

**Operation Intent**:
A normalized description of the targets and possible side effects of the final ToolCall arguments used to make a permission decision.
_Avoid_: ToolCall, Execution Mode

**Permission Request**:
A pending one-time Host decision bound to a ToolCall's final arguments and Operation Intent before Tool Execution can continue.
_Avoid_: model approval, permanent trust rule

## Extensions

**Extension**:
A registered package of Kernel capabilities and handlers operating through fixed registration and Hook interfaces.
_Avoid_: Cordis component, arbitrary state mutation

**Hook**:
A fixed interception seam where an Extension can observe, transform, block, or supplement a specific Kernel operation.
_Avoid_: Extension, Event Stream
