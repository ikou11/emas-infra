# 监工：规则 + LLM，以及补足层（v1.2，2026-11-10）

## 1. 分层与开关

```
安全层      unknown_action, unknown_entity          只要 infra 开启就生效，不受监工开关影响
            （执行这类动作会让 EMAS 崩溃；放在开关之外，开 / 关监工的对比才不会被崩溃干扰）
───────────────────── supervisor.enabled ─────────────────────（一个总开关：规则和 LLM 一起开关）
纠错层      capability, precondition, outcome_failure, repeat_failure, idle_deadlock,
            stalled_action, repeated_denial, stagnation, help_routing
补足层      assist.first_failure_help   第一次失败就把求助信息路由给队友
（逐项开关）assist.workload_balance     workload_risk：时间不够时，建议空闲队友接手
            assist.plan_review          LLM 在开局复核 EMAS 的分工
LLM         supervisor.llm.*            规则决定"什么时候"，LLM 决定"做什么"
```

- 纠错层只指出"这一步错了"；补足层会提出"谁去做什么"，等于在弥补 EMAS 分工能力的不足，所以单独开关。
- 库里的默认值是：LLM 关闭，补足层全关。EMOS 的 `infra_default.yaml` 中全部打开。

## 2. 规则和 LLM 各管什么

原则：
- **规则**负责三类事：能用语义表和世界状态直接判定的；对时间敏感的（闸门、打断）；模板提示就足够的。
- **LLM** 只在规则发现了异常、而"下一步怎么办"需要推理时才被调用，不按固定时间巡检。所以 LLM 的调用次数和异常次数成正比，正常的 episode 基本不调用 LLM。

| 场景 | 规则 | LLM |
|---|---|---|
| 闸门：未知动作 / 实体、能力不符、前置条件不满足 | 同步拒绝，模板提示直接反馈给 EMAS 的 LLM | 不进入同步路径 |
| 同一 agent 连续 3 次被闸门拒绝（`repeated_denial`） | 检测 | 异步升级：写一条更有针对性的提示 |
| 动作第一次失败（`outcome_failure`） | 用模板注入提示；打开 `first_failure_help` 时，同时通知空闲且有能力的队友 | 不介入 |
| 连续失败、动作卡住、全员空闲、进度停滞 | 检测 | 诊断原因，决定通知谁、说什么、是否打断 |
| 时间不够（`workload_risk`，补足层） | 定量检测，并给出候选接手人 | 给出具体的分工建议 |
| 开局分工（`plan_review`，补足层） | — | 每个 episode 调用一次，只给建议，不打断 |

**兜底**：LLM 不可用、超时、出错、没有返回可用的工具调用、预算用完或者还在最小间隔内，都退回规则模板。所以 LLM 只会让干预更好，不会让监工失效。每次 LLM 决策（包括退回模板的那些）都写入 infra trace，事件类型是 `supervisor.llm_decision`，记录调用模式、用的是 LLM 还是兜底、原因、延迟、token 用量和工具调用。

**成本控制**（`supervisor.llm`）：
- 每个 episode 最多调用 8 次；
- 两次升级之间至少隔 30 步（开局复核不受这个限制）；
- 超时 60 秒。

**仿真中同步调用**：调用 LLM 时仿真暂停，所以 LLM 的延迟不会占用仿真步数。接真机时需要改为异步，命令会带上发出时的步数。

## 3. LLM 能看到什么、能做什么

- 上下文（JSON）包括：
  - 当前步数和步数上限；
  - 任务文本；
  - 每个目标条件是否已经达成；
  - 每个 agent 的身体类型、能力标签、手里拿着什么、子任务、当前动作和已经运行的步数、最近 6 个动作；
  - `known_failures`（来自记忆）：谁在什么物体上做什么失败过；
  - 本次触发的问题；
  - 规则默认会怎么干预；
  - 最近 5 次干预。
- 可以调用的工具：`inject_message`、`interrupt`、`use_default`、`no_intervention`。
- **v1 有上帝视角**：快照来自仿真真值。prompt 要求它只指出问题、提出建议，不透露 EMAS 本来应该自己发现的信息，例如物体的具体位置。以后可以用 `world_model.allowed_sources` 收紧。
- 凭据放在单独的 `embodied-infra/.env` 里（`SUPERVISOR_LLM_API_KEY / _BASE_URL / _MODEL`，模板见 `.env.example`），和 EMAS 自己用的 LLM 分开配置。当前配置为 DeepSeek `deepseek-flash`。
- `tool_choice` 默认是 `auto`：`deepseek-flash` 的思考模式不支持 `required`。`max_tokens` 设为 4000：思考过程也要占用 token；设成 2000 时，复杂场景下会出现 `finish_reason=length`，即没来得及调用工具就被截断。

## 4. 新规则

| 规则 | 层 | 触发条件 | 依据 |
|---|---|---|---|
| `repeated_denial` | 纠错 | 同一 agent 连续 n 次（默认 3 次）被闸门拒绝，每轮只报一次 | 动作记录 |
| `stagnation` | 纠错 | 目标条件 `window` 步内（默认 500）没有任何新进展 | 快照 + 目标条件 |
| `workload_risk` | 补足 | 正在干活的 agent 预计剩余工作量超过剩余步数，同时有空闲、能力匹配的队友 | 见下 |

