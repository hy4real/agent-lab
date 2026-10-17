# 本地后端部署与诊断

## 合同与实施计划

本阶段将已有任务服务组成 API + 独立 worker 运行单元，新增受保护的诊断接口、脱敏事件日志和关闭/恢复验收。沿用 schema v1，不做数据迁移；每个 worker 一次监督一个执行子进程，多 worker 的领取由同一 SQLite 文件协调。只使用本地文件系统，不适用于网络共享盘或多机部署。

新增 `GET /v1/research/task-metrics`，在单个只读事务快照内聚合状态计数、错误码计数、未结束任务数、容量余量、过期 deadline 数、过期租约数、最老待执行任务年龄。`storage_status=ok` 只说明读查询通过，不说明磁盘可写或 worker 活着。聚合接口不扫描正文、不返回 ID 或材料；错误码通过固定白名单折叠，未知值统一为 other。过期状态在任务访问或 worker 领取时收口，诊断快照会显式展示尚未收口的数量。

入口生命周期与 worker 生命周期记录 JSON 事件。允许字段为 event/task_id/attempt/status/error_code/queue_wait_ms/duration_ms；每次领取的 queue_wait_ms 表示自提交到本次领取的耗时，接管包含前次执行时间，不能当作纯排队延迟。duration_ms 使用 monotonic，表示本次 worker 处理耗时。worker_id 和 request_key 可由用户指定，因此不记录。日志输出到 stderr，查询/提交正文与结果不打印。错误路径只用稳定码；日志写入失败不改变任务提交结果。

worker CLI 对 SIGTERM 做退出处理，让监督代码回收子进程，未提交的任务等待租约恢复；不把正常停机标成任务成功。SIGKILL 仍由子进程守护检查和租约接管兜底。CLI 在主线程安装信号处理并在退出时恢复原 handler。

## Docker Compose

`compose.yaml` 使用同一构建镜像运行 api/worker，两个服务都以 UID/GID 10001 启动。镜像预建 0700、属于该用户的 `/state`，专用 named volume 首次挂载继承目录所有权。语料只读挂载 `./example/corpus`，只发布 `127.0.0.1:8000`；worker 没有监听端口。API 有 /health 存活探针；它不宣称 worker 正常。

```bash
docker compose up --build -d
docker compose ps
docker compose logs --tail=50 worker
docker compose restart worker
docker compose stop
docker compose start
```

停止/启动保留 named volume；不使用 `down -v`，不自动删除数据。需要换语料时调整只读挂载，并同时重启 API 与 worker；排队任务仍按旧语料指纹校验。

镜像仍保留旧 API ENTRYPOINT 供已有 docker run 用法使用，Compose 显式覆盖入口以启动 worker。Docker build context 继续按现有 allowlist，不包含数据库、真实资料、日志或其他仓库。

会话 token 的既有 CLI 启动输出仍是获取本地凭证的渠道；不要分享原始 API 启动日志。新增结构化事件不包含凭证。生产凭证分发及日志接入需要独立设计，本阶段不修改密钥/token 配置。

## 排障路径

| 现象 | 先检查 | 判定边界 |
| --- | --- | --- |
| 429 | metrics 的 outstanding/capacity，worker 事件 | 队列满不等于数据库故障 |
| 503 | 稳定 task_store_unavailable、volume 权限和占锁进程 | 只读 metrics 正常不证明写锁可获取 |
| pending 累积 | oldest_pending_age_ms、worker 容器状态和日志 | 数据库无 worker heartbeat 表，不伪造存活判断 |
| running 不结束 | expired_leases、overdue_tasks、attempt | 先区分 worker 失联和仍在预算内运行 |
| corpus_mismatch | API/worker 的 corpus 挂载是否一致 | 不自动换输入重跑 |
| cancelled/deadline_exceeded | 同 task_id/attempt 的 worker_finished 事件 | DB 终态先确定，进程回收有短暂延迟 |

## 验收

测试聚合快照、错误白名单、诊断鉴权、日志不含问题/request_key/路径/异常正文、数据库异常和 SIGTERM 处理。真实 loopback HTTP 脚本覆盖重启、积压计数、执行事件、取消和容量释放。Docker 守护进程可用时构建并验证非 root 双服务、volume 重启持久化、worker 停止期间积压及恢复；不可用时只报告 compose config 静态验证，不能声称容器验收通过。
