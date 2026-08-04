# Retrieval Chunking Experiment Report

- 实验时间：2026-07-31
- 状态：确定性离线结果；语料为合成文档（边界：真实论文语料实验需数据授权）

## 方法

- 语料：3 篇合成中文文档（缓存/队列/熔断主题），固定不变
- 查询：3 组 `(query, answer_phrase)`；gold 为分块后**完整包含 answer_phrase 的 chunk 集合**
- 索引：词法 term 倒排（CJK 字符 + 拉丁词），`search(query, top_k=3)`
- 变量：chunk_size ∈ {6, 12, 24}，overlap=0
- 指标：`hit@3` 与 `recall@3`（chunk 级）

## 结果

| chunk_size | hit@3 | recall@3 |
| --- | --- | --- |
| 6 | 0.000 | 0.000 |
| 12 | 0.000 | 0.000 |
| 24 | 1.000 | 1.000 |

best_chunk_size = 24

## 解读与边界

- 长 answer_phrase（16-18 字符）在 chunk_size 6/12 时被切分到多个 chunk，无单个 chunk 完整包含答案，因此 gold 为空、召回为 0；chunk_size 24 能完整容纳答案短语，召回 1.0。该结果展示 harness 能区分 chunk 大小对召回的影响，不代表真实语料的绝对召回水平。
- 本报告可复现：`python -m agentlab.demo` 输出逐字节一致。
- 不表述语义检索、真实语料效果或生产可用性；受限语料不属于本报告范围。
