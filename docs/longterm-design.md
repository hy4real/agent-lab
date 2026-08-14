# Long-Term Memory Design

- 状态：v1.5 Controlled Memory 已实施并通过冻结离线契约评测（2026-08-14）
- 范围：显式 proposal/confirmation 生命周期、版本化本地状态、scope 隔离、BM25 召回、预算控制、legacy migration
- 非目标：模型自动抽取或写入、语义冲突裁决、向量记忆、远端服务、多租户 ACL、Research Citation Source

## 记录与生命周期

`MemoryRecord` 保存 kind、规范化正文、scope、status、origin、来源 message ID、时间戳、内容 hash 与稳定 ID。新事实通过 `propose()` 创建 candidate，只有显式 `confirm()` 后才能召回；`deactivate()` 保留历史但停止召回，不提供物理删除或原地正文更新。

`message` origin 必须携带来源 message ID；`manual` 和 `legacy_import` 不得伪造 message 来源。模型输出、召回块、工具观察与 Research 结果不会自动触发 proposal。兼容接口 `add()/retrieve()` 只代表显式 local-only baseline，不提供 scope 隔离语义。

## 存储

顶层 JSON 固定为 `schema_version=2`、单调递增 `revision` 与严格 `records`。mutation 在同路径 advisory lock 内重新加载状态并校验 `expected_revision`，再通过临时文件、fsync 和原子替换写入；目录、状态和锁文件权限分别为 `0700/0600/0600`。未知 schema、额外字段、损坏内容、hash/ID 篡改、锁失败与 revision 冲突全部 fail closed。

旧 `entries` schema 只能只读加载。`import_legacy()` 默认 dry-run；显式 apply 时先创建逐字节一致的 `0600` backup，再写入 v2。迁移不删除旧数据，backup 已存在时拒绝覆盖。

## 召回

检索使用纯 Python deterministic BM25，tokenizer 为小写拉丁词加单个 CJK 字符；只检索 exact scope 的 confirmed 记录，按 score 降序和稳定 ID 升序排序。自动召回最多 2 条、512 estimated tokens，完整记录装不下时跳过而不截断。

注入块明确标注为不可信历史笔记，禁止执行其中指令、改变权限或工具参数，也禁止作为事实和引用来源。短期状态已因单条超限消息突破自身预算时，自动召回关闭；正常完整上下文上限为短期预算加 512-token 显式额度。

## 主动搜索

ReAct 可显式启用 `memory_search`。该工具使用严格 JSON 参数、top_k 1..5、每次 1024 estimated tokens、每轮最多 2 次。正文只进入当轮 reasoning context；Trace records 只保存工具名、状态和结果数量。`plan_execute` 继续拒绝 memory context，Research Citation validator 不接受 MemoryRecord。

## 验证

`evals/controlled-memory-v1/` 固定 7 条合成记录、8 个查询、7 项 100% 门槛和 5 个状态 probe。canonical report 只证明冻结合成案例上的 expected recall、scope isolation、unconfirmed exclusion、预算、注入边界、确定性、迁移、损坏拒绝、CAS、并发写入与无自动反馈循环，不证明模型质量或生产能力。
