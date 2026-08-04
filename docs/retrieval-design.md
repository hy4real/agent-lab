# Retrieval And Hybrid RAG Design

- 状态：lexical baseline 与 v1.2 Hybrid RAG 已实现并通过离线工程验收（2026-08-02）
- 范围：安全 corpus ingestion、确定性 chunking、lexical baseline、可选本地 embedding、Qdrant 本地向量索引、reciprocal-rank fusion、检索评测与 stale index 防护
- 当前事实：`Corpus.search()` 保持默认词法 baseline；`research.retrieval.HybridRetriever` 已将 Qdrant local vector search 与 lexical 结果通过 RRF 融合，并经 `search_corpus` 进入 `ResearchAgent` 显式 hybrid 主链路
- 非目标：远端向量数据库、多租户检索、在线索引服务、MCP、真实用户/生产效果、未明确授权的真实语料、模型质量结论

## 决策

1. `chunk_text(text, chunk_size, overlap)`：固定字符长度分块，overlap 必须小于 chunk_size，确定性可测。
2. `LexicalIndex`：CJK 字符 + 拉丁词 term 倒排索引，`search(query, top_k)` 按命中降序、chunk 序升序。
3. `evaluate`：对每个查询按 gold 文档计算 `hit@k` 与 `recall@k`（文档级相关性）。
4. `run_chunking_experiment`：固定语料 × 固定查询 × 多个 chunk_size，输出各 chunk_size 的指标与 `best_chunk_size`；全部确定性。
5. 本版语料为完全虚构的合成文档；任何私有或受限语料均不得在未明确授权前读取或写入证据。

## v1.2 计划决策

1. **保留 lexical baseline**：默认 CLI 和默认测试继续不需要模型、网络或额外依赖；历史 suite 的结果不被新检索器覆盖。
2. **增加本地 Hybrid RAG**：使用可选 `rag` extra 提供 `qdrant-client` 与 `fastembed`；固定多语言模型 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`，通过 `EmbeddingProvider` 适配，不把依赖导入 core。
3. **索引显式、可验证、可失效**：Index Manifest 绑定 corpus hash、每个 chunk hash、chunk 参数、embedding model、embedding backend/version、固定 probe 的 embedding hash、向量 dimension、distance metric 和 index version。任一依赖或 embedding 行为变化都必须拒绝复用旧索引并要求显式 rebuild。
4. **主链路必须真实使用**：`ResearchAgent -> ToolRegistry.search_corpus -> lexical/hybrid retriever` 是唯一检索入口；只在测试中构造 HybridRetriever 不算完成。
5. **结果仍由原有 citation validator 把关**：向量相似度只能改变候选 chunk 排序，不能直接生成事实；所有最终 Claim 仍须精确 citation，无法引用时 fail closed。
6. **Prompt 与检索模式分开版本化**：grounded prompt profile 记录检索模式、索引版本和提示版本；没有 baseline 对照和固定评测结果时，不表述“Prompt 效果优化”。

## 验证

- 测试覆盖：分块尺寸/overlap 校验、索引排序、hit@k/recall@k、实验确定性、demo 可复现。
- demo 使用固定语料与查询，连续两次运行逐字节一致。
- 默认离线：无网络、无真实 embedding、无密钥、无真实语料；安装 `rag` extra 和下载模型必须是显式动作。
- v1.2 代码阶段新增：local index manifest 校验、corpus/chunk/model stale 拒绝、lexical/hybrid 主链路端到端调用、semantic rewrite case、多文档冲突、无答案、prompt injection 与 citation validator 回归。
- v1.2 评测阶段保留 `research-agent-suite-v1` 不变，另建独立 RAG suite v2；若 Hybrid 在预注册 semantic subset 上没有可解释改善，报告必须标记失败，不得调整 gold 或阈值。
