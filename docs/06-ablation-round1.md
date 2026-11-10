# 小规模 infra 开 / 关对比（第一轮，2026-11-10）

> 场景：Fetch + Stretch 两机协作搬运（`multi_rearrange/llm_fetch_stretch_man`，数据集 `two_agent_manipulation_eval`，按顺序取前 5 个 episode：0、1、2、3、109）
> 设计：同一批 episode，infra 关 / 开各跑 3 次，共 30 个 episode。两组都开启只读录制器，用于统计结果；录制器不改变 EMOS 行为。
> 工具：`EMOS/scripts/run_ablation.sh`（批量运行）、`tools/compare_runs.py`（只读 trace，与具体 EMAS 无关）

## 1. 结论先行

| 轮次 | EMOS 状态 | 关 infra | 开 infra |
|---|---|---|---|
| fs5 | 只修了 E1 | 1/15（7%） | 0/15（0%） |
| **fs5b** | **修了 E1 和 E2** | **3/15（20%）** | **3/15（20%）** |

- **两组成功率相同**：每个 episode 在两组里的结果都一样（ep109 两组都是 3/3，其余 4 个 episode 两组都是 0/3）。按目标条件统计的完成度：关 2.4/4，开 2.6/4，差距在噪声范围内。
- **infra 的开销**：LLM token 多约 10%（执行阶段每个 episode 约 12.3 万 vs 13.5 万），步数基本不变。
- **没有误干预**：干预只出现在确实有动作失败或卡住的 episode（ep0、ep2）。在 ep1、ep3、ep109 这种"一切正常但做得慢"的 episode 里，infra 一次也没有介入。
- 这一轮最大的收获不是 infra 的效果，而是**用 trace 定位到了 EMOS 的第二个底层 bug（E2）**。修复后，基线成功率从 7% 升到 20%。

## 2. E2：`place` 判定"完成"时看错了物体

- `OraclePlacePolicy._is_skill_done` 读取的是 `ObjectToGoalDistanceSensor`，而这个传感器**永远只测 0 号目标**（`task.targ_idx` 固定为 0）。
- 后果是：
  - 0 号物体到位之后，再对其他物体执行 `place`，都会**在第 0 步判完成并直接松手**。物体掉落，PDDL 后置条件也不生效。
  - 第一轮 fs5 中，这样的 `place` 有 21/22 次。
  - 只要先放 0 号物体，第二个物体就永远放不上。
- 修复：在 `HrlDefinedSkillConfig` 中新增 `done_on_settle` 开关，默认 `False`，即原版行为。多智能体技能配置里把它打开。打开后：
  - 判断完成的条件改为：至少运行 10 步，且机械臂关节不再变化；
  - 完成时不再主动松手，由 PDDL 后置条件完成放置。这和原来"超时"那条路径一致，而那条路径一直是能成功的。
- 验证：修复后已经没有"第 0 步就完成"的 `place`；第二个物体可以放到位。

E1（`reset_arm`）的修复见 docs/05 §4。环境端改为每个 episode 重新记录初始姿态；技能端改为"关节停止移动即完成"；超时只结束这个技能，不再结束整个 episode。修复后 `reset_arm` 平均 79 步结束。

## 3. 为什么 infra 没有提升成功率

一次完整的搬运（导航 → 拿起 → 导航 → 放下 → 收臂），平均约 107 + 67 + 107 + 83 + 79 ≈ **440 步**，而每个 episode 只有 **750 步**。一个机器人串行搬两个物体就要 880 步左右，所以这个任务**基本必须两个机器人并行**才能成功。连成功的 ep109 也是在第 747 步左右才完成的。

fs5b 中 infra 开启时失败的 12 个 episode，可以分成两类：

| 类别 | 出现在 | 次数 | 表现 | infra v1 能看到吗 |
|---|---|---|---|---|
| A. 分工串行 | ep1、ep3 | 6/6 | leader 把两个物体都分给 Fetch，Stretch 一直在等待；没有任何动作失败，只是时间不够 | **看不到**。从每个动作来看一切正常；这是任务分配的问题，属于 EMAS 的职责 |
| B. 身体能力失败 | ep0、ep2 | 6/6 | Stretch 的 `pick` 超时（够不到），随后的 `nav_to_obj` 又卡住 | 能看到，但**介入太晚**，见下 |

B 类的时间线（开启 infra，典型例子 ep0 r3）：
- step 285：`outcome_failure` 提醒 Stretch，Stretch 重试；
- step 600：`stalled_action` 打断 Stretch，`help_routing` 通知 Fetch 接手；
- 此时只剩 150 步，不够完成一次搬运。

`help_routing` 默认只在"连续失败 2 次"或"动作卡住"时触发，而每次失败都要花掉 100 步左右，等到触发时已经来不及了。

## 4. 下一步可以改的地方（按投入产出排序）

1. **`help_routing` 更早触发**：动作**第一次**失败，并且有空闲、能力匹配的队友时，就通知队友。这是很小的改动，仍然是通用逻辑。
2. **`nav_to_obj` 的 `max_steps` 收紧**：从 300 改为 200。正常运行最长 184 步，这一轮中卡住的导航最长 319 步。
3. **A 类问题要在讨论阶段解决**：在讨论阶段把世界状态和能力查询工具交给 leader（即验收标准第 3 条）。也可以加一条"负载不均"规则：有一个 agent 负责的目标条件还剩很多，而有能力的队友一直空闲。这条规则会碰到"infra 不替 EMAS 做任务分配"这条边界，需要先讨论。
4. **统计规模**：5 个 episode × 3 次，只能看出"有没有明显差异"。正式的消融实验需要更多 episode，并且要选 infra 能起作用的场景（例如 B 类比例高的场景）。

## 5. 本轮 infra 改动

- 新规则 `stalled_action`：语义表中声明 `max_steps`；动作运行超过这个步数时打断并提示，每个动作只触发一次。
- 新规则 `help_routing`：由其他规则的发现触发（`on_findings`，只往下传一层），通知**空闲**且**有能力**的队友，并打断它当前的空闲动作。
- `idle_deadlock` 新增 `max_alarms_per_state`（默认 2）：同一组未满足条件最多报警 2 次。这是在 Spot+无人机感知任务 ep15 中发现的：那个任务里 `is_detected` 只靠导航无法达成，同样的提醒被重复了 5 次。
- EMOS 侧：开启 infra 时默认每 25 步发一次快照（`habitat_baselines.eval.infra.snapshot_every`），作为 `stalled_action` 判断时间的依据。

## 6. 数据位置

- `EMOS/outputs/runs/20261110_1522*_fs5_*`：第一轮，只修了 E1
- `EMOS/outputs/runs/*_fs5b_*`：第二轮，修了 E1 和 E2
- `EMOS/outputs/runs/20261110_151302_v2_infra_spot_drone_per_probe`：感知任务的试跑
- 复现：
  ```
  scripts/run_ablation.sh fs5b multi_rearrange/llm_fetch_stretch_man 5 3 6
  python embodied-infra/tools/compare_runs.py outputs/runs/*_fs5b_*
  ```
