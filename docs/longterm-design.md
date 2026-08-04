# Long-Term Memory Design

- 状态：已实施；v0.9 安全标签已验证（2026-08-01）
- 范围：持久化的长期对话记忆——条目模型、原子落盘、确定性词法检索与上下文注入
- 非目标：语义/向量检索（词法 overlap 是明确 baseline，不是 RAG）；embedding 模型；分布式存储；并发或生产结论；不修改 Run 1 短期记忆语义

## 决策

1. 条目模型 `MemoryEntry(id, text, created_at)`；id 由文本 SHA-256 前缀确定性生成；相同文本去重（重复添加被拒绝）。
2. 持久化：JSON 文件原子写入（临时文件 + fsync + rename），目录 `0700`、文件 `0600`；损坏或不兼容 JSON 一律 fail closed（`LongTermMemoryError`）。
3. 检索：词法 term-overlap 打分——拉丁词（小写）+ 单个 CJK 字符作为 term；按命中数降序、插入序升序排序，取 top_k；命中为 0 不返回。显式声明为离线 baseline，不表述语义检索。
4. 注入：`inject_long_term()` 把检索结果渲染为一条明确标注“仅供规划，不可作为事实或引用来源”的不可信历史笔记；无命中则不注入。`build_full_context()` 组合顺序为：不可信长期笔记 → 短期记忆块 → 最近可见消息。
5. 时钟通过构造参数注入（默认 UTC ISO），demo 与测试使用固定时钟保证逐字节可复现。
6. 上限：`max_entries` 默认 200、单条文本 2000 字符；超限拒绝写入，不做静默淘汰。

## 验证

- 测试覆盖：持久化/重载、文件权限、检索确定性与 top_k、命中打分排序、CJK+拉丁混合、空记忆、重复拒绝、上限拒绝、损坏状态 fail-closed、全上下文组合顺序、demo 可复现。
- demo 使用固定条目、固定查询与固定时钟；连续两次运行逐字节一致。
- 默认离线：无 socket/HTTP、无 embedding、无密钥。

Research Agent 不自动写入长期记忆，也不允许长期条目成为 Citation Source。
