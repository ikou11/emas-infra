# EMOS 对 v1 接口的支持情况审查

> 审查对象：`/home/ikou/project/emos/EMOS`（`embodied_mas` 分支 + 本地未提交的修改），审查日期 2026-11-10。
> 结论：**v1 的全部必选契约 EMOS 都具备实现能力**。其中多数只需要在现有位置加钩子，只有 `request_replan` 需要真正改造 EMOS（已在 v1 中降为可选）。

图例：✅ 现成可用　🟡 小改（只加钩子或少量代码）　🔴 需要改造 EMOS 逻辑

## 1. EMOS 执行链路（与接口相关的部分）

```
HabitatMASEvaluator.evaluate_agent             habitat-baselines/.../multi_agent/habitat_mas_evaluator.py
 ├─ 新 episode: envs.call("get_task_text_context")   → 场景描述 + robot resume   (:156)
 ├─ 每一步: agent.actor_critic.act(...)               → MultiLLMPolicy.act        (:175)
 │    ├─ 首步(prev_actions 全 0): group_discussion()   → 每个 agent 的子任务       multi_llm_policy.py:516
 │    └─ 对每个 agent: HierarchicalPolicy.act
 │         ├─ _update_skills: 若 _cur_call_high_level → LLMHighLevelPolicy.get_next_skill
 │         │     └─ CrabAgent.chat → OpenAIModel.chat → (动作名, 参数)          llm_policy.py:137
 │         │   → skill.on_enter(args) → _parse_skill_arg（实体名 → PDDL 实体）
 │         ├─ skill.act → 底层动作
 │         └─ _get_terminations: skill._is_skill_done / 超时 / HL.get_termination
 │              → 更新 _cur_call_high_level（下一步是否需要重新决策）
 ├─ envs.step(actions) → observations, infos(指标)
 └─ done → 统计指标、写视频；Env 内部写 episode_log
```

关键事实：
- 每个 agent 只在当前技能结束后才会再次决策，**不同 agent 的决策时刻互不同步**。
- 环境（仿真）运行在 VectorEnv 的**子进程**中，策略和 LLM 在**主进程**中。主进程要读取仿真状态，只能通过 `envs.call(...)`，`get_task_text_context` 已经是这种用法的先例。
- EMOS 断言只支持单环境（`multi_llm_policy.py:503`），v1 只考虑 `num_environments=1`。

## 2. 契约逐项对照

### 2.1 事件

| 契约 | EMOS 中的位置 | 判断 | 说明 |
|---|---|---|---|
| `episode.started` | evaluator 中检测到 `episode_id` 变化处（`habitat_mas_evaluator.py:147-165`） | 🟡 | `task_text` 取自 `pddl_text_goal` 观测；`scene_text` 取自 `get_task_text_context()["scene_description"]`；实体表需要新增一个 env 侧方法，读取 `pddl_problem.all_entities` |
| `agent.registered` | 同上，`robot_resume`（JSON，以 `agent_i` 为键） | ✅ | resume 已有；`action_space` 取自 `llm_policy.ACTION_POOL`，它们是 crab Action，自带 JSON schema |
| `plan.assigned` | `group_discussion()` 返回的 `AgentArguments`（`multi_llm_policy.py:531`） | ✅ | 返回值中包含每个 agent 的 `subtask_description`；token 用量在函数内部打印，需要顺带返回 |
| `action.proposed` | `LLMHighLevelPolicy.get_next_skill` 中拿到 `llm_output` 之后、写入 `next_skill` 之前（`llm_policy.py:149-158`） | 🟡 | 天然的拦截点，`gate` 也放在这里 |
| `action.started` | 同上，确定 `next_skill` 之后 | 🟡 | |
| `action.ended` | `MultiLLMPolicy.act` 中，`policy.act(...)` 返回之后读取 `policy._cur_call_high_level`：为 True 表示当前技能在这一步结束 | 🟡 | 超时的判断方式：`skill._cur_skill_step >= skill._max_skill_steps`。**没有语义上的成败**，见 F2 |
| `state.snapshot` | 需要新增 `RLTaskEnv.get_infra_snapshot()`，再转调 task 方法，主进程通过 `envs.call` 获取 | 🟡 | 数据来源：`articulated_agent.base_pos/base_rot`、`pddl_problem.sim_info.get_entity_pos`、`pddl_problem.get_true_predicates()`。全部是只读操作 |
| `agent.message` | `CrabAgent.chat` 中处理 `send_request` 的分支（`crab_agent.py:116-127`） | 🟡 | |
| `llm.usage` | `OpenAIModel.token_usage` | ✅ | |
| `episode.ended` | evaluator 中 `not_done_masks` 为 False 的分支（`habitat_mas_evaluator.py:308`），从 `infos[i]` 取指标 | ✅ | |

### 2.2 控制

