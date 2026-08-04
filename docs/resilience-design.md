# Resilience Components Design

- 状态：实施中（Run 4；2026-07-31）
- 范围：弹性工程组件——持久任务队列（优先级/延迟/崩溃恢复）、异步 worker 池、LRU+TTL 缓存、限流/熔断/fallback 降级原语、受控离线压测
- 非目标：多进程/多主机分布式队列；真实并发 serving 基准；无限重试/消息重投；不表述生产可用性或实时并发能力

## 决策

1. 队列：追加式 JSONL journal（add/ack），启动时重放计算 pending = adds - acks；目录 `0700`、文件 `0600`；损坏日志 fail closed（`TaskQueueError`）。就绪顺序 = (run_at, priority, 插入序)，确定性可测。
2. worker 池：asyncio 并发处理就绪任务；失败任务计入 failed 并从队列移除（重试/重投由断路器层负责，避免无限循环）；统计字段 total/ok/failed。
3. 缓存：LRU 驱逐 + 可选 TTL（惰性过期），时钟注入保证确定性；统计 hits/misses/evictions/size。
4. 降级：`RateLimiter`（滑动窗口）、`CircuitBreaker`（closed → open → half_open，探活成功复位，失败可走 fallback）；时钟注入。
5. 压测：固定任务数经队列 + worker 池处理，只把计数作为确定性断言；耗时仅作诊断展示，报告显式标注"不作为实时并发能力结论"。
6. 所有组件单进程语义，注入时钟/处理器，默认离线、无网络、无密钥。

## 验证

- 测试覆盖：优先级/插入序、延迟调度、持久化与崩溃恢复、重复/损坏拒绝、worker 并发计数、失败计数、LRU 驱逐、TTL 过期、限流窗口、熔断三态与 fallback、压测计数确定性、demo 可复现。
- 默认离线；压测耗时不计入断言，报告声明边界。

## 后续

- 若用户授权真实环境评测，可在同一接口上运行多进程/真实负载并单独记录边界；本版不包含该结论。
