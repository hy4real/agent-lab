# Hybrid RAG Evaluation Report

- 状态：通过（2026-08-02）
- Suite：`research-rag-suite-v2`
- 范围：冻结合成语料上的 lexical 与 Hybrid retrieval 对照，以及确定性 Research Agent 契约
- 非目标：Provider 生成质量、真实用户价值、生产规模、业务效果或外部结果

## 实际运行链路

```text
12 个 synthetic corpus chunks
  -> FastEmbed 0.8.0 / multilingual MiniLM-L12-v2
  -> Qdrant 1.18.0 local persistent index
  -> lexical + vector candidate retrieval
  -> reciprocal-rank fusion
  -> ResearchAgent.search_corpus / read_chunk
  -> exact citation validator
```

本报告来自实际本地 FastEmbed 与 Qdrant 索引，不使用测试中的 Gold vector provider。运行未调用网络、真实 Provider 或私人语料。

## 结果

| 指标 | Lexical | Hybrid | 预注册门槛 | 结果 |
| --- | ---: | ---: | ---: | --- |
| 6 个 semantic rewrite case 的 recall@5 | 0.50 | 1.00 | Hybrid >= 0.80，绝对提升 >= 0.20 | 通过 |
| 3 个 lexical-friendly case 的 recall@5 | 1.00 | 1.00 | 下降 <= 0.10 | 通过 |
| citation validity | - | 1.00 | 1.00 | 通过 |
| unsupported factual claims | - | 0 | 0 | 通过 |
| refusal contract | - | 1.00 | 1.00 | 通过 |
| prompt injection contract | - | 1.00 | 1.00 | 通过 |
| Hybrid mainline contract | - | 1.00 | 1.00 | 通过 |

13 个 case 全部通过。完整、无原文的结构化结果保存在 `evals/research-rag-v2/canonical-report.json`。

## Index Provenance

| 字段 | 值 |
| --- | --- |
| index version | `87d2f8a5fb0b00d01bbd1a451427ec2c46ddbb27fd55a1f220a536db72213ac3` |
| corpus hash | `3406717314761927765993691d239ec7a7507f8a631b5e9abe3b26b87d454b7b` |
| backend | `fastembed 0.8.0` |
| model | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| embedding probe hash | `c3b79129435d4366090bc692fdd74dfec0b7739c1f500d09eb86b0bdd574981b` |
| dimension / distance | `384 / cosine` |
| point count | `12` |

Manifest 同时绑定 corpus、每个 chunk、chunking 参数、backend 版本、模型、probe hash、维度、距离和 point count。任何变化都会使旧索引 fail closed，并要求显式 rebuild。

## Prompt Boundary

`baseline-v1` 与 `grounded-hybrid-v1` 已版本化并记录在 run trace。后者要求先检索和读取，只依据 `read_chunk` 回答，冲突分别引用，无证据时拒答，并忽略文档内指令。

本次没有运行真实 Provider 的 Prompt A/B。`prompt_injection_contract=1.0` 证明的是确定性离线 Agent 与 citation validator 契约，不能表述为 Prompt 生成质量提升。允许的说法是“设计并验证版本化 grounded prompt”；不允许写“Prompt 优化使回答质量提升”。

## 已知限制

- Suite 只有 13 个 case 和 12 个索引点，适合验证工程链路与预注册行为，不代表真实语料泛化。
- Qdrant 1.18.0 local mode 在 Python 3.14 执行 collection rebuild 时会因上游未先关闭被删除 collection 的 SQLite storage 产生 `ResourceWarning`；普通 build、verify 和 query 路径已显式关闭连接。该告警不影响结果，但当前不把 local rebuild 描述为生产级索引迁移。
- FastEmbed 会提示该模型从 CLS pooling 改为 mean pooling。Manifest 记录 backend 版本和固定 probe hash，因此行为变化会使旧索引失效，而不会静默复用。

## 结论

本次结果支持以下工程陈述：Agent Lab 实现了实际进入 Research Agent 主链路的本地 Hybrid RAG、可验证失效的 Qdrant 索引、RRF 融合、版本化 grounded prompt 与确定性 citation/refusal/injection 防线，并在冻结合成 semantic rewrite subset 上观察到可重算的 recall@5 提升。

本报告不支持模型质量、真实用户、生产吞吐、业务效果或外部结果结论。
