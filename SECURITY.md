# Security policy

## 范围

Agent Lab 是本地、默认离线的实验项目。默认运行不调用网络或真实 Provider；可选 live/serve 路径只在显式启用时工作。

## 报告问题

请不要在公开 issue 中粘贴密钥、私有语料、原始 trace、真实用户数据或完整本机路径。报告安全问题时，先移除敏感内容，并提供最小的合成复现步骤、受影响版本和预期/实际行为。

在尚未配置公开安全邮箱或安全公告渠道前，请通过项目维护者已确认的私下渠道提交；不要为了报告问题而创建公开 exploit。

## 设计边界

安全约束包括 fail-closed 的 corpus/path 校验、精确 citation、Prompt injection 防护、默认禁网、loopback 服务校验和发行包 allowlist。它们是工程约束，不是安全认证或生产 SLA。
