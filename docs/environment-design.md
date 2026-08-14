# Agent Interaction Environment Design

- 状态：已实施；v1.5 受控记忆集成已验证（2026-08-14）
- 范围：Agent 交互环境门面——工具注册/执行、会话上下文组装、策略循环驱动
- 非目标：浏览器/多进程环境；沙箱安全边界（本版为单进程注入式环境）；生产部署；不表述模型能力

## 决策

1. `AgentEnvironment` 组合三部分：`ToolRunner`（工具注册与执行协议）、`build_full_context`（不可信长期笔记 + 短期摘要 + 最近消息）、策略循环（`react` 或 `plan_execute`）。构造时只要求所选策略的依赖，拒绝缺少必需依赖的配置。
2. `run(task)` 返回 `EnvironmentRun(task, trace, context_messages)`；`context_messages` 统计实际注入的 memory messages，不再借用 trace record 数量。
3. `react` 路径在每轮决策前只读取当前 memory context 这一份可变上下文，并把 tool action、observation 与 final answer 写回 `ShortTermMemory`，避免重复注入和绕过压缩预算。长期记忆接入必须显式提供 `memory_scope`。
4. `enable_memory_search=True` 时，每次 run 创建独立的 `MemorySearchToolRunner`，因此两次调用上限不会跨 run 泄漏。正文进入动态短期上下文，Trace 只保留 `memory_search:ok:<count>` 等稳定元数据。
5. `plan_execute` 尚未消费 memory，传入 short/long memory、scope、search 或 initial context 时显式拒绝。`build_context()` 保留为显式上下文组装入口，并要求 scope。
6. 环境单进程、注入式、确定性：策略、工具、记忆全部可注入；Provider/网络能力只能由上层显式注入。

## 验证

- 测试覆盖：策略运行与报告、按策略依赖校验、显式 scope、plan memory 参数拒绝、动态 memory 单次注入和写回、memory search 调用上限与 trace 脱敏、全上下文组装顺序、非法策略拒绝、demo 可复现。
- demo 使用固定任务与固定后端，连续两次运行逐字节一致。
