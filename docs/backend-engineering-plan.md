# 后端工程推进计划

## 当前范围

第一阶段已接入持久任务，第二阶段解决任务无限运行、调用方无法取消和无界接纳，第三阶段已补齐本地双服务部署与诊断观测。会话存储和 JobPrep 服务化仍是独立交付。

## 第二阶段设计

沿用 research_tasks schema v1，不迁移数据库、不重写历史参数。deadline 固定为 created_at + 300 秒，跨排队、领取、接管共享；策略变更须重新评审，旧版本 worker 不可与新版混跑。老数据按其原创建时间应用相同规则。到期任务设置 status=failed/error_code=deadline_exceeded；用户取消设置 failed/cancelled。这是明确终态原因，不代表模型或工具异常。

入口在同一个 BEGIN IMMEDIATE 内清理过期任务、检查同键重放、计数尚未结束任务并插入。上限 100，包含 pending/running；已有键优先处理，队满仍允许重放，异参仍返回 409。容量满返回 429 + Retry-After: 1，新请求不落库。完成、取消和过期释放容量；历史结果保留，磁盘保留策略另行设计。该限制只覆盖持久任务接口，不是同步接口或整台机器的全局并发限制。

首次并发启动曾出现一次初始化失败。schema 事务提交与 WAL 切换是两个独立 SQLite 操作，因此初始化全程（含数据库 sidecar 校验）现在使用同目录 0600 的 `<database>.init.lock` 做有界 advisory lock；等待沿用 busy_timeout，失败明确报错。运行期仍由 SQLite 事务控制并发，初始化锁不包裹 Agent 执行。已在 WAL 模式的数据库只读取模式，不再次要求切换。用五轮四进程同步启动和初始化锁超时测试验收。

新增 POST /v1/research/tasks/{id}/cancel。无记录 404；有效任务原子终止；重复取消或已结束任务返回已有终态 200；完成与取消由 SQLite 写事务提交顺序决定，终态不被覆盖。继承 Host/Origin/token 检查。

GET 首先只读；发现该条记录已过期时再短事务收口，避免普通轮询争抢写锁。claim/renew/finish/submit/cancel 同样在取得锁后检查时间。完全没有活动进程时，过期收口发生在下一次访问；不声称有独立后台调度器。

worker 使用标准库 multiprocessing 的 spawn 子进程执行现有 ResearchAgent。父进程定期检查任务状态、续租，deadline 同时受数据库墙上时间和本次执行的 monotonic 剩余预算约束；子进程还有独立预算看门狗及父进程存活检查。取消、超时、失去租约或存储异常后，父进程 terminate，必要时 kill，然后 join。子进程不接收数据库连接，只输出私有临时 JSON 结果；避免 Pipe/Queue 大结果阻塞监督循环。子进程异常正文与 stdout/stderr 不输出。

默认检查周期至多 0.1 秒，数据库锁等待仍有最长 5 秒延迟，子进程 terminate 宽限 1 秒。只支持离线只读工具；终止计算不能撤销外部副作用。此设计依赖子进程隔离和同机 SQLite，不提供对不可信本地代码的沙箱。临时目录在正常退出时清理，父进程 SIGKILL 可能留下私有临时文件；不做未授权的历史数据清理。

monotonic 预算只在本次领取期间防止墙上时间回拨延长执行；跨服务重启仍依赖同机时钟，不宣称跨主机时钟一致性。子进程看门狗依赖 Python 线程能运行，若父进程被 SIGKILL 且子进程卡在永久持有 GIL 的 native 调用，需要外部进程管理器兜底；当前默认离线工具不包含这种调用。

最小替代方案是协作式取消，但阻塞工具无法保证及时返回，因此本轮选择进程隔离。代价是每任务 spawn 的启动成本和 corpus 快照复制；不引入第三方依赖或远端服务。

```text
HTTP 提交/取消/查询 → SQLite ← worker 监督循环 → 执行子进程 → ResearchAgent
```

## 修改与验收

