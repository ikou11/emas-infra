# Embodied Infra v1：接口与契约

> 状态：草案 v0.2（已根据 EMOS 代码审查修订）
> 约定：**MUST** = 必须实现，**SHOULD** = 强烈建议，**MAY** = 可选。可选能力由适配器在注册时声明，infra 按声明降级运行。

## 0. 总览

```
        ┌──────── 北向：Query Tools（infra → agent，只读）────────┐
        │  world.get_entity / world.find_entities /              │
        │  world.get_agent_state / memory.get_action_history      │
        └────────────────────────────────────────────────────────┘
                              Embodied Infra
        ┌──────── 南向：Adapter Contract（EMAS ↔ infra）──────────┐
        │  EMAS → infra : emit(Event)        事件上报（单向）       │
        │  infra → EMAS : gate(...)          动作闸门（同步）       │
        │                 control(Command)   控制命令（异步+回执） │
        └────────────────────────────────────────────────────────┘
              EMOS 适配器 │ Mock 适配器 │（以后）其他 EMAS / 真机
```

v1 中 infra 与适配器运行在**同一个进程**里，通过 Python 接口直接调用。所有数据结构都必须能**无损序列化为 JSON**，这样以后可以直接拆到进程外。

## 1. 共享数据模型

### 1.1 标识

| 概念 | 规则 | EMOS 中的取值 |
|---|---|---|
| `session_id` | 一次运行的唯一 id，由 infra 生成 | — |
| `episode_id` | 字符串，由 EMAS 提供，在同一个 session 内唯一 | 数据集中的 `episode_id`，如 `"119"` |
| `agent_id` | 字符串，在 episode 内唯一，也是 agent 调用动作时用来称呼自己/他人的名字 | `"agent_0"`, `"agent_1"` |
| `embodiment` | 本体类型 | `"SpotRobot"`, `"DJIDrone"`, `"FetchRobot"`, `"StretchRobot"` |
| `entity_id` | 世界实体的 id，**必须与 agent 在动作参数中使用的名字一致** | PDDL 实体名，如 `"any_targets|0"`, `"TARGET_any_targets|0"` |
| `action_id` | 一次动作调用的唯一 id，由适配器生成，格式建议 `"{agent_id}:{seq}"` | EMOS 没有该 id，由适配器生成 |

实体可以有别名（`aliases`），例如场景描述里给出的可读名或物体 handle。infra 内部统一使用 `entity_id`。

### 1.2 时钟

- `step`：仿真步序号（int），在 episode 内单调递增，从 0 开始。**这是 v1 的主时钟**，所有事件都要带上。
- `t_wall`：墙钟时间戳（float，Unix 秒），仅用于调试和性能分析。

### 1.3 信息来源 `source`

每个携带世界信息的事件都要标注来源，供监工按权限过滤：

| 值 | 含义 |
|---|---|
| `sim_gt` | 仿真真值（例如 PDDL 谓词、精确位姿） |
| `agent_report` | agent 自己的陈述或决策（例如 LLM 输出） |
| `observation` | 由机器人传感器观测得到（v1 的 EMOS 中暂时没有） |
| `system` | EMAS 框架自身的元信息（例如注册、episode 开始和结束） |
| `infra` | infra 自己产生的信息（干预、推断出的结果） |

## 2. 事件信封

所有事件共用同一个信封：

```json
{
  "schema": "embodied-infra/event/0.1",
  "seq": 42,
  "session_id": "s-20261110-1",
  "episode_id": "119",
  "step": 137,
  "t_wall": 1793770000.12,
  "type": "action.ended",
  "agent_id": "agent_0",
  "source": "system",
  "data": { }
}
```

- `seq`：发送方生成的序号，在 session 内严格递增。infra 依靠它检测事件丢失或乱序。
- `agent_id`：与具体 agent 无关的事件填 `null`。

## 3. 事件类型（EMAS → infra）

### 3.1 一览

