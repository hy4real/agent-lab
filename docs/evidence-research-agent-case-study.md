# Evidence Research Agent Case Study

Agent Lab 原有短期记忆、长期记忆、ReAct、工具调用、检索和评测组件。v0.9-v1.2 把这些组件接成了一个 Provider 无关的本地 Evidence Research Agent，并增加可选 Hybrid RAG：它在用户显式选择的语料上调用工具完成研究任务，输出事实必须带有可由程序重新定位的原文引用。

## 主链路

```text
显式选择的本地 corpus
  -> 有界读取与稳定 source/chunk identity
  -> AgentEnvironment + ReActStrategy
  -> lexical 或 FastEmbed + Qdrant vector retrieval
  -> reciprocal-rank fusion
  -> list_sources / search_corpus / read_chunk
  -> ShortTermMemory + untrusted LongTermMemory notes
  -> claims + exact citations
  -> deterministic citation validator
  -> answer + sanitized trace
```

`ResearchAgent` 直接复用既有 `AgentEnvironment` 和 `ReActStrategy`，没有实现第二套 Agent loop。它通过 `list_sources`、`search_corpus` 和 `read_chunk` 三个 callable tools 完成来源发现、检索和原文读取；工具参数使用严格 schema，未知工具、非法参数和 handler 异常都会转换为稳定错误。

短期记忆进入每轮决策上下文。长期记忆只作为标注为不可信的历史笔记参与规划，不能成为事实引用，也不能由模型自动写入。

## 可信输入边界

Corpus 只读取显式根目录内的 UTF-8 `.md` 和 `.txt` 文件，最多包含 32 个来源，单文件不超过 1 MiB，总量不超过 8 MiB。读取过程固定 root directory file descriptor，逐级使用 no-follow 语义打开并校验普通文件，拒绝符号链接、路径逃逸和其他文件类型。

来源文本规范化换行后生成稳定的 content、source 和 chunk hash。文档始终是不可信数据，其中的指令不能修改系统规则、工具权限、输出 schema 或引用要求。

## Hybrid RAG 与 Prompt

默认 Research 路径仍使用 lexical retrieval。显式选择 hybrid 时，FastEmbed 生成 query embedding，Qdrant local mode 返回 vector candidates，`HybridRetriever` 再把 lexical 与 vector 排名通过 reciprocal-rank fusion 合并。融合分数只用于候选排序，不作为答案置信度；最终原文仍必须经 `read_chunk` 读取。

本地 Index Manifest 绑定 corpus hash、每个 chunk hash、chunking 参数、embedding backend 及版本、模型、固定 probe 的 embedding hash、dimension、distance 和 point count。加载前逐项验证，任何漂移、损坏、未知字段或 payload/count 不一致都会 fail closed，并要求显式 rebuild。

`baseline-v1` 与 `grounded-hybrid-v1` 两个 Prompt Profile 都进入 run provenance。grounded profile 要求先检索和读取、只依据 `read_chunk`、分别引用冲突来源、无证据时拒答并忽略文档内指令。当前没有真实 Provider Prompt A/B，因此只陈述“设计并验证版本化 grounded prompt”，不陈述生成质量提升。

## 引用与失败策略

每条 `Claim` 至少包含一个由 `source_id`、`chunk_id`、`start`、`end` 和 `exact_text` 组成的 `Citation`。运行结束后，确定性 validator 会重新检查来源与 chunk 的所属关系、span 边界及原始 source 切片，只有与 `exact_text` 逐字符一致的 claim 才能进入 answer。

系统采用 fail-closed 行为。无相关来源、无可引用证据、工具失败、步骤预算耗尽、非法 JSON 或非法 citation 都不会保留部分事实，而是返回 `insufficient_evidence` 及稳定原因。

## 交付界面

默认 Replay reasoner 完全离线；只有显式 `--live` 才会调用 OpenAI-compatible Provider，CLI 会在 stderr 提示外发边界，并保持 JSON stdout 只输出 JSON。命令行提供离线 demo、本地 corpus 研究、冻结评测和本地服务四个入口。

HTTP API 只开放 `GET /health` 和 `POST /v1/research`。服务固定启动时的 corpus root，不接受请求指定文件路径；本地进程只绑定 `127.0.0.1`，研究请求还要通过 loopback Host、可选 Origin 和随机 session token 校验。

每次运行的 trace 只记录 finish reason、steps、tool name 与 status、duration、estimated tokens 及当前 run 的 Provider usage delta。问题、prompt、messages、corpus 原文、绝对路径、密钥和底层异常正文都不会进入 trace。

## 冻结评测

`research-agent-suite-v1` 冻结了 30 个 case，覆盖 direct retrieval、multi-document、conflicting sources、unsupported/refusal、prompt injection 和 tool/budget failure。其中 18 个 case 来自仓库公开设计文档的冻结副本，12 个是完全合成的对抗 fixture；修改 case、Gold、阈值或递归 corpus 内容必须提升 suite version。

本次离线运行 30/30 case 通过，citation validity 为 1.0，unsupported factual claims 为 0，refusal contract 为 1.0，retrieval recall@5 为 1.0。

独立 `research-rag-suite-v2` 冻结 13 个完全合成 case。实际 FastEmbed 0.8.0 + Qdrant 1.18.0 local 运行中，6 个 semantic rewrite case 的 recall@5 从 lexical 0.50 提升到 Hybrid 1.00，3 个 lexical-friendly case 无下降，citation validity、refusal、prompt injection 与 Hybrid mainline contract 均为 1.00，unsupported factual claims 为 0。

仓库全量验证为 156 tests 通过，coverage 不低于 90%。CI 默认运行 pytest、coverage、network deny、compileall、Ruff、mypy、build 和 package audit，不读取真实密钥，也不调用真实网络或 Provider。

## 能力边界

这些结果只证明冻结 synthetic/offline fixture 上的检索差异和确定性 Agent 契约，不证明模型回答质量、真实并发性能、生产可用性、真实用户规模、业务效果或外部结果。RAG suite 只有 13 个 case 和 12 个索引点；项目没有使用真实用户数据，也没有把合成评测结果外推为线上结论。
