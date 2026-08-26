# Keep the Kernel headless behind one run interface

The Kernel exposes an `AgentKernel` that creates an `AgentRun`: an asynchronously iterable run handle carrying `AgentSessionEvent` values and the `steer`, `follow_up`, `cancel`, `resolve_permission`, and final-result controls for that run. Terminal CLI and future integrations remain thin Hosts so AgentLoop, Session, Model Context, Tool Execution, Provider, Permission Policy, and Extension coordination stay behind one public interface.