| type | 级别 | source | 触发时机 |
|---|---|---|---|
| `episode.started` | MUST | system | episode 开始，早于该 episode 的其他所有事件 |
| `agent.registered` | MUST | system | 紧接 `episode.started`，每个 agent 一条 |
| `plan.assigned` | SHOULD | agent_report | EMAS 完成任务分配（EMOS：组讨论结束） |
| `action.proposed` | MUST | agent_report | agent 决定了下一个动作，**尚未执行**（闸门在这里生效，见 §4） |
| `action.started` | MUST | system | 动作通过闸门，开始执行 |
| `action.ended` | MUST | system | 动作结束（无论原因） |
| `state.snapshot` | SHOULD | sim_gt / observation | 世界状态快照，触发时机见 §3.5 |
| `agent.message` | MAY | agent_report | agent 之间的消息（infra 只观测，不参与） |
| `llm.usage` | MAY | system | LLM 调用的 token 统计 |
| `episode.ended` | MUST | system | episode 结束，晚于该 episode 的其他所有事件 |

infra 自己会往追踪里写入 `intervention.issued` 和 `control.ack`（见 §5），不需要适配器上报。

### 3.2 `episode.started`

```json
"data": {
  "task_text": "Goal of this episode is ... 0. The object 'any_targets|0' is placed at 'TARGET_any_targets|0'. ...",
  "scene_text": "Here are the descriptions of the scene: ...",
  "goal": {"type": "pddl", "expr": "and(at(any_targets|0,TARGET_any_targets|0),not_holding(agent_0))",
           "conditions": ["at(any_targets|0,TARGET_any_targets|0)", "not_holding(agent_0)"]},
  "entities": [
    {"entity_id": "any_targets|0", "type": "movable_entity_type", "aliases": ["005_tomato_soup_can_:0000"]},
    {"entity_id": "TARGET_any_targets|0", "type": "goal_entity_type", "aliases": []}
  ],
  "meta": {"dataset": "replica_cad", "scene_id": "...", "config_name": "..."}
}
```

- `task_text`：MUST。
- `entities`：MUST，给出本 episode **所有**可在动作参数中引用的实体。它是 `unknown_entity` 规则的依据。`type` 直接使用 EMAS 自己的类型名（EMOS 中是 PDDL 的 `expr_type`，如 `movable_entity_type`、`goal_entity_type`、`robot_entity_type`），infra 不强制统一类型体系。
- `scene_text` 和 `goal`：SHOULD。
- `goal.conditions`：SHOULD。它是一组谓词字符串，**全部成立**即表示任务完成，格式与 `state.snapshot.predicates` 相同。infra 只对它做字符串匹配，不解析 `expr`。`idle_deadlock` 规则依赖它判断任务是否已经完成。

### 3.3 `agent.registered`

```json
"data": {
  "embodiment": "SpotRobot",
  "capabilities": { "mobility": {...}, "perception": {...}, "manipulation": {...} },
  "capability_tags": ["mobility", "perception", "manipulation"],
  "action_space": [
    {"name": "nav_to_obj", "params": {"target_obj": "entity_id"},
     "semantics": {"post": ["pred(robot_at, args.target_obj, agent.id)"]}},
    {"name": "pick", "params": {"target_obj": "entity_id"},
     "semantics": {"requires": ["manipulation"],
                   "pre": ["agent.holding == null"],
                   "post": ["agent.holding == args.target_obj"]}},
    {"name": "place", "params": {"target_obj": "entity_id", "target_location": "entity_id"},
     "semantics": {"requires": ["manipulation"],
                   "pre": ["agent.holding == args.target_obj"],
                   "post": ["agent.holding == null", "pred(at, args.target_obj, args.target_location)"]}},
    {"name": "reset_arm", "params": {}, "semantics": {"requires": ["manipulation"]}},
    {"name": "wait", "params": {}, "semantics": {"idle": true}},
    {"name": "send_request", "params": {"request": "text", "target_agent": "agent_id"}}
  ],
  "supports": ["gate", "inject_message", "interrupt"]
}
```