修改 tasks.py、worker.py、api.py，更新 API/任务测试、真实 HTTP 验收脚本、README、AGENTS、包内容 allowlist 及设计说明。默认 300 秒/100 条是明确的本地策略常量，不增加跨进程可能不一致的启动配置。

验收覆盖：排队到期不执行、持续心跳也必须到期、跨接管预算不重置、到期写回拒绝、取消 pending/running/终态/不存在任务、取消与成功竞争、满额并发接纳只剩一个名额、队满重放、释放容量后可提交；阻塞子进程被回收并能继续下一任务；worker 异常退出后子进程自行退出。保留第一阶段进程竞争、两种崩溃窗口和旧 attempt 测试。真实 HTTP 验证 429/409/取消鉴权与重启终态。运行全套 pytest coverage≥90%、ruff、mypy、compileall、build、包审计和 diff 检查。

不删除数据库；停服务和 worker 可停止新版行为。schema 未变，但回退旧程序会丢失 deadline/取消执行回收/容量保障，因此不可把能打开数据库等同于行为兼容。

## 后续工程交付顺序

1. 会话与消息：在确定产品的多轮研究语义后，设计会话/消息/任务关联、顺序与分页合同；验收并发追加无丢失、重启历史可用、会话之间不会串上下文。涉及新增 schema，实施前单独提供具体表结构。
2. JobPrep：先围绕现有 analyze/Coordinator 定义提交、结果与材料版本边界，保护现有 JSON Store 和真实求职数据，再实施独立任务服务；验收重复提交、材料变化、重启恢复和失败不污染既有状态。不能把 Agent Lab 的实现算成 JobPrep 已完成。

本计划作为工程路线，不是本人能力认证；短问答不阻断已授权实现，理解验收集中到实际代码和故障复现。

## 第二阶段验收结果（2026-09-18）

- 全套 pytest：312 passed，覆盖率 90.48%；保留既有 Qdrant 连接关闭 ResourceWarning。默认覆盖率工具没有汇总 spawn 子进程内部行覆盖率，进程执行路径由真实进程行为断言验证。
- 已验证真实阻塞计算被取消/超时回收、忽略 SIGTERM 时升级到 kill、父 worker 被 SIGKILL 后子进程退出、墙上时间回拨期间 monotonic 预算生效，以及随后继续处理新任务。
- 五轮四进程同步启动/幂等提交/领取竞争通过；只剩一个容量名额时四进程竞争，只有一个提交成功；取消与完成竞争后终态稳定。
- `scripts/check_research_tasks_http.py` 通过：真实 loopback HTTP、独立 worker、重启保留结果、429、取消鉴权、幂等取消、取消释放容量。Provider 调用为 0。
- compileall、ruff、mypy、CLI help、git diff --check 通过；使用 `python -m build --outdir dist/research-tasks-stage2` 与 `python scripts/check_package_contents.py dist/research-tasks-stage2` 构建并审计两份归档。
- 第一轮全量回归出现一次初始化失败；补充初始化全程互斥和更严格的并发启动测试后，上述最终全套回归通过。没有把重跑本身当作修复。

部署观测已完成；会话与消息、JobPrep 持久任务服务化仍为后续阶段。本轮没有创建或迁移真实用户数据库，没有改动 JobPrep 历史状态结构，没有提交或推送。

## 第三阶段验收结果（2026-09-18）

- Compose 双服务真实运行通过：API 与 worker 使用同一专用 named volume，均以 UID/GID 10001 运行；`/state` 为 0700，SQLite 为 0600，API 只发布宿主 loopback。
- 停止 worker 时任务进入 pending 积压，重新启动后恢复完成；API 重启后历史结果保留，旧 session token 被拒绝。
- 真实 loopback HTTP 验收补充了受保护 metrics、容量/取消计数、worker claimed/finished 事件与输入脱敏检查；Provider 调用为 0。
- Agent Lab 全量 pytest：316 passed，覆盖率 90.46%；compileall、ruff、mypy、构建和两份归档内容审计通过。保留既有 Qdrant 未关闭连接 ResourceWarning。
