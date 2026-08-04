# Evaluation And Observability Design

- 状态：实施中（Run 5；2026-07-31）
- 范围：以 Agent Lab 为评测对象的离线评测 harness——固定 case 回归、效果/效率/资源成本量化指标、多轮可观测聚合
- 非目标：真实模型评测（需另行授权）；语义质量评分（当前为确定性 expected-result 匹配）；真实业务转化或营收表述；并发/生产结论

## 决策

1. `EvalCase(id, task, expected_result, tags)` 固定 case 契约；执行器、时钟、token 计数全部注入，保证确定性。
2. `EvalRunner` 对每个 case 记录：ok（expected_result 命中）、quality_score（1.0/0.0，确定性）、completion_seconds（注入时钟差值）、tokens_used（既有字符启发式 + 输出）。
3. `RegressionSuite` 固定 case 集 + baseline snapshot hash（canonical JSON of id/ok）；重跑对比，输出通过/失败明细与 `baseline_match`。
4. 量化指标（`aggregate`）：success_rate、avg_quality、total_tokens、avg_completion_seconds。
5. 可观测（`aggregate_runs`）：多轮聚合 + per-tag 成功率；`conversion_proxy` = 任务完成率，显式标注"任务完成率代理业务转化；不表述真实业务转化或营收"。
6. 全部离线、无网络、无密钥；报告必须带边界声明。

## 验证

- 测试覆盖：质量与用量记录、聚合指标、回归通过/失败与 baseline 匹配/不匹配、多轮聚合、per-tag 成功率、转化代理边界、报告渲染、demo 可复现。
- demo 使用固定 case、固定执行器与固定时钟，连续两次运行逐字节一致。

## 后续

- 若用户授权真实模型评测，可把 `execute` 替换为真实 Agent 调用并单独记录评测边界；本版不包含该结论。