- `capabilities`：MUST，内容可以是任意 JSON。EMOS 中直接使用 robot resume。
- `capability_tags`：SHOULD，能力标签列表，由适配器给出，infra 不会从 `capabilities` 里去猜。例如 EMOS 中无人机的 resume 也有 `manipulation` 键，只是工作空间为空。
- `action_space`：MUST。参数类型 `entity_id` 告诉 infra 哪些参数应该在实体表里做校验。
- `action_space[].semantics`：**MUST**，即**动作语义表**，见 §3.3.1。允许为空对象（`{}`），表示 infra 对这个动作不做任何检查。

#### 3.3.1 动作语义表

动作语义由**适配器**提供，infra 只负责**通用地解释执行**，不写死任何 EMAS 的动作名。同一张表有三个用途：

| 字段 | 类型 | 用途 |
|---|---|---|
| `requires` | 能力标签列表 | `capability` 规则：agent 的 `capability_tags` 必须包含这里列出的全部标签 |
| `pre` | 条件列表（与） | `precondition` 规则：在 `action.proposed` 时，对照当前世界状态检查 |
| `post` | 条件列表（与） | 结果推断：在 `action.ended` 之后的**下一条快照**上检查。全部成立判为 `success`，否则判为 `failure`。没有 `post` 时判为 `n/a` |
| `idle` | bool | 标记这是一个"空闲"动作，供 `idle_deadlock` 规则使用 |
| `max_steps` | int，MAY | `stalled_action` 规则：动作运行超过这么多步仍未结束，就判为卡住。`idle` 动作不检查。需要适配器有一个时间基准，即任何带 `step` 的事件都行；建议定期发 `state.snapshot`（EMOS 中开启 infra 时默认每 25 步一次） |

条件的语法很小，没有 `eval`：

```
cond   := term ("==" | "!=") term
        | "pred(" NAME { "," term } ")"          # 世界状态中该谓词当前为真
        | "not " cond
term   := "null" | "agent.id" | "agent.holding" | "args." NAME | "'" 字面量 "'"
```

- `agent` 指发起动作的 agent；`args.X` 取动作参数 `X` 的值。
- `pred(...)` 会被展开成谓词字符串（例如 `at(any_targets|0,TARGET_any_targets|0)`），然后在最新快照的 `predicates` 中做精确匹配。
- 条件引用的信息缺失时（例如快照里没有 `predicates`），结果为 `unknown`，不会触发规则。
- `supports`：MUST，声明该 agent 支持哪些控制能力（见 §5），可以为空数组。

### 3.4 `action.*`

```json
// action.proposed
"data": {"action_id": "agent_0:3", "name": "pick", "args": {"target_obj": "any_targets|0"},
         "raw": "<LLM 原始输出，MAY>", "decision_latency_s": 2.4}

// action.started
"data": {"action_id": "agent_0:3", "name": "pick", "args": {"target_obj": "any_targets|0"},
         "gate": "allow"}          // allow | replaced | (deny 不会产生 started)

// action.ended
"data": {"action_id": "agent_0:3", "name": "pick",
         "status": "completed",     // completed | timeout | interrupted | rejected | error
         "steps": 41,
         "outcome": "unknown",      // success | failure | unknown（MAY，适配器给不出时填 unknown）
         "detail": "skill terminated: is_holding=1"}
```

约束：
- 对同一个 `action_id`，事件必须按 `proposed → started → ended` 的顺序出现。被闸门拒绝时，没有 `started`，直接以 `ended(status=rejected)` 结束。
- 同一个 agent 在同一时刻最多只有一个进行中的动作。
- `status` 描述的是**执行层面**发生了什么；`outcome` 描述的是**语义上**是否达成目的。EMOS 的 oracle 技能只给得出 `status`（例如 pick 的结束条件是"手里拿着某个东西"，不保证拿的是目标物体）。因此 `outcome` 允许为 `unknown`，由 infra 根据动作结束后的状态快照**推断**结果，推断结果记录为 `infra` 来源的 `action.outcome_inferred` 追踪记录。

### 3.5 `state.snapshot`

