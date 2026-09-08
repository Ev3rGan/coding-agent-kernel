# Issue #33：真实 SWE-bench 上下文压缩验收

本目录保存 Issue [#33](https://github.com/Ev3rGan/coding-agent-kernel/issues/33) 在 PR [#35](https://github.com/Ev3rGan/coding-agent-kernel/pull/35) 合并后的真实验收记录。它同时保留成功样本、配置失败样本与改造前的 Django 历史样本，供后续分析压缩质量、代理重复探索和失败模式时复用。

## 结论

两个彼此独立、且不同于历史 Django 样本的 SWE-bench Verified 实例均满足关闭 Issue 前约定的运行门禁：

| 实例与运行 | 字符窗口 | v2 checkpoints | FAIL_TO_PASS | PASS_TO_PASS | 官方 Harness | 结论 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `pallets__flask-5014` / `attempt-1-c5000-agent-failed` | 5,000 | 8 | 未运行 | 未运行 | 未运行 | 保留的失败证据；第 9 次摘要超过 2,500 字符预算 |
| `pallets__flask-5014` / `attempt-2-c20000-resolved` | 20,000 | 3 | 1/1 | 59/59 | resolved | 通过 |
| `scikit-learn__scikit-learn-14141` / `attempt-1-c20000-resolved` | 20,000 | 1 | 1/1 | 2/2 | resolved | 通过 |

成功运行均使用公开入口 `python -m coding_agent swebench run`、真实 `deepseek-v4-pro` Adapter 和官方 SWE-bench Harness，不使用 fake provider，也不以仓库内 pytest 代替外部验收。

本 PR 的后续产品改动把 20,000 设为 Kernel 与 SWE-bench CLI 共用的默认字符窗口；`--context-max-characters` 仍可显式覆盖。由于真实运行先于该默认值改动完成，两个成功样本都显式传入了同一个 20,000 值：真实运行验证这个窗口下会发生压缩并通过双门，公开 CLI 回归测试则验证省略参数后 `config.json` 与 `manifest.json` 也会记录 20,000。

## 被验收的候选版本

- `main` / PR #35 merge commit：`815b166f45627a17f5c5f7641e508497648ddba3`
- 验收时远端 `main`：同一 commit
- 从该 merge commit 构建并隔离安装的 wheel：`coding_agent_kernel-0.1.0-py3-none-any.whl`
- wheel SHA-256：`3fc99aa73f6626e035cb6507bf5b2190cc5fdf98f8f47e13354d33c35bc798ba`
- Python：3.12.10
- SWE-bench：5.0.2
- Verified dataset revision：`78f471bf655a3137b2e8a75af1501690ec009ec3`
- 仓库内固定的 official contract commit：`7a21e05772954cc81471ae19d56f436cecf43c54`
- 本证据 PR 的后续改动：将产品默认字符窗口从 100,000 调整为经上述真实运行验证的 20,000

PR #35 合并前的单元、类型、格式和 wheel 门禁另见 [development-gates/pr-35.md](development-gates/pr-35.md)。这些门禁用于证明构建质量，但不替代本目录的真实 SWE-bench 验收。

## 压缩与连续性证据

### Flask 成功运行

checkpoint 位于 Session sequence 52、87、124，压缩深度依次为 1、2、3：

| sequence | covered messages | summary chars | request chars before | request chars after |
| ---: | ---: | ---: | ---: | ---: |
| 52 | 33 | 3,739 | 97,798 | 6,262 |
| 87 | 53 | 4,673 | 45,627 | 7,232 |
| 124 | 75 | 3,558 | 34,054 | 6,117 |

三条 checkpoint 均为独立 `kind=compaction`、`version=2` 节点。后一条的 `previous_checkpoint_id` 指向前一条，coverage 严格扩展前一条 coverage，且所有 covered ID 都能在 Session 中找到对应 message。摘要标题结构完整，未出现递归 `summary:summary:` 包装。

最后一条 checkpoint 后仍保存 12 条原始 message，代理随后完成源代码与回归测试修改、自测 `60 passed`、生成 patch，并由官方 Harness 判定 resolved。这证明恢复的工作状态不是只够生成一句结束语，而是支持了后续编辑、测试与交付。

### scikit-learn 成功运行

checkpoint 位于 sequence 61，覆盖 37 条 message，将约 46,146 字符压到 5,383 字符；摘要为 2,868 字符。checkpoint 后保留一条原始 assistant message，准确报告已经完成的两个文件修改和 `3 passed` 自测结果，随后生成 patch 并通过官方 Harness。

该样本的 coverage、唯一性、八段摘要结构与递归包装检查同样全部通过。

### 5k 失败与 20k 产品默认值热修复

Flask 首次运行用 5,000 字符窗口强制高频压缩。它先成功持久化 8 条合法 v2 checkpoint，随后第 9 次模型摘要超过 2,500 字符的实际摘要预算，被契约校验以 `compaction_summary_invalid` 拒绝；Run 以 exit 4 停在 agent 阶段，没有伪造 Harness 结果。

保留该失败而不是覆盖后，验收通过公开 CLI 的显式参数将窗口提高到 20,000。按照当前算法，有效摘要预算为 `min(max_summary_characters, max_characters / 2)`，因此从 2,500 提高到 10,000。两个成功样本仍实际触发压缩，说明 20k 没有绕过验收目标。随后，本 PR 将同一个 20k 值提升为产品默认值，同时保留显式覆盖能力。

这次结果支持把 20k 作为当前产品默认值；它不证明任意更小窗口都应成功。未来可单独评估“模型摘要偶发超预算时有限重试或确定性收缩”是否值得实现。

## 证据文件说明

每个新运行目录包含：

- `config.json`、`kernel_configuration.json`、`provenance.json`：运行配置、Kernel 投影设置与官方实例来源；
- `manifest.json`：唯一的运行终态；
- `session.jsonl`：完整持久化 Session，包括 raw message、权限决策和 compaction checkpoint；
- `tool_results.jsonl`：工具结果审计记录；
- `events.lifecycle.jsonl`：移除逐 token 的 `message_update` 与重复的 `tool_execution_update` 后的生命周期事件；
- `audit.json`：从上述记录派生的 checkpoint、lineage、coverage、连续性与 Harness 门禁检查；
- `prediction.jsonl`、`workspace.patch` 和 `official-harness/`：成功运行的预测、补丁和扁平化官方 Harness 报告；
- `source-manifest.json`：外部不可变源 artifact 的文件大小与 SHA-256，以及归档时的去标识化/过滤规则；
- `files.sha256`：仓库内归档文件的 SHA-256。

目录根部的 `archive.sha256` 进一步固定除它自身外的全部归档文件。

`workspace/` 没有复制进 Git：成功运行已保留完整 patch、prediction、Session、ToolResults 和官方报告；工作树副本体积大且可由官方实例与 patch 重建。原始 `events.jsonl` 主要由逐 token/update 事件构成，仓库中保存精简生命周期流，同时在 `source-manifest.json` 中固定原始字节数、行数和 SHA-256。

统一 diff 的空 context 行按格式必须包含一个前导空格；Harness 的 `eval.sh` 内也嵌有同一 diff，生成的日志和测试输出还保留进度条回车及原始空白行。目录内 `.gitattributes` 仅对这些证据文件关闭 `blank-at-eol` 误报，不改变其内容或其他 whitespace 检查。

归档前用实际运行凭据执行了精确值扫描，全部记录为 0 命中；API key 未写入本目录。主机专属路径已替换为 `<external-artifact-root>` 与 `<user-profile>`，不会把开发者机器路径固化进仓库。

## 目录索引

- [机器可读的总验收结论](acceptance.json)
- [20k 产品默认值补充门禁](development-gates/pr-36-default-window.md)
- [Flask 5k 失败运行](runs/pallets__flask-5014/attempt-1-c5000-agent-failed/audit.json)
- [Flask 20k 成功运行](runs/pallets__flask-5014/attempt-2-c20000-resolved/audit.json)
- [scikit-learn 20k 成功运行](runs/scikit-learn__scikit-learn-14141/attempt-1-c20000-resolved/audit.json)
- [改造前 Django 六次运行](historical/django__django-11133/README.md)
- [机制设计](../../design/context-compaction-v2.md)
- [系统调研与 Django 原始分析](../../research/context-compaction-systems.md)

## Issue 状态边界

本目录证明两个真实实例已经满足技术验收条件，但生成记录本身不自动关闭 Issue #33。只有本证据 PR 合并、对应 commit 与结果回写 Issue 后，才具备关闭条件。
