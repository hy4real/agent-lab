# Contributing

感谢你对 Agent Lab 的关注。这个仓库优先接受能被离线复核、边界清晰的小改动。

## 开始之前

- 阅读 AGENTS.md，先确认范围、数据安全和验证约束。
- 不提交密钥、私有语料、真实用户数据、原始 trace 或本机绝对路径。
- 新增评测 fixture、gold 或阈值时，必须提升对应 suite version，并说明原因。

## 本地验证

验证入口包括 pytest 覆盖率门槛、compileall、ruff、mypy、build、发行包审计与 git diff --check，详见 AGENTS.md。

默认测试必须离线运行。真实 Provider、网络、部署或其他外部副作用不属于普通贡献验证。