```json
"source": "sim_gt",
"data": {
  "reason": "action_boundary",          // action_boundary | periodic | episode_start | episode_end
  "agents": {
    "agent_0": {"pos": [x, y, z], "yaw": 1.57, "holding": "any_targets|0"},
    "agent_1": {"pos": [x, y, z], "yaw": 0.0,  "holding": null}
  },
  "entities": {
    "any_targets|0": {"pos": [x, y, z]},
    "TARGET_any_targets|0": {"pos": [x, y, z]}
  },
  "predicates": ["holding(any_targets|0,agent_0)", "robot_at(any_targets|0,agent_0)"]
}
```

- 坐标系：直接使用仿真世界坐标，`[x, y, z]` 中 **y 轴朝上**（与 EMOS 场景描述一致），单位为米。
- 触发时机：适配器 MUST 在 `episode.started` 之后和每个 `action.ended` 之后各发一次快照；MAY 每隔 N 步额外发一次（N 可配置，默认不发）。
- `predicates`：MAY，取值为当前为真的谓词，格式是 `name(arg1,arg2,...)`。

### 3.6 `episode.ended`

```json
"data": {"success": true, "steps": 349, "metrics": {"pddl_success": 1.0, "num_steps": 349, "...": 0}}
```

## 4. 同步闸门：`gate`

**这是 v1 中唯一会阻塞 EMAS 主循环的调用。**

```python
class GateDecision(TypedDict):
    decision: Literal["allow", "deny", "replace"]
    reason: str                     # deny/replace 时必填，便于追踪
    replacement: Optional[dict]     # replace 时必填，格式为 {"name": ..., "args": {...}}
    message: Optional[str]          # MAY：附带一条要注入给该 agent 的提示

def gate(event: ActionProposedEvent) -> GateDecision   # infra 实现，适配器调用
```

- 适配器 MUST 在发出 `action.proposed` 之后、执行动作之前调用 `gate`；只有在 `supports` 里声明了 `gate` 的 agent 才需要这样做。
- **超时**：默认 2 秒。超时或 infra 抛出异常时，适配器 MUST 按 `allow` 处理，**infra 的故障绝不能导致 EMAS 崩溃**。
- `deny` 的语义由适配器决定，但必须是"安全且无副作用"的行为。EMOS 中的做法：把动作替换为短时间的 `wait`，并把 `reason/message` 作为提示，在该 agent 下一次决策时注入。
- infra 关闭时，适配器不调用 `gate`，行为与原版一致。

## 5. 异步控制：`control`

```python
class Command(TypedDict):
    command_id: str
    type: Literal["inject_message", "interrupt", "request_replan"]
    agent_id: Optional[str]         # 为 None 表示面向整个 EMAS（例如全局重新规划）
    payload: dict

class ControlAck(TypedDict):
    command_id: str
    status: Literal["accepted", "applied", "ignored", "unsupported", "failed"]
    step: int
    detail: str

def control(cmd: Command) -> ControlAck     # 适配器实现，infra 调用；必须立即返回，不能阻塞
```

| type | 级别 | payload | 语义 |
|---|---|---|---|
| `inject_message` | SHOULD | `{"text": str, "from": "supervisor"}` | 在该 agent **下一次决策时**把文本加入它的输入 |
| `interrupt` | SHOULD | `{"reason": str}` | 终止该 agent 当前的动作（产生 `action.ended(status=interrupted)`），让它立刻进入下一次决策。通常与 `inject_message` 配合使用 |
| `request_replan` | MAY（v1 不强制） | `{"reason": str, "evidence": [...]}` | 请求 EMAS 重新做任务分配；由 EMAS 自己决定交给 leader 还是各个 agent |

回执：
- `control()` 立即返回 `accepted`（已入队）、`unsupported` 或 `failed`。
- 命令真正生效时，适配器再通过事件上报一条 `control.ack`，`status` 为 `applied` 或 `ignored`，`data` 字段与 `ControlAck` 相同。例如 `inject_message` 在 agent 下一次决策、消息被真正拼进 prompt 时才算 `applied`。
- 一个 episode 结束时，所有未生效的命令都作废，记为 `ignored`。

