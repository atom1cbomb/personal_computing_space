# Grafana 会话轮换修复生产部署

部署目标：`9.0.16.66`，Observer：`http://9.0.16.66:8080`。
发布目录：`/opt/loom-production/releases/grafana-session-rotation-20261022-092319`。
对应 [设计](../specs/2026-10-21-grafana-session-rotation-design.md) 与
[本地验证](2026-10-21-grafana-session-rotation-validation.md)。

## 发布和迁移

发布 `grip://loom.local/workspaces/workspace-default/packages/grafana-http/v4`：

- 包摘要：`9d30609c5ec0447d5eadb42cf6f2b03f190c7c7b160e700c5e57fb78707baa67`。
- 镜像：`localhost:5000/loom/grafana-gateway@sha256:51fc9b9b86c27deb369f498a1722f341b3feeafa634434d867d375708fa99981`。
- 服务代码 SHA-256：`8e869dfe89caa49249ffb67234893e20157c44a03ebec3631ba43878cdbfd33c`。
- 源码归档 SHA-256：`027c47d6a406641aba5d21af756102f016319a3e089b5b76757ebd2b54ad995b`。
- skill 归档 SHA-256：`dd04d83c047731cddab65de3544118444bf6e0240e509c1d9070b61eb9306bd0`。

实例的会话卷先在生产主机私有目录备份，再复制到 v4 对应实例卷。经 Observer
激活后，三个实例均自动执行令牌轮换并获得新 Cookie，没有重新扫码。各自的数据源
action 验证成功后，通过 Observer 停止对应 v3 实例。

统一前缀为 `grip://loom.local/workspaces/workspace-default/`：

| 实例 | 新激活实例 | 稳定资源 |
|---|---|---|
| 南京 | `activations/grafana-http/v4/slave-a/nj` | `resources/grafana/nj` |
| 北京 | `activations/grafana-http/v4/slave-a/bj` | `resources/grafana/bj` |
| 环保园 | `activations/grafana-http/v4/slave-a/ict-bj` | `resources/grafana/ict-bj` |

激活 URI、包 URI 和摘要均取自 Observer 发布/激活响应。原 Grafana 资源保持
身份和集群关联，本地统一登录入口已切换到 Observer 返回的 v4 激活 URI。

## 真实轮换周期验收

南京 v4 激活于北京时间 09:26:13 自动轮换并持久化新会话，之后持续通过 Observer
观察同一 revision 的认证和就绪状态，并核对私有文件元数据以确认下一次真实轮换。

第一次观察于 09:28:00 捕获一次 `degraded/auth_failed`，随后不经过登录恢复。
同期容器内脱敏诊断的三次上游数据源检查均成功，耗时分别为 0.124、0.085、0.078 秒。
这次短暂探测故障的具体触发原因未确认；原始观察记录保存在私有部署审计中。
随后继续观察真实轮换，不把短暂故障包装为全程无抖动。

南京下一次实际 Cookie 更新发生在 09:36:20.381，距上一轮 606.66 秒。
09:36:46 的 Observer 观察确认仍为同一激活、revision=1、ready/authenticated。
后续观察共 13 次均为 ready/authenticated，私有会话仍为 0600，没有人工登录。

业务验收均通过 Observer 的能力包 action：

| 项目 | 结果 | Run |
|---|---|---|
| 南京 GPU 指标 | 7 条 GPU 监控记录，complete=true，无 warnings | `run-2ba82bbe3068` |
| 北京 GPU 指标 | 9 条 GPU 监控记录，complete=true，无 warnings | `run-50a378c5eb13` |
| 南京真实轮换后的 GPU 指标 | 7 条记录，complete=true，无 warnings | `run-3efdc39a7abf` |

三个实例的数据源列表均已成功读取。旧 v3 激活在 Observer 为 stopped，对应容器
由正常 stop 生命周期移除；原私有会话卷和备份保留。三个资源的公开属性及集群关联
与升级前一致，ready 绑定均为对应 v4 实例。

部署校验确认三个新容器使用指定不可变镜像和匹配源码；其他 13 个受检容器
（10 个业务能力容器及 Observer、Slave、Driver）ID、镜像和启动时间保持一致。
Observer execution/orchestration 均为 true。

## 运维材料

生产发布目录保存源代码、镜像构建和推送日志、不可变镜像引用及 skill 归档。
`backup/` 保留每个旧实例的私有状态卷归档；`before-private.json` 记录连接配置、
卷、镜像和其他容器的升级前状态，不能作为公开资源属性或上传到内容存储。
本地 `production/grafana-session-rotation-*.json` 保存发布状态、观察和验收结果，
均位于忽略目录。报告不包含会话 Cookie 或桥接 token。
