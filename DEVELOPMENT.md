# ApplyOps development workflow

日常修改先运行快速测试：

```bash
./scripts/test-fast.sh
```

涉及浏览器、提交安全边界或 MCP 流程时，再运行 smoke：

```bash
./scripts/test-fast.sh
./scripts/test-smoke.sh
```

准备合并或发布时运行完整检查：

```bash
./scripts/test-full.sh
```

三档测试分别覆盖：

- **fast**：Python core/API、状态机、ledger、answers、preferences/company policy 等，不启动 Chrome，也不构建前端。
- **smoke**：少量关键 Demo ATS 浏览器流程、Web console 流程和提交安全回归。
- **full**：全部 Python 测试、并发与浏览器测试、frontend Vitest/typecheck/build，以及 wheel 构建。

不要在每个小改动后自动跑 full suite；只有改动涉及对应边界时才提升测试档位。