为什么需要 `interrupt`：EMOS 中 agent 执行 `wait` 会一次等待 500 步，期间不会再做决策（审查 F5）。如果没有打断，注入的消息可能要等上几百步才能被看到。

## 6. 北向查询工具（infra → agent）

infra 以 **OpenAI function-calling 的 JSON schema** 形式提供工具定义，外加一个统一的调用入口：

```python
def tool_schemas(agent_id: str) -> list[dict]           # 返回可以直接放进 tools=[...] 的定义
def call_tool(agent_id: str, name: str, args: dict) -> str   # 返回 JSON 字符串，供 LLM 阅读
```

v1 工具集（全部只读）：

| 工具 | 参数 | 返回 |
|---|---|---|
| `world_get_entity` | `entity_id` | 最新位置、`holder`（被谁拿着）、最后更新的 step、来源 |
| `world_find_entities` | `type?`, `name_contains?`, `near_agent?`, `radius?` | 实体列表 |
| `world_get_agent_state` | `agent_id` | 位置、手持物、当前动作 |
| `memory_get_action_history` | `agent_id?`, `entity_id?`, `last_n=10` | 动作记录（名称、参数、status、推断出的 outcome、步数） |

约束：
- 工具名只能使用 `[a-zA-Z0-9_]`，因为 OpenAI 的 function name 不允许出现 `.`。
- 每次调用都会写入追踪（记为 `infra` 来源的 `tool.called`）。
- 查询结果只基于 infra 当前持有的世界状态，**不会**反向触发 EMAS 的任何行为。

## 7. 适配器 Python 接口（v1）

```python
class EmasAdapter(ABC):
    """EMAS 侧实现。infra 通过它下发控制。"""
    @abstractmethod
    def control(self, cmd: Command) -> ControlAck: ...

class InfraClient(ABC):
    """infra 侧实现。适配器通过它上报事件、调用闸门、获取工具。"""
    def attach(self, adapter: EmasAdapter) -> None: ...
    def emit(self, event: dict) -> None: ...                     # 必须非阻塞，不能抛异常
    def gate(self, event: dict, timeout_s: float = 2.0) -> GateDecision: ...
    def tool_schemas(self, agent_id: str) -> list[dict]: ...
    def call_tool(self, agent_id: str, name: str, args: dict) -> str: ...

class NullInfraClient(InfraClient):
    """infra 关闭时使用：emit 什么都不做，gate 永远返回 allow，tool_schemas 返回 []。"""
```

EMAS 侧只依赖 `InfraClient` 这个抽象。infra 关闭时注入 `NullInfraClient`，从而保证回归一致。

## 8. 配置（v1）

```yaml
infra:
  enabled: false
  session_dir: outputs/runs/${run_id}/infra
  trace:
    enabled: true
  world_model:
    enabled: true
    allowed_sources: [sim_gt, agent_report, system]   # 以后切换为只看观测时修改这里
  tools:
    enabled: true
    expose_in: [discussion]                           # discussion | execution（execution 需要 EMOS 改造）
  supervisor:
    enabled: true
    rules: [unknown_entity, unknown_action, precondition, capability, repeat_failure, idle_deadlock]
    repeat_failure_n: 2
    idle_deadlock_decisions: 2
  interventions:
    allowed: [gate_deny, gate_replace, inject_message, interrupt]
    gate_timeout_s: 2.0
```

各级开关之间的关系：
- `enabled=false`：其余配置全部忽略，使用 `NullInfraClient`。
- 关闭 `supervisor`：只观测和记录，不做任何干预。
- 可以只关闭 `tools`，单独评估记忆查询的作用。

## 9. 版本与兼容

- 事件信封中的 `schema` 字段标明版本。新增事件类型和字段属于兼容变更；修改或删除已有字段需要升级主版本。
- infra 必须忽略不认识的事件类型和字段，不能报错。
