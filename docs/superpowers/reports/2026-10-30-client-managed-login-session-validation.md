# 客户端共享登录会话验证

实现位于独立 worktree `/root/.codex/worktrees/2119/loom_v2`，分支
`feat/client-managed-login-session`，基于已提交的 `main`（`b6977b2`）。
原工作区的 Overleaf、streams 等未提交改动未带入或修改。
设计见 [客户端管理登录会话](../specs/2026-10-30-client-managed-login-session-design.md)。

## 结果

统一助手不再依据包的 `idp` 划分档案。所有选定资源使用同一用户的私有浏览器档案，
实际登录跳转和浏览器域规则决定会话复用。客户端补存会话型 Cookie，认证排队发生在
DESCRIBE/auth-start 之前。服务 Cookie 验证失败只定向清理该 Cookie。
现有包无需重发；Observer 停止投影 `idp` 是元数据清理，不是客户端复用的前提。

将新的真实 Chromium 回归用例装载到原 `HEAD` 的客户端实现中，复现两次登录提示；
同一用例在新实现中只出现一次。最终相关回归 **107 passed**，无跳过：

```sh
python -m pytest \
  tests/contracts/test_auth_sso.py \
  tests/contracts/test_auth_client.py \
  tests/contracts/test_auth_browser.py \
  tests/contracts/test_http_package_contract.py \
  tests/api/test_browser_auth.py \
  tests/api/test_grip.py \
  tests/capability_services/test_service_contract_artifacts.py \
  -q --disable-warnings
```

真实浏览器配合模拟上游覆盖：未声明/错误声明 IdP、三种交互方式、会话型与持久
Cookie、重开助手档案、同一资源动态更换 IdP、不同域隔离、禁用复用，以及服务
Cookie 失效后保留 IdP 和其他资源会话。另覆盖并发等待、取消、排队后读取新 revision、
匿名 Cookie pending、档案不可用、快照损坏、权限和删除同步。

`generate_service_contracts.py --check`、修改文件的 Python 编译、CLI help 和
`git diff --check` 均通过。pytest 输出为现有 Python 3.14/pytest-asyncio 弃用警告。

## 使用边界

上述为初次 worktree 验证记录；随后已按用户要求合并和部署，见
[生产发布记录](2026-10-30-client-managed-login-session-production.md)。
本次未对线上真实账号完成升级后的整组资源交互登录验收。
升级后的首次登录会建立新档案，旧的按 IdP 保存的档案保留但不自动合并。
上游要求的授权确认、设备码确认、撤销或真正过期仍按正常认证流程处理。
