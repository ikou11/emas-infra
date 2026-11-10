# EMOS 只读录制器与运行目录

> 目的：按照 v1 事件格式（[02-v1-interface-spec.md](02-v1-interface-spec.md)），把 EMOS 的**真实运行过程**录制成 JSONL 文件，作为 mock 适配器的数据源和以后联调的参照。
> 原则：**只读**。录制器不改变 EMOS 的任何行为；关闭时（默认）走 `NullRecorder`，所有调用都是空操作；它内部的任何异常都会被吞掉，不会让仿真崩溃。

## 1. 用法

```bash
cd /home/ikou/project/emos/EMOS
scripts/run_emos.sh <run_name> <config_name> [其他 hydra 覆盖参数...]

# 例：录制 Spot+无人机 的 episode 119
scripts/run_emos.sh spot_drone_ep119 multi_rearrange/llm_spot_drone_rearrange_replica_test habitat_baselines.test_episode_count=1
```

脚本使用的环境变量：
- `EMOS_TRACE=0`：关闭录制器
- `EMOS_VIDEO=0`：不生成视频
- `EMOS_PYTHON`：指定 Python 解释器，默认是 `emos-repro` 环境
- `CUDA_VISIBLE_DEVICES`：默认 0

不使用脚本、直接运行 `habitat_baselines.run` 时，加上以下覆盖参数即可开启录制：

```
habitat_baselines.eval.trace.enabled=True
habitat_baselines.eval.trace.dir=<目录>          # 留空时为 <hydra run dir>/trace
habitat_baselines.eval.trace.snapshot_every=0     # >0 时每 N 步额外记录一次快照
habitat_baselines.eval.trace.include_raw_llm=True
```

## 2. 运行目录约定

每次运行的全部输出都放在 `outputs/runs/<日期_时间>_<run_name>/` 下：

```
outputs/runs/20261110_103000_spot_drone_ep119/
├── command.txt              完整的启动命令
├── run.log                  标准输出和错误输出（最后一行是 exit=<code>）
├── hydra/.hydra/            hydra 解析后的完整配置和覆盖参数
├── trace/episode_<id>.jsonl 录制的事件流，每个 episode 一个文件
├── videos/                  每个 episode 一个 mp4
├── chat_history_output/     EMOS 原有的 LLM 对话记录（leader / agent_i，以及 token 统计）
├── episode_log/             EMOS 原有的步数和子目标记录
└── tb/                      tensorboard
```

`chat_history_output/` 和 `episode_log/` 原本写到当前目录（`./`）下，现在由新增的环境变量 `EMOS_OUTPUT_ROOT` 指定根目录；**未设置时行为与原来一致**。

2026-11-10 之前的输出都已整体移到 `outputs/legacy/`（只移动，没有删除任何内容）：

| 原位置 | 现位置 |
|---|---|
| `outputs/<各次运行>/`、`outputs/2026-10-27/`、`outputs/2026-10-29/` | `outputs/legacy/<原名>/` |
| `chat_history_output/` | `outputs/legacy/chat_history_output/` |
| `episode_log/`（包括格式化失败留下的 `{split}` 目录） | `outputs/legacy/episode_log/` |
| `tb/` | `outputs/legacy/tb/` |

另外两项整理：
- 仓库根目录下的 `meshes/` 和 `collision_meshes/` 只是 10/27 解压数据时留下的**空目录骨架**（文件数为 0，真正的网格文件在 `data/objects/ycb/` 下），已删除。
- 测试配置 `llm_spot_drone_rearrange_replica_test.yaml` 原本引用 `/tmp/emos_episode_119.json.gz`，这个文件重启后会丢失。已复制到 `data/datasets/replica_cad/emos_episode_119.json.gz`（与 `two_agent_manipulation_eval` 中的 episode 119 完全相同），配置也已改为指向新位置。

## 3. 钩子位置（对 EMOS 的全部改动）

| 文件 | 改动 | 产生的事件 |
|---|---|---|
| `habitat-mas/habitat_mas/infra_adapter/recorder.py`（新增） | 录制器本体：`TraceRecorder`、`NullRecorder`、`get_recorder()` | — |
| `habitat-mas/habitat_mas/infra_adapter/snapshot.py`（新增） | 在 env 子进程中构建只读状态快照：agent 位姿与手持物、实体位置、为真的谓词；可选附带实体类型、别名、目标表达式 | — |
| `habitat-lab/habitat/core/environments.py` | `RLTaskEnv.get_infra_snapshot()`，供主进程通过 `envs.call` 调用 | — |
| `habitat-baselines/.../config/default_structured_configs.py` | 在 `EvalConfig` 中新增 `trace: InfraTraceConfig`，默认关闭 | — |
| `habitat-baselines/.../multi_agent/habitat_mas_evaluator.py` | 创建录制器；新 episode 开始、每一步之后、episode 结束时调用 | `episode.started`、`agent.registered`、`state.snapshot`、`episode.ended` |
| `habitat-baselines/.../multi_agent/multi_llm_policy.py` | 组讨论之后记录分工；每个 agent 的 `policy.act()` 之后检查技能是否结束；聊天记录目录改为读取 `EMOS_OUTPUT_ROOT` | `plan.assigned`、`action.ended` |
| `habitat-baselines/.../hrl/hl/llm_policy.py` | 在 LLM 给出动作之后记录"提议的动作"和"实际执行的动作"，以及决策耗时 | `action.proposed`、`action.started`、`llm.usage` |
| `habitat-mas/habitat_mas/agents/crab_agent.py` | 保存 LLM 最近一次的原始函数调用（`last_raw_call`）；`send_request` 时记录消息 | `agent.message` |
| `habitat-lab/habitat/core/env.py` | episode 日志目录改为读取 `EMOS_OUTPUT_ROOT` | — |

## 4. 与接口规范的差异（录制器是只读的）

- 从事件格式 0.2 开始（2026-11-10），录制器会输出**动作语义表**、`capability_tags`（按本体类型给出）和 `goal.conditions`。这些内容来自 EMOS 适配器自己维护的 `habitat-mas/habitat_mas/infra_adapter/emos_profile.yaml`。此前录制的 0.1 版轨迹缺少这些字段，回放时用 `--profile` 补全。

- `agent.registered.supports` 为空数组：录制器不支持任何控制命令，`action.started.gate` 一律为 `allow`。
- `action.ended.outcome` 一律为 `unknown`：成败交给 infra 根据快照推断（审查 F2）。
- **EMOS 自身的回退行为**，会记录在 `action.started.emas_note` 中，同时 `action.proposed` 中保留 LLM 的原始意图：
  - `emas_mapped:send_request->wait`：发送请求后，EMOS 会让该 agent 等待 500 步；
  - `emas_fallback:invalid_action`：动作名不在技能表中，EMOS 回退为 wait；
  - `emas_fallback:no_llm_output`：LLM 没有给出动作（例如 agent 向自己发送请求）。
- episode 结束时，仍在执行中的动作会以 `status=interrupted` 结束（`detail="episode ended"`）。最后一步之后环境已经自动重置，因此**没有** episode 结束时的快照。
- `step` 的含义：本 episode 中已经执行的环境步数。某一步中做出的决策和技能结束，都标记为执行该步**之前**的计数；该步执行完之后的快照，标记为执行之后的计数。

## 5. 校验录制结果

```bash
python /home/ikou/project/emos/embodied-infra/tools/validate_trace.py outputs/runs/<run_id>/trace
```

脚本会检查信封字段、`seq` 和 `step` 的单调性、事件的先后顺序（包括动作生命周期）、每个必选事件是否出现、动作参数是否引用了不存在的实体。
