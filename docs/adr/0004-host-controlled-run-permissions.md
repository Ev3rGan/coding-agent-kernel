# Keep permissions Host-controlled and run-scoped

Each Agent Run uses a Host-selected Permission Mode, and every Permission Request is a one-time decision bound to the final ToolCall arguments and Operation Intent after Extension transformation but before Tool Execution. Models and Extensions cannot raise permissions or impersonate approval; `full` may bypass Kernel approval and workspace containment for an explicitly trusted run, but it never elevates operating-system authority or constitutes a production security Sandbox.