| 契约 | EMOS 中的实现方式 | 判断 | 说明 |
|---|---|---|---|
| `gate: allow` | 什么都不做 | ✅ | |
| `gate: deny` | 在 `get_next_skill` 中把技能换成 `wait`，参数为一个较小的步数（如 `["20"]`），并把原因写入该 agent 的消息队列 | 🟡 | `llm_output is None` 时 EMOS 原本就是这样回退的，属于已有路径 |
| `gate: replace` | 用替换后的动作名和参数写入 `next_skill/skill_args_data` | 🟡 | 替换后的参数同样要经过实体校验，否则仍有崩溃风险 |
| `inject_message` | `CrabAgent.message_pipe[agent_id].append(text)`：下一次 `chat` 时会把队列中的文本拼进 prompt（`crab_agent.py:110-113`） | ✅ | 已有通道。需要注意 `message_pipe` 是类变量，跨 episode 不会清空（F6） |
| `interrupt` | 重写 `LLMHighLevelPolicy.get_termination`：有待处理的打断请求时返回 True。HierarchicalPolicy 会在下一步重新调用 HL（`hierarchical_policy.py:559`） | 🟡 | 基类中的这个钩子原本就是为"强制结束当前技能"设计的 |
| `request_replan` | 首步之外不会再触发组讨论（判断条件是 `not env_prev_actions.any()`）。需要新增标志位，在中途重新调用 `group_discussion`，并带上失败的上下文 | 🔴 | v1 列为可选 |

### 2.3 北向工具

| 场景 | EMOS 现状 | 判断 | 说明 |
|---|---|---|---|
| 讨论阶段 | `OpenAIModel` 在 `discussion_stage=True` 时会执行工具调用，把结果返回给模型并继续循环；`DISCUSSION_TOOLS` 目前是空列表 | 🟡 | 需要把 infra 的工具包装成 crab `Action`（pydantic 模型加上 `run`），再追加到 `DISCUSSION_TOOLS` |
| 执行阶段 | 执行模式下，第一个工具调用会被直接当作环境动作返回给策略 | 🔴 | 需要修改 `OpenAIModel`：信息类工具在内部执行并继续循环，直到模型给出环境动作。v1 不做 |

## 3. 关键发现

**F1　非法实体名会让整个进程崩溃。** `OracleNavPolicy._parse_skill_arg`（`oracle_nav.py:131-139`）以及 `OraclePickPolicy`、`OraclePlacePolicy` 对应的解析函数，在 `get_entity()` 找不到实体时都会 `raise ValueError`，而上层没有捕获。LLM 只要写错一个目标名，整次运行就会中断。
→ 这让 `unknown_entity` 闸门成为 v1 收益最高的功能。同时，即使 infra 关闭，这也是 EMOS 的稳定性问题。

**F2　技能"结束"不等于"成功"。**
- `pick` 的结束条件是 `is_holding`，即拿着**任意**物体，最多执行 100 步。
- `place` 的结束条件是物体到目标的距离小于 0.02，最多 100 步。
- `nav` 的结束条件是 `has_finished_oracle_nav`，最多 1000 步。
- 到达步数上限时，`force_end_on_timeout=False`，技能**只是结束，不会报告失败**。
- 实测（`outputs/runs/20261110_095609_spot_drone_ep119`）：`place` 跑满 100 步、以 `timeout` 结束，但 PDDL 后置条件照样生效，任务成功。也就是说，**`timeout` 不代表失败，反过来 `completed` 也不代表成功**。成败只能对照动作结束后的状态快照来判断，例如看 `holding(...)` 和 `at(...)` 谓词是否成立。

→ 接口中把 `status`（执行层面）和 `outcome`（语义层面）分开，`outcome` 允许为 `unknown`，由 infra 根据动作结束后的快照推断。

**F3′　快照函数在子进程中抛出异常会直接杀死 env worker。** VectorEnv 的 worker 循环不捕获异常（`vector_env.py:274-284`），主进程只会收到 `EOFError`，之后整次运行失败。第一版录制器调用 `PddlProblem.get_true_predicates()` 时就遇到了这个问题：某些谓词（`in(obj, receptacle)` 这类）在 `_is_obj_state_true` 中 `raise NotImplementedError`。现在的录制器逐个谓词捕获异常，整个快照函数也有兜底。→ 以后凡是要在 env 侧运行的 infra 代码，都必须保证不抛出异常。

**F3　仿真状态在子进程里。** 可以沿用 `envs.call` 的模式。每一步都调用会带来进程间通信开销，所以只在动作边界（以及可选的每隔 N 步）获取快照。需要注意：`get_task_text_context` 本身在 agent 位置为 0 时会**随机放置** agent（`rearrange_task.py:448`），有副作用；新增的快照方法必须是纯读取。

**F4　决策时刻不同步。** 各 agent 只在自己的技能结束时才决策，`action.*` 事件天然以 agent 为单位、异步出现。这也是 `step` 时钟必须出现在每条事件中的原因。

**F5　`wait` 一次等待 500 步。** 由 `LLMHighLevelPolicy` 和 `CrabAgent` 写死为 `["500"]`。在一个 349 步的 episode 里，agent_1 第一次 wait 之后再也没有做过决策。如果没有 `interrupt`，注入给它的消息永远送不到。
→ 接口中新增 `interrupt` 控制（SHOULD）。

