# SWE-bench Verified 基础性能冒烟报告（2026-11-06）

本报告记录一次小样本基础性能冒烟：用当前仓库的 `python -m coding_agent swebench run`
在本地 Docker 上运行候选 SWE-bench Verified 实例，观察成功路径与失败路径，作为后续
CodingAgent 优化的输入。

样本只有 3 次运行（2 个实例），**不是**能力评分，也不能外推为整体解决率。

## 结论

3 次运行中 1 次被官方 Harness 判为 `resolved`。失败不是单点模型能力问题，而是三个
运行框架层的可用性缺陷：

1. **P0 容器路径未映射**：模型按系统提示使用 `/testbed`，文件工具与权限分类却按宿主
   路径解析，导致合法读写被判定为 `outside` 并拒绝。
2. **P0 非交互审批默认拒绝**：`--mode auto` 在非交互入口把组合 shell 命令判为
   `unknown` 并拒绝，正常开发命令无法通过，任务停在 `no_patch`。
3. **P1 压缩摘要契约不稳定**：`--mode full` 下首次压缩即因摘要标题结构不匹配失败，
   Run 以 `compaction_summary_invalid` 结束，且没有 checkpoint 落盘可供取证。

同日的 P0/P1 修复与两次回归见下文[回归验证](#回归验证同日)。结论是：路径映射与默认
`full` 已生效，压缩失败重试后两次回归均 `resolved`；剩余待办是组合 shell 命令分类与
失败取证。

## 运行矩阵

| 实例 | 模式 | 结果 | 退出码 | 耗时 | 轮次 | 工具调用 | 输入/输出 token | 审批拒绝 | 压缩 | 官方 Harness |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| `scikit-learn__scikit-learn-14141` | auto | success | 0 | 4.6 min | 27 | 29 | 69,547 / 3,326 | 6 / 29 | 1/1 | resolved（F2P 1/1，P2P 2/2） |
| `pallets__flask-5014` | auto | no_patch | 6 | 8.7 min | 37 | 47 | 111,334 / 7,993 | 25 / 47 | 2/2 | 未运行 |
| `pallets__flask-5014` | full | agent_failed | 4 | 2.6 min | 17 | 16 | 43,703 / 2,106 | 0 / 16 | started 1，succeeded 0 | 未运行 |

机器可读摘要见 [runs.json](runs.json)。

## 环境与固定项

- Windows Host，Docker Desktop 29.7.2，Linux containers；Docker 数据目录位于 D 盘。
- `swebench` 5.0.2，`datasets` 5.0.1，`coding-agent-kernel` 0.1.0，Python 3.12。
- 模型 `deepseek-v4-pro`，上下文字符窗口 20,000。
- 数据集 `SWE-bench/SWE-bench_Verified`，固定 revision `78f471bf655a3137b2e8a75af1501690ec009ec3`。
- 每次运行使用新的 artifact 目录；容器以 `--network none` 创建并 bind mount 单个 workspace。

## 发现

### P0-1：容器内绝对路径被按宿主路径解析

证据：

- `flask5014-baseline` 中 `ls /testbed`、`find /testbed`、`read /testbed/src/flask/blueprints.py`、
  `grep /testbed/...` 均被权限分类为 `read/outside`，随后被 Host 拒绝。
- 同一运行中最终修改 `edit /testbed/src/flask/blueprints.py` 被分类为 `write/outside` 并拒绝，
  这是该运行停在 `no_patch` 的直接原因。
- `scikit-learn__scikit-learn-14141` 同样先被拒绝，模型改用相对路径后才成功。

根因：

- 系统提示把工作区写成容器路径 `Workspace root: /testbed`。
- [`ContainerCodingEnvironment.resolve_path`](../../../src/coding_agent/swebench.py:488) 用
  `(self.workspace / path).resolve()` 解析；Windows 上 `/testbed/x` 变成宿主盘符下的
  `<drive>:\testbed\x`，落在 workspace 之外。
- [`PermissionPolicy._normalize_target`](../../../src/coding_agent/permissions.py:410) 在权限
  分类阶段也做同样的宿主解析，所以请求在进入 Tool 之前就被判为 `outside`。
- `bash` 的 `cwd` 参数同样受影响；`cwd: /testbed` 被分类为 `shell/outside` 并拒绝。

影响：文件工具对容器绝对路径完全不可用；模型必须猜相对路径。这直接降低 SWE-bench
入口的可用性，也让 `--mode full` 无法完全兜底（绕过审批后仍会在 environment 层抛
`path escapes workspace`）。

建议：在权限分类与 environment 解析两层都增加容器前缀映射（`/testbed` → workspace），
并覆盖 `bash` 的 `cwd`；作为兜底，可在系统提示中明确要求使用相对路径。

### P0-2：`auto` 模式在非交互入口默认拒绝，正常命令被挡

证据：

- `flask5014-baseline` 的 47 次权限决策中 25 次被拒；其中 `bash/unknown/unknown` 13 次，
  典型命令是 `grep ... | head`、`pwd && ls`、`cat ... | head`、`python -c "..."`、`awk ... | head`。
- 模型每次被拒后更换命令重试，消耗 37 轮仍未产出 patch。
- `scikit-learn__scikit-learn-14141` 有 6 次拒绝，最终虽 `resolved`，但模型在结束语中明确
  说明无法运行聚焦测试（`bash` 持续被拒），成功未经过自测。

根因：

- [`_canonical_permission_actions`](../../../src/coding_agent/permissions.py:213) 在 `auto`
  下对 `unknown`、`outside`、`network` 一律返回 `ASK`。
- CLI 非交互 stdin 读到 EOF 时按 deny 处理（`_read_permission_decision`）。
- [`PermissionPolicy._bash_intent`](../../../src/coding_agent/permissions.py:315) 对含
  `&&`、`|`、`;`、`$()` 等元字符的命令直接判 `unknown`，没有拆段分类。
- 后果是 `swebench run --mode auto` 在无人值守环境实际上不可用；文档已说明 `auto` 需要
  Host 审批，但 SWE-bench 入口仍把它作为默认值。

建议：SWE-bench 入口默认改为 `full`，或提供明确的非交互预授权策略；同时把组合命令
按元字符拆段后逐段分类，并对 `git status`、`git diff`、`pytest` 等常见命令建立只读白名单。

### P1-1：压缩摘要契约失败且不可取证

证据：

- `flask5014-full` 在 17 轮、16 次工具调用后，`compaction_started=1`、`compaction_succeeded=0`，
  Run 以 `compaction_summary_invalid` 结束。
- session 中没有 `compaction` 记录，events 中没有失败原因事件，只有 `manifest.json` 的
  `diagnostic` 保留了错误码与一句话描述。
- 同一实例的 `auto` 运行成功完成 2 次压缩，说明压缩路径本身可用，但模型摘要输出是否
  严格匹配八段标题并不稳定。

根因：

- [`validate_compaction_summary`](../../../src/coding_agent/compaction.py:156) 要求标题序列
  严格等于 `COMPACTION_SUMMARY_HEADINGS`（Goal、Constraints and preferences、Done and
  verified、In progress、Blocked、Key decisions and relevant rejected approaches、Exact
  files, symbols, commands and errors、Next steps）。
- 失败没有重试或降级路径；失败前也不落 checkpoint，导致无法复盘坏摘要。

建议：对摘要失败增加有限重试或确定性收紧；失败时记录 `compaction_failed` 事件与脱敏后的
摘要摘要（标题列表、长度），便于定位；评估是否把标题校验放宽为“包含必需段落”而非严格相等。

### P2：其他观察

- **失败取证不足**：`no_patch` 与 `agent_failed` 都只留一句 `diagnostic`；建议在 manifest
  之外增加结构化的失败阶段证据（最后一次被拒的工具调用、最后一次压缩错误）。
- **事件体积**：单次运行 `events.jsonl` 为 3.3–5.9 MB，主要是 `message_update`；归档时需要
  过滤，否则证据目录会迅速膨胀。
- **成功未自测**：`scikit-learn__scikit-learn-14141` 的 `resolved` 只证明补丁通过官方测试，
  不证明模型在运行中验证过；这与 P0-2 直接相关。

## 回归验证（同日）

P0 修复包括：容器路径别名（`/testbed` → workspace）在 `PermissionPolicy` 与
`ContainerCodingEnvironment` 两层生效，`bash` 的 `cwd` 同步映射；`swebench run` 的
默认 Permission Mode 改为 `full`，`auto` 仍可显式选择。

用同样的两个实例重跑：

| 实例 | 模式 | 结果 | 轮次 | 工具调用 | 输入/输出 token | 审批拒绝 | 压缩 | 官方 Harness |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| `scikit-learn__scikit-learn-14141` | full（新默认） | agent_failed | 12 | 20 | 35,326 / 2,098 | 0 / 20 | started 1，succeeded 0 | 未运行 |
| `scikit-learn__scikit-learn-14141` | auto | success | 17 | 26 | 55,317 / 3,391 | 3 / 26 | 1/1 | resolved（F2P 1/1，P2P 2/2） |
| `pallets__flask-5014` | auto | agent_failed | 15 | 19 | 43,526 / 2,446 | 2 / 19 | started 1，succeeded 0 | 未运行 |

### 已修复

- **路径映射生效**：`/testbed` 文件工具与 `bash` cwd 不再被判为 `outside`。flask auto 的
  outside 拒绝从 9 次降为 0；scikit full 的 20 次权限决策全部 approved；两次运行都没有
  出现 `path escapes workspace`。scikit auto 因此恢复 `resolved`。
- **默认 full 生效**：不传 `--mode` 时 `config.json` 记录 `permission_mode: full`，运行中
  不再出现 permission prompt。

### 仍阻塞

- **P1 压缩摘要契约**：三次未成功运行都在首次压缩时 `compaction_summary_invalid`。默认
  `full` 放行了更多探索与大输出，压缩路径成为当前首要瓶颈；`scikit-learn-14141` 的 auto
  运行是唯一成功压缩（1/1）的样本。
- **P2 grep 非 UTF-8 文件**：scikit full 运行中 `grep` 对含非 UTF-8 字节的文件抛出
  `UnicodeDecodeError`，整个调用失败，而不是跳过该文件。建议捕获解码错误并按文件跳过。

### 回归结论

P0 两项修复达到预期：路径不再误判，非交互默认 `full` 可用。当前阻止 SWE-bench 稳定
resolved 的不是权限或路径，而是压缩摘要失败；下一步应优先处理 P1。

## P1/P2 修复与第二次回归（同日）

在上一轮回归之后继续处理两项：

- **压缩摘要重试**：第一次摘要因标题结构无效时，最多再请求一次；第二次在 system prompt
  与 prompt 中都强化“只返回八个二级标题、顺序一致、不要额外标题或正文”。两次尝试的
  token 用量都累计进 `summary_usage`。
- **grep 跳过非 UTF-8 文件**：读取文件时捕获 `UnicodeDecodeError` 并继续扫描，不再让整个
  调用失败。

用同样两个实例再次回归：

| 实例 | 模式 | 结果 | 轮次 | 工具调用 | 输入/输出 token | 审批拒绝 | 压缩 | 官方 Harness |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| `scikit-learn__scikit-learn-14141` | full（默认） | success | 35 | 41 | 124,416 / 4,612 | 0 / 41 | 2/2 | resolved（F2P 1/1，P2P 2/2） |
| `pallets__flask-5014` | auto | success | 33 | 50 | 107,466 / 8,971 | 7 / 50 | 3/3 | resolved（F2P 1/1，P2P 59/59） |

### 第二次回归结论

- 两次运行都由官方 Harness 判为 `resolved`。flask-5014 从上一轮的 `no_patch` / 压缩失败
  恢复；scikit-learn 的默认 `full` 运行从压缩失败恢复。
- 两次运行共完成 5 次压缩，全部成功，没有再出现 `compaction_summary_invalid`；本次没有
  观察到重试被触发的证据，但重试为同类失败提供了兜底。
- flask auto 的 7 次拒绝全部是 `bash/unknown`（`head`、`sed`、`python -c` 等组合命令），
  不再包含任何路径类拒绝，也没有阻止最终 `resolved`。
- 组合 shell 命令的分类仍是 P1 待办：它降低了 auto 模式的成功率与效率，但在文件工具可用
  之后不再是硬阻断。

## 优化优先级

| 优先级 | 动作 | 状态 | 涉及位置 | 验证方式 |
| --- | --- | --- | --- | --- |
| P0 | 容器路径映射（文件工具、权限分类、`bash` cwd） | 已完成 | `swebench.py`、`permissions.py`、`environment.py` | 回归中 `/testbed` 不再出现 `outside` 拒绝 |
| P0 | 非交互入口默认 `full` | 已完成 | `cli.py`、`docs/guides/swebench.md` | 回归中无 permission prompt |
| P1 | 压缩摘要失败一次重试 + 提示强化 | 已完成 | `context.py` | 两次回归共 5 次压缩全部成功 |
| P1 | 组合 shell 命令拆段分类与只读白名单 | 待办 | `permissions.py` | 单元测试覆盖 `&&`、`|`、`;`、`$()` |
| P2 | grep 跳过非 UTF-8 文件 | 已完成 | `tools.py` | 含二进制文件的目录不再整体失败 |
| P2 | 结构化失败证据与事件裁剪 | 待办 | `swebench.py`、`events.py` | artifact 体积下降，失败可复盘 |

## 复现

```console
python -m coding_agent swebench run \
  --instance pallets__flask-5014 \
  --artifacts <new-artifact-dir> \
  --timeout 1800 \
  --harness-timeout 1800
```

默认 Permission Mode 是 `full`；需要验证审批矩阵时显式传 `--mode auto`，但非交互环境
遇到审批请求会按 deny 处理。

## 清理与脱敏

- 运行结束后容器由 evaluator 关闭；本次共 8 个 artifact 目录（3 次冒烟 + 5 次回归/重试）
  在提取摘要后删除，原始 `session.jsonl`、`events.jsonl` 未提交。
- 报告与 `runs.json` 不含 API key、宿主绝对路径或容器 ID；路径统一以 `<artifact-dir>` 表示。
- 本地 SWE-bench 镜像保留，便于后续补跑；如需释放空间可另行删除。

## 局限

- 只有 2 个实例、3 次运行，不能得出整体解决率；`flask-5014` 的两次失败只说明该配置下的
  可用性问题，不代表模型在所有实例上的能力。
- 单模型（`deepseek-v4-pro`）、单上下文窗口（20,000 字符）、单 Host（Windows + Docker Desktop）。
- 未覆盖 `django`、`sympy` 等大仓库实例；后续可用它们验证压缩与大规模探索的稳定性。
