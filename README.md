# Agent Lab

Provider 无关、无框架的本地 Evidence Research Agent。它把短期/长期记忆、ReAct、工具调用、检索、评测和可观测组件接成一条可运行主链路，并用精确 span 校验阻止无证据事实进入答案。项目适合研究 Agent 运行时边界、可复算评测和 grounded retrieval；它不是生产服务或模型质量基准。

## 项目状态

当前版本是公开发布的脱敏快照，默认路径完全离线。代码、fixture、测试、Demo 和构建审计均可在本仓库内复核；不声明真实用户、生产部署、社区采用或外部影响力。

公开仓库：https://github.com/hy4real/agent-lab

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,serve]"
.venv/bin/agentlab research-demo
.venv/bin/agentlab research --corpus ./example/corpus --question "你的问题"
```

`research` 默认完全离线。`--live` 需要先安装 `.[live]`，并会把问题、选中的 chunks 和工具观察发给 OpenAI-compatible Provider；该边界也会写到 stderr。

## Controlled Memory

长期记忆采用显式 candidate → confirmed/inactive 生命周期。只有同 scope 的 confirmed 记录可召回；Agent 不会自动抽取或写入事实，记忆也不能成为 Research Citation Source。

```bash
printf 'Project demo uses a local queue.' | .venv/bin/agentlab memory propose \
  --scope project:demo --kind decision --origin manual --expected-revision 0
.venv/bin/agentlab memory confirm <record-id> \
  --scope project:demo --expected-revision 1
.venv/bin/agentlab memory list --scope project:demo
```

CLI 默认状态为 `.agentlab/longterm.json`，正文从 stdin 读取，JSON 输出不回显正文或绝对路径。旧 schema 先执行 dry-run；只有显式 `--apply` 才会在生成逐字节 backup 后迁移：

```bash
.venv/bin/agentlab memory import-legacy --scope project:demo
.venv/bin/agentlab memory import-legacy --scope project:demo --apply
```

ReAct 可按 run 显式启用 `memory_search`：自动召回最多 2 条/512 estimated tokens，主动搜索每次最多 5 条/1024 tokens、每轮最多 2 次。记忆正文只进入当轮规划上下文，Trace 只保存工具名、状态和结果数量。`plan_execute` 仍不支持 memory context。

## Hybrid RAG

Hybrid retrieval 是显式安装和显式选择的可选路径；默认 lexical 行为不变。

```bash
.venv/bin/pip install -e ".[rag]"
.venv/bin/agentlab research-index build \
  --corpus ./example/corpus \
  --index-dir .agentlab/example-index \
  --model-cache .agentlab/model-cache
.venv/bin/agentlab research \
  --corpus ./example/corpus \
  --question "Agent Lab 如何校验引用？" \
  --retrieval hybrid \
  --index-dir .agentlab/example-index \
  --model-cache .agentlab/model-cache
```

本地 Qdrant index manifest 绑定 corpus、chunking、embedding backend/model/probe、dimension、distance 和 point count；任何变化都会拒绝旧索引，并要求显式 `research-index rebuild`。

## API 与 Docker

```bash
.venv/bin/agentlab serve --corpus ./example/corpus --port 8000
docker build -t agent-lab:local .
docker run --rm -v "$PWD/example/corpus:/corpus:ro" -p 127.0.0.1:8000:8000 agent-lab:local
```

API 仅提供 `GET /health` 和 `POST /v1/research`。本地服务只监听 `127.0.0.1`；请求只能提交 question，不能指定本地路径。
启动命令会在 stderr 输出本次会话 token；调用 `POST /v1/research` 时通过 `X-AgentLab-Token` header 提交。Host 与浏览器 Origin 也必须指向 loopback。

## 离线评测

```bash
.venv/bin/agentlab research-eval --suite-root evals/research-agent
```

冻结 suite 共 30 case，门槛为 citation validity 100%、unsupported factual claims 0、refusal contract 100%、retrieval recall@5 至少 0.90。它只证明确定性 fixture 上的工程契约，不证明模型质量、真实用户规模或业务效果。

Hybrid RAG 使用独立的冻结 suite v2：

```bash
.venv/bin/agentlab research-rag-eval \
  --suite-root evals/research-rag-v2 \
  --index-dir .agentlab/rag-suite-v2-index \
  --model-cache .agentlab/model-cache
```

实际 FastEmbed + Qdrant local 结果为：6 个 semantic rewrite case 的 recall@5 从 lexical `0.50` 提升到 Hybrid `1.00`，3 个 lexical-friendly case 无下降，citation/refusal/prompt-injection/mainline 契约均为 `1.00`。该结果只适用于 13-case、12-point 的冻结合成评测，详见 `docs/rag-evaluation-report.md`。

RAGChecker paper-to-code 实验使用独立的 oracle-label suite：

```bash
.venv/bin/python scripts/evaluate_ragchecker_replication.py \
  --suite-root evals/paper-ragchecker-v1 \
  --check evals/paper-ragchecker-v1/canonical-report.json
```

该实验复现论文的 claim-level 指标计算层，并让三个真实 `ResearchAgent` 合成运行分别呈现 clean context、irrelevant noise 与 retrieval miss。它不包含 claim extractor、entailment model 或论文 benchmark，不能表述为完整 RAGChecker 复现或模型质量评测。

### Controlled Memory Evaluation

```bash
.venv/bin/python scripts/evaluate_controlled_memory.py \
  --suite-root evals/controlled-memory-v1
```

冻结 suite 包含 7 条合成记录、8 个查询、7 项 100% 契约门槛，以及 migration、corruption、CAS、concurrent write 和 feedback-loop probe。canonical report 当前全部通过；它不证明模型记忆质量、真实用户价值、生产并发或多租户能力。

## 范围

- Run 1：短期记忆（`docs/memory-design.md`）
- Run 2：长期记忆（`docs/longterm-design.md`）
- Run 3：推理策略对比（`docs/reasoning-design.md`）
- Run 4：弹性工程组件（`docs/resilience-design.md`）
- Run 5：评测与可观测（`docs/evaluation-design.md`）
- Run 6：检索实验（`docs/retrieval-design.md`）
- Run 7：交互环境（`docs/environment-design.md`）
- v0.9-v1.1：Evidence Research Agent（`docs/v0.9-v1.1-research-agent-plan.md`）
- v1.2：Hybrid RAG、Qdrant local index 与 grounded prompt（`docs/v1.2-hybrid-rag-plan.md`）
- v1.3：RAGChecker claim-level 指标层复现（`docs/v1.3-ragchecker-paper-to-code-plan.md`）
- v1.5：受控长期记忆、scope/预算/并发契约与冻结评测（`docs/v1.5-controlled-memory-plan.md`）
- Case study：Evidence Research Agent 工程实现与验证（`docs/evidence-research-agent-case-study.md`）
- RAG report：冻结合成评测与边界（`docs/rag-evaluation-report.md`）
- Paper-to-code report：RAGChecker 方法映射、实验结果与边界（`docs/ragchecker-paper-to-code-report.md`）
- 可选真实模型评测：`docs/real-model-eval-report.md`（需显式授权）

本项目只陈述可核验的工程事实，不表述模型能力、生产结果或外部采用。
