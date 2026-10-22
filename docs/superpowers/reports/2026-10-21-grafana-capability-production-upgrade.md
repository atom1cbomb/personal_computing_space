# Grafana 能力包生产接入

> 后续状态：Grafana 已升级至 v3，三个实例均已完成统一登录及真实数据读取验证。
> 本文保留初次接入记录；当前生产状态和 GPU 实测见
> [统一认证生产升级记录](2026-10-21-unified-authentication-production-upgrade.md)。

日期：2026-10-21。生产：`9.0.16.66`，Observer：`http://9.0.16.66:8080`。
发布目录：`/opt/loom-production/releases/grafana-capability-20261021-185049`。
对应 [设计](../specs/2026-10-21-grafana-capability-design.md) 和
[验证](2026-10-21-grafana-capability-validation.md)。

## 已发布

当前包：`grip://loom.local/workspaces/workspace-default/packages/grafana-http/v2`。
Observer 返回的 package digest：
`1b8e45348b251b563606448ab71958a2f15decae936c8dc0c1542b4a9fb32f3c`。
镜像：`localhost:5000/loom/grafana-gateway@sha256:a42f1a0ff3a7a6802101988e96ae17ef10974350a2218289ec0c136a135c49af`。
已通过 Observer 发布 MCUE `computing/grafana/v1`，注册三个领域资源。
最初仅支持凭据文件的 v1 已不可变发布，作为历史保留；三个资源的 metadata.package
保留首次注册的 v1 来源，当前 callable offers 由绑定的 v2 激活提供。

所有 URI 共用 `grip://loom.local/workspaces/workspace-default/` 前缀：

| Grafana 资源 | Kubernetes 引用 | Volcano 引用 |
|---|---|---|
| resources/grafana/bj | activations/kubernetes-http/v4/slave-a/beijing | resources/volcano/beijing |
| resources/grafana/nj | activations/kubernetes-http/v4/slave-a/nanjing | resources/volcano/nanjing |
| resources/grafana/ict-bj | activations/kubernetes-http/v4/slave-a/beijing-ict | resources/volcano/beijing-ict |

## 运行时地址与认证前置条件

| 实例 | 公开 dashboard_url / browser_origin | 已验证的生产 api_origin | 认证方式 |
|---|---|---|---|
| bj | https://grafana-prod.bj.cs.ac.cn:7443 | https://grafana-prod.bj.cs.ac.cn | 独立浏览器扫码会话 |
| nj | https://grafana-prod.nj.cs.ac.cn:10062 | https://grafana-prod.nj.cs.ac.cn:10062 | 独立浏览器扫码会话 |
| ict-bj | https://grafana-prod.ictbj.cs.ac.cn:10062 | https://grafana-prod.ictbj.cs.ac.cn | 独立浏览器扫码会话 |

三个入口 `/api/health` 返回 200，Grafana 版本 13.2.4。公开与生产网络的
`/api/datasources` 均返回 401；公开 `/api/search` 和 `/api/frontend/settings`
同样需要登录。已确认 generic OAuth 登录入口，不复用 Kubernetes/Volcano 登录态。

v2 默认无需 credential_profile，使用本地可见浏览器完成扫码；原站处理 OAuth
state/PKCE/callback，现有一次性 Observer bridge 接收该上下文自己的 cookie，
网关验证后保存到各激活独立持久卷。未改写已登记的回调地址，未要求手工导出 Cookie。

目标激活为 `activations/grafana-http/v2/slave-a/{nj,bj,ict-bj}`。登录前为 pending_auth，
扫码并通过数据源验证后由 health 转为 ready。当前仍等待实际用户完成扫码，尚未读取
受保护的仪表盘或真实 GPU 利用率；本记录不代表生产查询验收成功。
完成后通过 Observer action 发现实际指标/标签、数据源 uid，查询指定节点。
GPU action 支持显式传入发现的映射，无需为登录后发现的指标重建激活。

未修改既有实例、数据库、Observer/Driver/Slave 进程或运行时插件。发布前记录了
24 个既有 VM/Volcano/激活资源和 10 个运行中的业务容器，完成阶段比对另存审计文件。

## 发布产物

工作区基准：`3c6990674ffe40bba797e95749cae7ca9a2a1b0a`。
源码快照包含本次未提交改动，593 个文件；归档 SHA-256：
`2c67738cde82c101fb72d3997f3ffcd0e47486b4bdd72b521140a77a0eea672b`。
源码不包含 production 密钥或私有审计记录。报告另存，不计入归档摘要。
更新后的 skill ZIP 包含 Grafana 指引及 SDK 0.6.0，SHA-256：
`8d2d44f90bfb07109d7ebdea140167d502b3d6b8febbc9327af15c53c9e17b6b`。
ZIP 同时包含 Grafana 本地扫码 client、依赖清单与说明。