`workload_risk` 是通用的估算方法，不和 EMOS 绑定：
- **条件归属**：一个目标条件归属于最近一个对它所涉及实体做过动作的 agent；没人碰过的条件，由正在忙的 agent 平摊。
- **每个条件的成本**：用本 episode 中已达成条件的平均耗时估算，开局就已成立的条件不算在内；还没有任何条件达成时，用先验值 `prior_steps_per_condition`。
- **剩余时间**：需要适配器在 `episode.started` 里给出 `max_steps`（MAY 字段），EMOS 填的是 750。
- **接手人**：
  - 必须空闲（所有 agent 都开始过动作之后，才判断谁空闲）；
  - 必须有能达成该谓词的动作，即语义表中 `post` 含 `pred(<谓词>, …)` 的动作，并且能力标签覆盖这个动作的 `requires`；
  - 在这些实体上没有失败过（来自记忆）。
- **两边都通知**：建议接手人去做；也告诉原负责人"X 准备接手 B"，但**不打断**原负责人。

其他调整：
- `nav_to_obj` 的 `max_steps` 从 300 改为 200；
- `help_routing` 选接手人时，排除在这件事上失败过的 agent。

## 5. 开环回放验证（真实 DeepSeek，基于 fs5b 中关闭 infra 的失败轨迹）

```
python -m embodied_infra.adapters.replay <trace> --profile examples/emos/profile.yaml \
    --config ../EMOS/habitat-mas/habitat_mas/infra_adapter/infra_default.yaml --llm
```

| episode | 失败类型 | 监工做了什么 |
|---|---|---|
| ep1 | 分工串行 | 开局复核**发现两个物体都分给了 Fetch**，分别建议 agent_0 只做 0 号物体、agent_1 并行做 1 号物体。原轨迹中 Stretch 一直在等待 |
| ep0 | Stretch 够不到物体 | 开局复核判断分工合理，不干预。step 285：第一次失败，用模板提示。step 395：`workload_risk` 升级给 LLM，LLM 让 Fetch 接手，并告诉 Stretch 不要再试。step 459：全员空闲，同样的结论。两次独立回放，决策一致 |
| ep2 | Stretch 够不到物体 | step 538：全员空闲，LLM 根据失败历史让 Fetch 接手 0 号物体，并让 Stretch 让开 |

单次 LLM 调用耗时约 3–16 秒，每次约 2–3k 个 prompt token。每个 episode 调用 1–3 次。

改进前发现并修复的问题：
- 思考模式不支持 `tool_choice=required`；
- `max_tokens` 太小，导致回复被截断；
- 消息前缀重复；
- 开局复核和第一次升级在同一步时，最小间隔把升级挡掉了；
- 有 agent 还没开始动作时就被当作空闲，导致误报；
- LLM 曾让已经失败过的 agent 和接手的 agent 去处理同一个物体。修复方式是在上下文中加入 `known_failures`，并在 prompt 里约束。

### 5.1 闭环中发现的关键问题：监工推翻了 EMAS 正确的分工

第一次闭环运行（`20261110_170227_v3_llm_supervisor_fs`）的 ep1 中：
- EMOS 的 Stretch 在讨论阶段就推理出"1 号物体高 1.7，超出我的可达高度 1.24"，于是把两件活都交给了 Fetch。这个"串行分工"**是正确的**。
- 但监工只知道粗粒度的能力标签，开局复核和第 321 步的升级都要求 Stretch 去拿这个物体，结果白白卡了 203 步。

修复：
1. 给监工 LLM 和 EMAS 同样的信息：场景描述（`episode.started.scene_text`，包括物体高度等）、各机器人的原始能力参数（例如手臂工作空间，长段说明文字已去掉）、agent 之间的消息（`agent_messages`）。
2. 在 prompt 中要求：先核实物理上是否可行；尊重 agent 自己说明过的限制；不均匀的分工可能是有意的。
3. 加入 `supervisor_notes`：监工自己之前的决策，包括不干预的理由。后续的决策要和开局复核保持一致。
4. 加入 `require_confirmation`：`workload_risk` 中**只靠时间估算**得出的接手建议，必须由 LLM 确认才发出；LLM 不可用时直接丢弃，不退回模板。有"负责人失败过"这条证据的建议不受这个限制。
5. `workload_risk` 用上记忆：负责人在这件事上失败过（`blocked_owner`），有空闲、有能力的队友时**立即**建议接手，不必等到时间不够。
6. `max_tokens` 改为 8000、超时改为 90 秒：开局复核偶尔推理过长，在 4000 时被截断过。

修复后，在 ep1 上回放 3 次，开局复核和升级每次都正确判断"Stretch 够不着，分工不均是合理的"，选择不干预。

## 6. 闭环冒烟测试（`20261110_172037_v3b_llm_supervisor_fs`，Fetch+Stretch ep0、ep1）

| episode | 结果 | 监工做了什么 | 对照（同 episode，不开监工） |
|---|---|---|---|
| ep1 | 失败（750 步） | 开局复核和第 0 步的 `workload_risk` 升级都判断"Stretch 够不着两个物体"，选择不干预。行为和基线一致，**没有帮倒忙** | 同样失败：只有 Fetch 能拿到这两个物体，一个机器人串行做，750 步来不及 |
| ep0 | 失败（750 步） | step 285：第一次失败，用模板提示。step 317：`workload_risk`（负责人失败过）升级给 LLM，LLM 让 Fetch 接手、Stretch 停下。Fetch 在第 667 步开始放置，差几步没完成 | 关 infra 的 3 次运行中，1 号物体一直没有人搬 |

- LLM 调用：每个 episode 2 次，每次 5–14 秒，仿真在调用期间暂停。
- 结论：链路是通的，监工的决策方向正确，也没有误伤正确的分工。但这两个 episode 在 750 步内是否可解，受限于机器人的身体能力。监工把 1 号物体的接手时间从第 500 步提前到了第 317 步，仍然差一点。

**仍然存在的风险**：LLM 有随机性。上下文变复杂时，偶尔会推理过长或判断出错。兜底逻辑和 `require_confirmation` 能把损失控制在"少干预"这一侧。
