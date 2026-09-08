# PR #36：20k 产品默认窗口补充门禁

本记录固定 PR [#36](https://github.com/Ev3rGan/coding-agent-kernel/pull/36) 在真实 SWE-bench 验收之后补交的产品默认值改动。改动只把 Kernel、`SWEbenchRunConfig` 与 `swebench run` 的共同默认字符窗口从 100,000 调整为 20,000；显式 `--context-max-characters` 覆盖能力保持不变。

## 真实验收与默认值的关系

两个成功的真实 SWE-bench 运行都先于本改动完成，并显式传入 `--context-max-characters 20000`。因此，真实 DeepSeek Adapter、实际 v2 compaction 与官方 Harness 的 FAIL_TO_PASS / PASS_TO_PASS 双门结果验证的是 20k 这个运行值；本补充改动及其公开入口回归测试证明省略参数后会选用同一个值。没有把仓库内 pytest 冒充为新的 SWE-bench 外部验收。

## TDD 证据

先加入两个公开行为断言：

- `ContextSettings()` 的产品默认值必须为 20,000；
- 省略 CLI 参数时，`SWEbenchRunConfig`、`config.json` 与 `manifest.json` 都必须为 20,000。

红灯结果为 `2 failed`，两个实际值均为 `100000`。随后引入单一权威常量 `DEFAULT_CONTEXT_MAX_CHARACTERS = 20_000`，由三个入口共同引用；同一测试切片转绿为 `3 passed`，并保留显式 4,096 覆盖的回归断言。

## 最终门禁

- Context 与 SWE-bench 定向回归：`61 passed, 1 skipped`；skip 是需要显式启用网络的固定数据集行检查；
- 全量测试：`365 passed, 1 skipped`；
- strict mypy：`Success: no issues found in 34 source files`；
- Ruff lint：通过；Ruff format check：55 files already formatted；
- wheel 构建与隔离安装：通过；安装后的 `ContextSettings`、`SWEbenchRunConfig` 与 CLI help 均显示默认值 20,000；
- 验证 wheel SHA-256：`2e4bd3f8f2a5207a29ccf5cc429fb70200662729c74a6d880c9bd3bd0bfd9b77`。

第一次全量 pytest 调用没有把共享虚拟环境的 `Scripts` 目录放在 `PATH` 首位，导致两个既有工具循环测试内的 `python` 子进程解析到 Windows Store 占位符；修正 PATH 后全量通过。共享虚拟环境中的无关新版 NumPy stub 也与项目 mypy 目标产生语法版本冲突，因此最终 strict mypy 结果来自 Python 3.12、仅安装项目类型检查依赖的隔离环境。这两项环境修正没有改变产品代码。
