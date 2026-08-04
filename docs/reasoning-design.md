# Reasoning Strategy Comparison Design

- 状态：实施中（Run 3；2026-07-31）
- 范围：ReAct 与 Plan-and-Execute 两种推理策略的确定性实现，以及在固定 task 集上的结构对比
- 非目标：不表述模型推理能力或质量分数；不评测真实 LLM 策略效果（需要真实 Provider 与另行授权的评测）；不做并发/生产结论

## 决策

1. 两种策略共用注入式接口：`Reasoner.decide()`（ReAct 每步决策）、`Planner.plan()/replan()`（Plan-and-Execute 计划与重规划）、`ToolExecutor`（脚本化工具行为）。
2. 测试与 demo 全部使用确定性实现（`DeterministicReasoner`、`DeterministicPlanner`、`DeterministicStepExecutor`），连续两次运行逐字节一致；生产可替换为真实模型后端，但效果评测属于另行授权的范围。
3. 对比指标只取结构量：步骤数、工具调用数、重规划次数、成功与否；不合成任何质量得分。
4. ReAct：观察 → 决策 → 工具 → 观察的循环，直到 finish 或 `max_steps` 上限。
5. Plan-and-Execute：先输出完整计划再逐步执行；工具失败时触发 `replan`（有 `max_replans` 上限），失败超过上限则本轮失败。
6. 工具错误（`ToolError`）被策略层捕获为观察结果，不会导致进程崩溃；不构造网络/外部副作用。

## 验证

- 测试覆盖：ReAct 工具序列与 finish、Plan-and-Execute 计划执行、工具失败触发 replan、ReAct 对工具错误的容忍、max_steps 上限、对比确定性、渲染表、demo 可复现、Action 校验。
- demo 使用固定任务、固定后端与固定工具，连续两次运行逐字节一致。
- 默认离线：无 socket/HTTP、无真实 Provider、无密钥。

## 后续

- 若用户授权真实模型评测，可在同一 harness 上替换后端并单独记录评测边界；本版不包含该结论。
