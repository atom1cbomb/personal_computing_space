# 版本记录

## 0.2.0 — 2026-11-04

配套客户端：`loom-orchestration 0.15.0`。

- 新增 `cs101-http/v2` 成绩分析能力包：支持配置 HTTPS 服务地址、查询试卷与成绩、整卷统计及学生分析，共五个只读动作。网关只访问配置的 origin 和固定 API 白名单。
- 新增 `browser-storage` 认证，支持从业务站点指定的 localStorage 键取得候选凭据，由网关验证后保存私有认证状态。
- 手动登录和自动复用会话均使用有界面的 Chromium，复用私有浏览器档案；无显示环境时明确失败。
- 更新平台架构、认证和能力包使用文档，补充相关契约及集成测试。
- 收录 [Issue #9 的认证插件职责边界设计](docs/superpowers/specs/2026-11-03-auth-plugin-boundary-design.md)。独立 AuthHost、认证插件热注册和通用凭据交付仍是设计方案，尚未实现。

### 升级与验证

使用 `browser-storage` 需要匹配的 Observer、HTTP runtime 和 SDK 0.15.0；仅升级客户端不足以启用该认证方式。用户机器还需安装 SDK 的 `auth` extra、Chromium，并提供图形显示环境。本版本不涉及数据库迁移。

相关契约、Observer API、能力包和 HTTP runtime 回归已通过：808 项通过、16 项跳过。发布构建及 MCP／Codex 协议回归另有 41 项通过。CS101 手动 token 登录及有界面会话复用已在测试站点验证；IAAA SSO 尚未验证。本次源码提交和版本标签不更新已部署实例。
