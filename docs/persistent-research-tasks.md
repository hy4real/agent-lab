# 持久研究任务：第一阶段

本文保留第一阶段设计和验收记录。当前实现已叠加 [第二阶段](backend-engineering-plan.md) 的 300 秒总 deadline、100 条接纳上限、取消接口与执行子进程监督；下面第一阶段中的无限运行边界已由第二阶段收口。schema 仍为 v1，部署时 API 与 worker 必须一起升级。

## 目标与范围

把现有离线 `ResearchAgent` 接入可重启的 HTTP 任务链路。SQLite 使用 Python 标准库，API 继续属于可选 serve extra。同步 `/v1/research` 保留；新任务 API 只有显式配置 `--task-db` 时启用。独立 `research-worker` 进程复用现有研究循环，不引入新的 Agent loop。

```text
客户端 → FastAPI → SQLite research_tasks ← worker → ResearchAgent
客户端 ← 查询状态/结果 ← SQLite
```

只支持同机本地文件系统、单个本地用户、默认离线 lexical 研究。暂不接入 live Provider、远端多租户、会话存储或有外部写入副作用的工具。提交问题和结果是私有持久数据；不写日志、git 或发行包。

## 数据合同

schema v1 使用独立 application_id 和 user_version；未知数据库或版本拒绝打开。只初始化全新空数据库，不迁移已有 JSON/JSONL 或长期记忆。

`research_tasks` 保存：`id` 主键、`request_key` 唯一键、不可变 `payload_json`、`content_hash`、`corpus_hash`、`status`、`worker_id`、`attempt`、`max_attempts`、`lease_until`、`result_json`、`error_code`、`created_at`、`updated_at`。索引覆盖 status/lease_until/created_at；CHECK 限定状态、attempt 与结果/租约一致性。

payload 是 canonical JSON，包含 question、corpus_hash 和固定 executor_version。相同键必须匹配完整 payload 与 hash；同键同参数返回已有任务，同键异参返回 409。语料指纹来自 `Corpus.content_hash`，覆盖来源、文本和切分。worker 加载启动语料快照；每次执行比较该指纹和执行器版本，不匹配则失败，不偷偷换输入。成功后包含拒答的正常研究结果也记作 succeeded，具体答案状态保留在 result.answer 中。

## 状态和并发

`pending → running → succeeded | failed`。worker 崩溃后，running 保持原状直到 lease 到期；接管原子增加 attempt。默认每任务最多 3 次领取；第 3 次执行也崩溃后，后续领取扫描把过期任务标记为 attempts_exhausted，防止永久重试。

SQLite WAL、FULL synchronous、5 秒 busy timeout。每个操作独立连接，写操作用 BEGIN IMMEDIATE，提交后关闭连接；锁竞争超时返回稳定的存储错误，不视作领取成功。时间在取得写锁后读取，使用同机 UTC epoch 秒。系统时钟跳变会影响租约时长，本阶段不解决跨主机时钟问题。

领取事务选择 pending 或已过期 running 行，再更新 owner、attempt、lease。续租和完成都必须同时满足 id、running、worker_id、attempt 和 lease_until > now；影响 0 行意味着失去资格。工具运行绝不持有数据库事务。默认租约 30 秒、心跳 10 秒；心跳失败后放弃写回，让租约自然过期。进程仍可能继续计算，版本校验只保护数据库写回，不能撤销外部动作。活着且持续心跳的卡死任务需要后续 deadline 机制。

这是至少一次计算、至多一次被接受的最终结果写入，不是 exactly-once 执行。当前只读工具允许重新计算；未来外部写工具必须另外设计稳定 operation_key、请求参数持久化和资源方幂等。

## HTTP 和 CLI

- `serve --corpus ... --task-db .agentlab/tasks/research.sqlite3`：启动任务 API；同步接口继续可用。
- `POST /v1/research/tasks`：JSON `{request_key, question}`，新任务返回 202，重复返回 200；Location 指向查询地址。
- `GET /v1/research/tasks/{id}`：200 返回状态/结果；不存在为 404。
- 两个接口都检查 loopback Host、Origin 和 X-AgentLab-Token；schema 错误 422，参数冲突 409，数据库不可用 503。响应不回显原始问题、request_key、worker_id 或数据库路径。
- `research-worker --corpus ... --task-db ... [--worker-id ...] [--once] [--lease-seconds 30]`：默认轮询，--once 最多领取一个任务并输出脱敏 JSON。Ctrl-C 退出，未完成任务依靠租约恢复。

数据库目录必须是当前用户拥有的 0700 目录；新建数据库文件为 0600，拒绝符号链接、不安全目录及非普通/共享权限文件。目录隔离保护 WAL/SHM。该边界不抵御同一 OS 用户主动替换运行中的数据库文件。不同 corpus 的服务不应共用任务数据库。

## 验收

真实 SQLite、多连接/多进程测试必须覆盖同键提交竞争、异参冲突、领取竞争、续租、过期接管、旧 attempt 写回拒绝、重启恢复、语料漂移、重试上限、数据库 busy 和 API 访问边界。子进程在执行前及执行后写回前被终止后，由新 worker 恢复，结果可查询。可控时钟测试不伪称真实容量测试。

执行项目 AGENTS.md 中 pytest coverage ≥90%、compileall、ruff、mypy、build、包内容审计、CLI help 和 diff 检查。新增文件加入现有包内容 allowlist；不更改 CI。使用临时私有状态目录和合成语料，不访问真实用户数据库或 Provider。

`.venv/bin/python scripts/check_research_tasks_http.py` 显式启动真实 loopback HTTP server 与独立 CLI worker，验证四个并发提交、冲突、鉴权、服务停止期间消费及重启后查询。仅此验收脚本允许本地 TCP，不改变默认 pytest 禁网策略；输出只包含脱敏计数和判断，临时 token 不输出。

停用时不传 --task-db 并停止 worker 即可返回同步模式，保留数据库以供恢复；无需数据迁移或删除。

## 本地验收记录（2026-09-18）

- Python 3.14.5、SQLite 3.53.2、FastAPI 0.141.1；全套 `pytest --cov=agentlab --cov-report=term-missing --cov-fail-under=90`：289 passed，总覆盖率 90.91%。新 tasks 模块 98%，worker 模块 100%。
- 四个独立进程首次打开同一数据库并发提交，只创建一个任务；四进程竞争同一个待执行任务，只领取一次。执行前和执行后写回前两个窗口都实际终止子进程，再用可控时钟推进租约，由 B 以 attempt 2 完成原任务。
- `scripts/check_research_tasks_http.py`：真实 Uvicorn/loopback HTTP 的 4 个并发提交返回一个 202 和三个 200；API 关闭期间独立 CLI worker 完成任务，重启后可读取原结果，旧会话 token 被拒绝。Provider 调用为 0。
- compileall、ruff、mypy、CLI help、git diff --check、文档本地链接均通过。sdist/wheel 在 `dist/research-tasks-stage1/` 单独构建，`scripts/check_package_contents.py dist/research-tasks-stage1` 审计通过；根 dist 中保留的多个历史版本不适合直接传给仅接受一对归档的审计入口。
- 全套测试仍出现一条既有 Qdrant ResourceWarning。单跑向量索引测试并启用 tracemalloc，定位到 `test_existing_index_requires_explicit_rebuild` 经 QdrantClient/CollectionPersistence 打开的 SQLite 连接；15 项向量索引测试通过。本次没有调整该独立索引模块。

这些结果证明本地持久任务合同和故障恢复，不证明生产容量、外部副作用幂等或维护者本人已掌握；本人代码解释与故障复现另行验收。
