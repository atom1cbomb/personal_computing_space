# 客户端共享登录会话生产发布

2026-10-30 按用户“合并部署”要求完成。共享会话实现提交 `359c1fd` 与主分支
Overleaf 提交 `6ee4082` 合并，发布代码为 `3d83838`，已推送 `origin/main`。
合并保留 Overleaf 的点号 Cookie、pending 回调、WebSocket 修复及原回归覆盖。

## 交付与部署

- SDK **0.9.3** 已构建为独立 wheel 和 `personal-computing-space.zip`，更新到 `dist/`，
  并安装至本机 `/root/loom_v2/.venv`。从 checkout 外确认实际加载的是安装后的 SDK。
- 统一登录助手使用安装后的 `loom-auth`，入口为 `http://localhost:3000/`，
  包含从生产目录发现的 11 个运行中、支持交互认证的资源。
- Observer 部署镜像为 `loom-production-observer:client-login-20261030`，镜像 ID：
  `sha256:79d729136179215739785d65f18dbe35d22005b96060fe384a6ba2acf5a5e89b`。
  基于现有生产镜像离线构建，保留依赖、CA、配置和数据；没有数据库结构迁移。
- Driver 自动恢复连接，未重启 Driver、Slave 或业务容器。现有能力包未重新发布。

发布源码归档来自 `git archive 3d83838`，SHA-256：
`fc1f8a01ae75cfa545c1010a79aacc40a7c1d5642c3fdead1f72f6645cedcf6e`。
SDK wheel SHA-256：`667cd6609475194acc3201f8757c7cddc9ed7f578ecf810628cb07eff6d0b249`。
安装的 personal-computing-space skill 指向仓库目录，其认证说明随合并同步更新。

## 验证结果

合并后认证、GRIP、包契约、Overleaf、HTTP runtime 和 WebSocket 回归 **180 passed**；
独立 SDK 安装与技能归档检查 **12 passed**。客户端源码与 wheel 内容逐字节匹配。
新镜像验证确认 Observer 不再投影历史 idp，同时保留 Overleaf pending 回调。

生产切换前没有 queued/running/cleaning_up 的 Run，11 个 awaiting_decision 的
历史任务保持原状态，未代替用户作决策。发布后检查：

| 检查 | 结果 |
|---|---|
| Observer healthz、execution、orchestration | 正常 |
| 原有 1447 个资源 URI | 全部保留 |
| 94 个激活 | state、revision、desired_state、认证状态保持不变 |
| 88 个能力包 | 包摘要全部保持不变 |
| Observer 以外的 23 个容器 | 容器 ID 和镜像保持不变 |
| Overleaf 真实只读调用 | 项目列表成功，39 个项目 |
| 本机登录入口 | HTTP 200，11 个实例，SDK 0.9.3 |

共享登录行为由真实 Chromium 配合模拟上游验证。本次未触发已有生产资源的
重新认证，未把只读调用成功等同于真实账号的跨资源 SSO 验收。
升级后首次登录会建立新的共享档案；旧的按 IdP 保存的档案保留但不自动合并。
资源自身要求的授权或设备码确认仍需正常完成。

## 运维与回退

本机私有发布资料在 `production/releases/2026-10-30-client-managed-login/`，
包括源码、SDK、登录助手 PID/参数、发布前后状态和验证结果。
生产主机备份与部署脚本在 `/opt/loom-production/releases/client-login-20261030/`，
含原 compose、环境文件、镜像标签和通过 `pg_restore --list` 校验的数据库备份。

Observer 可通过该目录的 `deploy_core.py rollback observer` 恢复原镜像和配置。
无结构迁移，应用回退无需整库恢复。客户端如需回退，可使用保留的 Overleaf 发布
目录中的 SDK 0.9.2 wheel，停止本次助手后恢复旧启动方式；不要删除浏览器档案。
