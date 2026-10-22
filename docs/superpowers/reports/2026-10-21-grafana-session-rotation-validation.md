# Grafana 会话轮换修复验证

对应 [设计](../specs/2026-10-21-grafana-session-rotation-design.md)。

## 生产诊断

南京旧 grafana-http/v3 激活的会话文件存在、权限为 0600，最近写入为
2026-10-21 22:04:50 北京时间。上游 `/api/health` 返回版本 13.2.4，数据库正常；
使用该私有会话读取 `/api/datasources` 返回 401，结构化标记为
`messageId=session.token.rotate`，没有新的 Set-Cookie。
诊断未输出 Cookie、凭据正文或登录参数，未修改生产实例或会话文件。

Grafana [13.2.4 官方客户端](https://github.com/grafana/grafana/blob/v13.2.4/public/app/core/services/backend_srv.ts)
收到这个标记会调用 POST `/api/user/auth-tokens/rotate`，随后重试原请求。
[官方服务端](https://github.com/grafana/grafana/blob/v13.2.4/pkg/api/user_token.go)
在会话仍可轮换时返回新的会话 Cookie，真正失效/撤销时才返回 401。
旧能力包只被动接收成功响应的 Set-Cookie，没有发起正常轮换的流程。

## 修复与验证范围

HTTP 请求复用原有 httpx 客户端和实例 RLock。Cookie 请求遇到明确轮换标记时，
同源 POST 一次，保存新 Cookie 并重试原 GET 一次；查询参数和查询时间保持不变。
健康检查、业务读取和浏览器回调验证共用这个行为。普通登录失效仍需要认证，
网络/权限/轮换失败保留故障语义，不无限重试。Bearer 请求不尝试 Cookie 轮换。

网关回归覆盖 Cookie profile、浏览器私有会话、重启恢复、并发轮换、业务查询
重试、回调验证、过期/撤销、上游失败和重复挑战。Observer 集成回归覆盖真实
HTTP 网关、包发布、同激活 revision 的就绪状态、自动轮换后的 GPU 查询和脱敏。

验证命令与结果：

- `.venv/bin/python -m pytest tests/capability_services/test_grafana_gateway.py tests/capability_services/test_grip_services.py tests/contracts/test_http_package_contract.py -q`：101 passed，55.33 秒；警告来自已有 Python 3.14/pytest-asyncio 的事件循环弃用提示。
- `scripts/generate_service_contracts.py --check`、skill quick_validate、`git diff --check` 均通过。
- 重新构建 `dist/personal-computing-space.zip`，确认 skill 文件与归档逐字节一致，引用的本地文档链接有效。

包模板更新为 grafana-http/v4，能力包说明、统一认证说明和 skill 引用同步更新。
截至 2026-10-21 本地验证时，生产仍是 v3，此阶段仅诊断和完成本地修复。
2026-10-22 的生产升级另见
[部署验收记录](2026-10-22-grafana-session-rotation-production-upgrade.md)。
后续生产验收需迁移私有会话状态，确认真实轮换周期后仍为 ready/authenticated，
并通过 Observer 读取 GPU 指标。不能把本地回归视为生产验收。