**F6　`CrabAgent.message_pipe` 是类级别的字典，跨 episode 不会清空。** 适配器在 `episode.started` 时应当清空它，或者改成按 episode 隔离。

**F7　重新规划没有入口。** 见 §2.2，v1 只观测、不支持。

**F8　ID 与命名。** agent 用 `agent_i` 作为 id，resume 和动作参数中都是这样写的。实体以 PDDL 实体名作为 id，场景描述里使用的是别名（你本地对 `scene_graph/utils.py` 的修改，会同时写出别名和真实 handle）。→ 接口中 `entity_id` 采用 PDDL 名，别名放进 `aliases`。

**F9　输出路径写死且依赖当前目录。** 聊天记录写到 `./chat_history_output/<日期>/<配置>/<消融模式>/<episode>`，episode 日志写到 `./episode_log/<数据集>/<消融模式>/`。不同运行的结果会混在一起（例如 `{split}` 这个目录就是格式化失败后留下的）。→ 录制器和运行脚本统一输出到 `outputs/runs/<run_id>/`。

## 3.1 真实失败案例（v1 监工规则的依据）

轨迹：`EMOS/outputs/runs/20261110_095614_fetch_stretch_man/trace/episode_0.jsonl`（Fetch + Stretch，两个物体，最终失败，跑满 750 步）

| step | 发生了什么 | 哪条 v1 规则能抓到 |
|---|---|---|
| 284 | agent_1（Stretch）对 `any_targets|1` 执行 `pick`，跑满 100 步、以 `timeout` 结束；快照显示 `agent_1.holding = null`。**LLM 并不知道这次 pick 失败了** | 结果推断：`pick` 结束后目标物体不在手里，判为失败 → 注入提示 |
| 308 | agent_1 在手里没有东西的情况下执行 `place(any_targets|1, ...)`，20 步后以 `completed` 结束（空操作） | `precondition`：闸门拒绝 `place`，并注入"你没有拿着 any_targets|1" |
| 328–329 | 两个 agent 都进入 `wait(500)`，此时目标条件 1 尚未满足 | `idle_deadlock`：打断 wait，并注入"目标 1 尚未完成" |
| 750 | 达到最大步数，episode 失败 | — |

同一次运行的 `episode_1.jsonl` 也失败了，失败方式不同：
- agent_1 从头到尾只做了两次 `wait(500)`，没有参与任务；
- agent_0 放置完物体之后执行 `reset_arm`，这个动作从 step 229 一直持续到 750，**始终没有结束**。原因未查，ResetArmSkill 的结束条件可能存在问题，需要单独调查。

对应的监工规则：`idle_deadlock`（一个 agent 一直在等，而任务没有完成），以及"动作持续时间远超同类动作的正常值"。后者 v1 暂不实现，记作候选规则。

第三个案例：`EMOS/outputs/runs/20261110_104009_spot_drone_ep119_v02/trace/episode_119.jsonl`。它和第一条成功的 episode 119 是**同一个 episode**，这次却失败了，说明 LLM 的随机性很大。
- step 0：两个 agent 互相 `send_request`。EMOS 会把 `send_request` 映射成 `wait(500)`，结果双方明明已经收到了对方的回复，还是**各自空等了 500 步**。
- step 664：agent_0 已经拿起物体，再次请求指引，又进入一次 500 步的 wait。到第 750 步超时，物体还在手里。

回放时 `idle_deadlock` 在 step 0 和 664 都能触发并下发 `interrupt`。这是 `interrupt` 必须存在的最直接证据：只发 `inject_message` 的话，消息要等 500 步之后才会被读到。

这个案例说明：**动作结果没有反馈给 LLM**、**前置条件不检查**、**长时间 wait 无法中断**，正是 v1 的三项干预（结果推断加注入消息、闸门、打断）要解决的问题。它也是 mock 适配器的第一个脚本场景。

## 4. 审查后对接口文档的修改

| 修改 | 原因 |
|---|---|
| 新增 `interrupt` 控制（SHOULD） | F5：没有它，`inject_message` 可能送不到 |
| `action.ended` 中拆分 `status` 和 `outcome`，`outcome` 允许为 `unknown`，由 infra 推断 | F2 |
| `request_replan` 降为 MAY | F7：需要改造 EMOS，不应阻塞 v1 联调 |
| 北向工具 v1 只要求接入讨论阶段 | 执行阶段需要修改 `OpenAIModel` |
| 快照只在动作边界和 episode 开始时强制要求 | F3：控制进程间通信开销 |
| `gate` 超时或异常时一律放行 | 保证 infra 的问题不会让 EMOS 崩溃 |
| 实体 `type` 直接使用 EMAS 自己的类型名 | 不同 EMAS 的类型体系不同，v1 不做统一 |

## 5. 只读录制器（已实现）

为了给 mock 提供真实的数据源，已经在 EMOS 中加入一个**只读**录制器，它按本接口文档的事件格式输出 JSONL。实现细节见 [04-emos-trace-recorder.md](04-emos-trace-recorder.md)。
