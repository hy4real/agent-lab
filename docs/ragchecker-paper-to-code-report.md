# RAGChecker Claim-Level Metric Replication

- 状态：工程实现与冻结离线评测已通过；本人方法答辩待完成（2026-08-04）
- 范围：RAGChecker claim-level 指标计算层的 oracle-label 复现
- 论文：arXiv `2408.08067` v2，NeurIPS 2024 Datasets and Benchmarks Track，DOI `10.52202/079017-0692`
- 官方实现基线：`amazon-science/RAGChecker@6091f08c00e676e87a970f2aeb4a23a484746348`
- 非目标：claim extraction、entailment model、完整 benchmark、meta-evaluation、模型质量或论文结论复现

## 验证问题

Agent Lab v1.2 的 recall@5 能说明 gold source 是否进入 top-k，但无法说明错误来自漏召回、无关上下文、相关上下文中的噪声，还是生成器脱离上下文。该实验验证：把 ground truth、response 和 retrieved chunks 转成论文定义的 claim entailment 关系后，是否能用确定性代码稳定区分这些来源。

## 论文主张到代码映射

| 论文方法 | 本地实现 | 验证 |
| --- | --- | --- |
| response claim 相对 ground-truth answer 的 entailment | `ClaimEntailmentMatrices.answer2response` | 非方阵主案例锁定 precision 方向 |
| ground-truth claim 相对 response 的 entailment | `response2answer` | 同一案例锁定 recall 方向，避免两组向量混用 |
| ground-truth claim × retrieved chunk | `retrieved2answer` | claim recall、context precision、context utilization |
| response claim × retrieved chunk | `retrieved2response` | 两类 noise、hallucination、self-knowledge、faithfulness |
| relevant chunk 包含至少一条 GT claim | `compute_ragchecker_metrics()` 中逐 chunk 归约 | relevant/irrelevant overlap case |
| 同一错误 claim 同时受两类 chunk 支持时只计 relevant noise | irrelevant 分支显式排除 relevant support | 排他案例预期 `1.0 / 0.0` |
| 官方退化输入分支返回 `0.0` | 显式 response/GT/chunk count guard | zero-response、zero-GT、zero-chunk cases |

指标计算只使用 Python 标准库。输入必须携带唯一 claim/chunk ID；四个矩阵的方向、行列数和严格 bool 类型在构造时验证，非法输入 fail closed。

## 冻结实验

Suite 在实现前固定为 6 个矩阵案例和 3 个真实 Agent 案例。后者使用完全合成语料和冻结排名，但真实经过 `ResearchAgent.run()`、list/search/read tool loop、claims、citations 与 trace；expected metrics 只在运行结束后比对，不参与矩阵计算。

| Agent case | Precision / Recall | Claim recall | Context precision | 诊断信号 |
| --- | --- | --- | --- | --- |
| clean context | `1.0 / 1.0` | `1.0` | `1.0` | 全部 response claims 有检索支持 |
| irrelevant noise | `0.5 / 0.5` | `0.5` | `0.5` | irrelevant noise sensitivity `0.5` |
| retrieval miss | `1.0 / 0.5` | `0.5` | `1.0` | 没有噪声；缺口来自未召回第二条 GT claim |

9 个案例的 11 项指标均在 `1e-12` 容差内匹配预注册值。macro metrics 只是不同故障构造的算术平均，不是 RAG 质量分数，也不用于比较系统。

## 首个结果耗时

- 预注册与计时起点：`2026-08-03T15:51:45Z`
- 首个 9/9 通过结果：`2026-08-03T16:02:00Z`
- 工程耗时：`615` 秒（10 分 15 秒）

该时间只记录本轮 AI 辅助的“冻结方案到首个可运行实现”窗口，不代表候选人独立阅读论文的速度，也不作为效率提升结论。用户本人能否解释方法由单独答辩门判断。

## 可重算产物

```bash
.venv/bin/python scripts/evaluate_ragchecker_replication.py \
  --suite-root evals/paper-ragchecker-v1 \
  --check evals/paper-ragchecker-v1/canonical-report.json
```

该命令已重算并逐字节核对 canonical report。19 项 RAGChecker 定向测试通过；仓库全量回归为 175 tests、coverage 90.18%。运行默认禁止网络，不调用 Provider、embedding、claim extractor 或 entailment model。

## 偏差与边界

1. 论文的自动评测有效性依赖 claim extraction 和 entailment checking；本实现用人工冻结 oracle label 隔离指标公式，因此不能验证这两个模型环节的误差。
2. 三个 Agent case 使用确定性 Replay reasoner 和注入排名，只证明指标接入真实 artifact 后的归因差异，不证明 lexical、Hybrid 或 Provider 质量。
3. 没有复现论文 benchmark、八个 RAG 系统对比或与人类判断的相关性实验。
4. 允许的表述仅为“基于 RAGChecker 论文复现 claim-level 指标计算层，并以冻结合成实验区分检索缺失与无关噪声”。禁止写“完整复现 RAGChecker”“验证论文结论”或“提升 RAG 质量”。
5. 工程实现完成不自动证明维护者本人掌握论文方法。维护者答辩通过前，不在公开项目说明中声称已完成论文理解闭环。
