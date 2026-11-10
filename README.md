# Embodied Infra

这是给具身多 agent 系统（EMAS，例如 EMOS）用的运行时基础设施，负责事件追踪、世界状态与记忆、规则监工和干预。目前是 v1 原型，目标是先和 EMOS 在仿真里联调跑通，同时**不和 EMOS 绑死**：infra 里不出现任何 EMAS 的动作名，所有和具体系统相关的知识都由适配器通过"动作语义表"提供。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/01-v1-scope.md](docs/01-v1-scope.md) | v1 职责、不做什么、监工规则、验收标准 |
| [docs/02-v1-interface-spec.md](docs/02-v1-interface-spec.md) | 接口与契约：数据模型、事件、动作语义表、闸门、异步控制、查询工具、配置 |
| [docs/03-emos-capability-audit.md](docs/03-emos-capability-audit.md) | EMOS 能力审查，以及真实失败案例 |
| [docs/04-emos-trace-recorder.md](docs/04-emos-trace-recorder.md) | EMOS 只读录制器和运行目录约定 |
| [docs/05-emos-closed-loop.md](docs/05-emos-closed-loop.md) | EMOS 闭环联调：实现方式、验证结果、第一轮运行结果、新发现的 EMOS 问题 |
| [docs/06-ablation-round1.md](docs/06-ablation-round1.md) | 第一轮开启/关闭 infra 对比：结果、E2、失败归类、下一步 |

## 代码结构

```
embodied_infra/
├── events.py            事件信封（pydantic），事件类型常量
├── control.py           GateDecision / Command / ControlAck
├── client.py            InfraClient（infra 侧）、EmasAdapter（EMAS 侧）、NullInfraClient
├── runtime.py           InfraRuntime：同步处理流水线（追踪 → 世界状态 → 监工 → 干预）
├── bus.py               进程内同步事件总线，订阅者之间的异常相互隔离
├── trace.py             JSONL 追踪：episode_<id>.jsonl（输入）+ episode_<id>.infra.jsonl（infra 产出）
├── config.py            InfraConfig，以及各模块的开关
├── semantics.py         动作语义表的条件语言（三值逻辑，不使用 eval）
├── tools.py             北向只读查询工具（OpenAI function schema）
├── world/               事件溯源的内存世界状态（WorldStore 接口 + InMemoryWorldStore）
├── supervisor/          结果推断、7 条规则、干预策略（冷却和预算限制）
└── adapters/replay.py   回放式 mock 适配器（开环），附带命令行
examples/emos/profile.yaml   EMOS 的动作语义表和能力标签（给旧轨迹补全字段用）
tests/                       单元测试，以及基于 4 条真实 EMOS 轨迹的黄金测试
tools/validate_trace.py      按 v1 契约校验轨迹
```

## 使用

环境要求：Python ≥ 3.9，依赖只有 pydantic 2 和 PyYAML（EMOS 的 `emos-repro` 环境里都已具备）。

```bash
# 运行测试（只用标准库 unittest，不需要 pytest）
python -m unittest discover -s tests -t tests

# 把一条录制好的轨迹回放进 infra，查看监工会在哪些地方介入
python -m embodied_infra.adapters.replay tests/fixtures/traces/emos_fetch_stretch_ep0_fail.jsonl \
    --profile examples/emos/profile.yaml
```

在 EMAS 侧接入（v1，进程内）：

```python
from embodied_infra import make_client
infra = make_client(cfg)          # cfg.enabled=False 时得到 NullInfraClient，EMAS 行为不变
infra.attach(my_adapter)          # my_adapter 实现 EmasAdapter.control()
infra.emit(event)                 # 上报事件
decision = infra.gate(proposed)   # 动作执行前询问闸门：allow / deny / replace
```

## 进度

- [x] v1 职责与接口（含动作语义表）
- [x] EMOS 可行性审查
- [x] EMOS 只读录制器，以及 4 条真实轨迹（已复制到 `tests/fixtures/traces/`；其中 1 条成功、3 条分别以不同方式失败）
- [x] 技术选型：进程内运行、同步总线、事件溯源加内存世界状态、动作语义表、规则监工
- [x] infra 骨架、回放式 mock、36 个测试（包括 4 条真实轨迹上的黄金测试）
- [x] EMOS 适配器：闸门、注入消息、打断（EMOS 分支 `infra-integration`），闭环跑通，并有 7 项集成测试
- [x] `stalled_action`、`help_routing` 规则；`idle_deadlock` 限制重复报警
- [x] 修复 EMOS 底层问题 E1（`reset_arm`）和 E2（`place` 判断完成时看错物体）
- [x] 小规模开启/关闭 infra 对比：两组成功率相同（各 3/15），详见 docs/06
- [ ] `help_routing` 在第一次失败时就触发；收紧 `nav_to_obj` 的 `max_steps`（docs/06 §4）
- [ ] 讨论阶段的查询工具接入 EMOS
- [ ] 脚本式 mock（真实闭环已经可以跑，优先级降低）

已知缺口：任务分配本身导致的失败（一个 agent 串行做完所有事，最后超时）infra v1 看不到，见 docs/06 §3。
