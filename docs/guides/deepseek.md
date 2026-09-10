# DeepSeek 运行指南

本指南说明如何通过公开 CLI 运行真实 DeepSeek 编码任务，以及该路径的凭据、Session、
Provider 和权限边界。

## 前置条件

- Python 3.11 或更高版本；
- 一个 disposable 或明确授权的 workspace；
- 通过进程环境提供的 `DEEPSEEK_API_KEY`；
- Host 能持续读取 stdout，并在交互式权限请求出现时从 stdin 作出决定。

从源码 checkout 安装：

```console
python -m pip install .
```

## 凭据处理

PowerShell：

```powershell
$env:DEEPSEEK_API_KEY = "<YOUR_API_KEY>"
```

Bash：

```bash
export DEEPSEEK_API_KEY="<YOUR_API_KEY>"
```

CLI 不接受包含 key 的命令行参数或配置文件，也不会自动读取 `.env`。可信 Host 可以从
受保护的 secret store 或本地 `.env` 读取凭据，但只应把它注入新建的 `coding-agent`
进程，不能让 workspace、Tool、Session 或日志读取该值。

Provider 捕获凭据后，CLI 会在 Agent Run 期间把它从 Tool 子进程可继承的环境中移除，
结束后再恢复 Host 进程环境。缺少凭据时，命令会在创建 Session 或发起网络请求前以
`deepseek_api_key_missing` 失败。

## 运行任务

```console
python -m coding_agent run --provider deepseek --workspace <workspace> --mode ask "<coding-task>"
```

当前支持官方模型标识 `deepseek-v4-pro` 和 `deepseek-v4-flash`，默认使用
`deepseek-v4-pro`。可用 `--model` 显式选择。

CLI 把 `AgentSessionEvent` 持续渲染为 JSON Lines。最终输出包含：

- authoritative `AgentRunResult`；
- Session ID 与 JSONL 路径；
- changed paths；
- 文本 patch；
- 单独列出的 binary paths。

workspace snapshot 会忽略 `.git`、symlink 和位于 workspace 内的 Session 文件，不会为了
生成 patch 修改目标仓库。

## 恢复 Session

每次新运行会创建 append-only JSONL Session。authoritative assistant messages 和对应
ToolResults 按 Active Branch 持久化，使恢复后的 Provider history 保持完整配对。

使用同一个 store 恢复已经关闭的 Session：

```console
python -m coding_agent run --provider deepseek --workspace <workspace> --mode ask --session-file <sessions.jsonl> --resume <session-id> "<next-coding-task>"
```

`--resume` 只接受已经关闭且通过 replay validation 的 Session。恢复不会把旧的 pending
message、流式增量或进程状态伪装成持久事实。

## 权限交互

`ask` 模式在 `permission_requested` 后从 stdin 接受一次 `approve` 或 `deny`。空输入、EOF
和其他输入默认拒绝。Host 也可以选择 `plan`、`auto` 或 `full`；各模式含义见
[架构说明](../architecture.md#permission-boundary)。

`full` 只跳过 Kernel approval 与 workspace containment。它不会获得额外 OS 权限，也不是
生产 Sandbox；只应在明确可信且可丢弃的环境中使用。

## Provider contract

Adapter 使用官方 `https://api.deepseek.com/chat/completions` endpoint、Bearer auth、
`stream=true` 和 `stream_options.include_usage=true`。实现依据：

- [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Thinking Mode](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [Error Codes](https://api-docs.deepseek.com/quick_start/error_codes/)

SSE 可以跨任意 byte boundary。`reasoning_content`、`content`、增量 `tool_calls`、usage、
finish reason 和 `[DONE]` 分别规范化到 Kernel 的 Provider event contract。最终 ToolCall
arguments 仍由 `AssistantMessageAccumulator` 组装并验证，再经过 Extension Hook、最终参数
重验证与 Host permission resolution；Adapter 本身不执行 Tool，也不复制 AgentLoop。

## 失败与重试

HTTP 429、500/503 和 timeout/transport interruption 会映射到有限 retry 分类。格式错误、
认证、余额、API error 和 malformed stream 产生结构化失败，不泄露 server body 或 key。
失败 attempt 的 partial delta 不会成为 authoritative Session message。

常规测试通过注入的 HTTP transport 运行，不访问 DeepSeek，也不会产生费用。真实凭据验收
只应在 disposable workspace 中显式执行。

返回[文档入口](../README.md)或[中文 README](../../README.md)。
