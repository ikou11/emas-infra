# EMOS 闭环联调（第一轮，2026-11-10）

> EMOS 分支：`infra-integration`（基于上游 `embodied_mas`）
> 开关：`habitat_baselines.eval.infra.enabled=True`，或运行脚本时设置 `EMOS_INFRA=1`；默认关闭

## 1. 实现方式

EMOS 侧没有另写一套上报逻辑，而是复用只读录制器的全部钩子。录制器多了两个"出口"：

```
EMOS hooks ──► TraceRecorder ──► InfraClient.emit()        （infra 开启时，轨迹由 infra 写）
                   │       └──► JSONL 文件                  （只开录制器时）
                   └── gate() ──► InfraClient.gate()
infra ──control()──► EmosInfraAdapter ──► CrabAgent.message_pipe / 打断标记
```

| 通路 | 实现位置 | 行为 |
|---|---|---|
| 闸门 | `LLMHighLevelPolicy.get_next_skill` | LLM 给出动作 → 上报 `action.proposed` → 调用 `gate()`。被拒绝时，把拒绝原因**当作下一条输入，立刻交给同一个 LLM 重新决策**，最多重试 `gate_retries=2` 次，不浪费仿真步。全部被拒绝后，回退为 `wait(deny_wait_steps=20)` |
| 注入消息 | `EmosInfraAdapter.control` → `CrabAgent.message_pipe` | agent 下一次调用 LLM 时，消息会拼进 prompt。读取的那一刻回执 `applied` |
| 打断 | `LLMHighLevelPolicy.get_termination`（EMOS 原有的钩子） | 返回 True，当前技能立即结束，agent 进入下一次决策；动作以 `status=interrupted` 结束 |
| 重新规划 | — | v1 不支持，回执 `unsupported` |
| episode 开始 | `adapter.reset()` | 清空 `CrabAgent.message_pipe`，修复审查 F6 中"消息跨 episode 泄漏"的问题，仅在 infra 开启时生效 |

配置项：`habitat_baselines.eval.infra.{enabled, config_file, gate_retries, deny_wait_steps}`。infra 自身的规则和干预配置默认使用 `habitat_mas/infra_adapter/infra_default.yaml`。

## 2. 验证

| 项目 | 结果 |
|---|---|
| 回归：infra 和录制器都关闭 | `20261110_112550_regression_trace_off`：ep119 成功，349 步，没有生成 trace 目录，日志里没有 infra 相关输出。LLM 有随机性，无法逐步比对，但走的是原始代码路径 |
| EMOS 集成测试 | `habitat_mas/test/test_infra_adapter.py`，7 项全部通过：拒绝后当场重试、全部拒绝后回退、合法动作只调用一次 LLM、打断只生效一次、没有运行中的动作时打断被忽略、注入回执、关闭 infra 时映射与原版一致 |
| infra 单元测试 | 30 项全部通过 |
| 闭环轨迹契约校验 | 3 条全部 OK |

## 3. 闭环运行结果（每个 episode 只跑了 1 次，属于个例，不是统计结论）

| 运行 | 结果 | infra 做了什么 |
|---|---|---|
| ep119（Spot+无人机），`20261110_112932_infra_spot_drone_ep119` | 成功，349 步 | 在线推断出 4 次动作结果，全部为 success；**没有干预**。无人机在等待时 Spot 一直在工作，不该干预，也确实没有干预 |
| Fetch+Stretch ep0，`20261110_112929_infra_fetch_stretch` | 失败，750 步 | step 285：推断出 agent_1 的 `pick` 失败，注入提示，同一步被 LLM 读到。agent_1 **改为重试 pick，没有像基线那样去做空放置**。step 385：第二次 pick 仍失败，触发 `repeat_failure`；随后 agent_1 的 `nav_to_obj` 一直卡到 750 步 |
| Fetch+Stretch ep1，同一次运行 | 失败，329 步 | 无干预。agent_0 的 `reset_arm` 超时，导致 **episode 被强制终止**（见 §4） |

结论：
- **链路是通的**：事件上报 → 结果推断 → 注入 → LLM 读到并改变决策，这一整条在真实运行中都发生了。闸门拒绝和打断两条通路，在这三次运行中 LLM 都没有触发对应的条件，已由集成测试覆盖。
- **"提醒犯错的 agent 本人"还不够**。ep0 的根本原因是 Stretch 够不到 `any_targets|1`，而 Fetch 从 step 328 起就一直空闲。v1 只会提醒 agent_1，不会去找能帮忙的队友。

## 4. 新发现的 EMOS 问题（infra 关闭时同样存在）

**E1　`reset_arm` 超时会直接结束整个 episode。**
- `oracle_skills_multi_agent.yaml` 中，`reset_arm` 没有设置 `force_end_on_timeout`，因此使用默认值 `True`（`default_structured_configs.py:266`）。
- 技能运行超过 100 步就会触发 `bad_terminate`，整个 episode 判为失败。ep1 中，agent_0 的 `reset_arm` 从 step 229 开始，episode 恰好在 step 329 结束。
- 再加上 `ResetArmSkill` 本身的问题：`_init_joint_pos` 只在这个技能对象**第一次被调用时**记录一次，之后从不重置；而"完成"的判定是关节回到这个姿态，这往往做不到。

修复方式（待你决定，因为会改变 EMOS 的基线行为）：给 `reset_arm` 加上 `force_end_on_timeout: False`，并在 `on_enter` 中重新记录初始姿态。

## 5. 下一步

1. **新规则 `stalled_action`**：动作持续时间远超同类动作的正常值时，打断并提示。需要适配器定期发快照（`trace.snapshot_every`），或者在语义表中声明 `max_steps`。对应 ep0 的 `nav_to_obj` 卡住 365 步，以及 `reset_arm` 卡住的情况。
2. **求助路由**：`repeat_failure` 或 `capability` 触发时，除了提醒本人，再向**空闲且具备所需能力**的队友注入一条提示。仍然是通用逻辑，依据是 `capability_tags` 和语义表中的 `requires`。这不是替 EMAS 做规划，只是把"谁能帮上忙"的信息送到对方手里。
3. **决定 E1 是否修复**，然后对同一批 episode 各跑 3 次，做一个小规模的开启/关闭 infra 对比。
4. **讨论阶段接入查询工具**（验收标准第 3 条）。
