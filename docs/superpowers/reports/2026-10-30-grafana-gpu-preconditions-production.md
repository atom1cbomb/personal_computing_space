# Grafana GPU 查询前置条件与错误报告修复

2026-10-30，生产 Observer：`http://9.0.16.66:8080`。
已发布 `grafana-http/v6`，完成三个 Grafana 资源的切换。原来的 v5 包和失败 Run 保留。

## 问题与变更

`run-69e438c169b9`、`run-6e069f7031e9` 都只传了 `hostname`。
部署前的运维配置快照确认，三个 v5 实例均没有默认 `gpu_metric`。
显式传入经真实样本验证的映射后，同一个北京实例能够查询两个节点。

- 包的 `HOW_TO_USE.md`、DESCRIBE 的 action 描述、输入 schema、实例配置 schema
  现在共同说明：`gpu_metric` 只有在 activation 已配置默认映射时才可省略；显式值覆盖
  本次调用的默认值。标签名区分大小写，readiness/authentication 不证明存在默认映射。
- IO schema 校验器支持标准 `description` 注解，参数说明可以随不可变 schema 发布。
- container HTTP runtime 从最多 4096 字节的错误响应中读取符合约束的稳定错误码，
  保留 HTTP 状态；不公开原始响应、额外字段、headers 或凭据。
- Slave 将服务 HTTP 错误记录为结构化执行失败。Slave 和 Observer 不再用成功输出
  schema/validator 覆盖已经失败的执行原因。标准失败结果进入 `awaiting_decision/repair`，
  例如 `terminal_error={code:gpu_metric_not_configured, category:capability_service_http_error, http_status:400}`。
- `no_data` 仍只表示查询成功但没有匹配样本；配置、认证、上游错误保持失败语义。

三个环境分别通过原实例的 GPU Arena 和 Prometheus 实际样本验证了相同的默认映射：
指标 `DCGM_FI_DEV_GPU_UTIL`，节点标签 `hostname`，GPU index 标签 `gpu`，UUID 标签
`UUID`，单位 `percent`，数据源 UID `prometheus`。这些值写入各 activation 配置，
包代码没有硬编码生产指标默认值。

## 发布与切换

- 包：`grip://loom.local/workspaces/workspace-default/packages/grafana-http/v6`
- 包摘要：`d0836db704f8401072987a9c6029454204c98f625a21e97d1270b34177cad787`
- 镜像：`localhost:5000/loom/grafana-gateway@sha256:3ab88e70cabd6db97199ba442fb678acb33e26eb80af5d68a071411dc83e4625`
- 发布的诊断源码包含 `server.py`、`Dockerfile` 和 `HOW_TO_USE.md`；逐文件回读比对通过，
  镜像内 `server.py` 与发布源码的 SHA-256 一致。

新实例先单独创建、认证和验收，随后通过 Observer 停止旧 v5 实例。
所有 URI 使用 `grip://loom.local/workspaces/workspace-default/` 前缀：

| 资源 | 当前 activation | 最终状态 |
| --- | --- | --- |
| `resources/grafana/nj` | `activations/grafana-http/v6/slave-a/nj-v6` | ready/authenticated，revision 3 |
| `resources/grafana/ict-bj` | `activations/grafana-http/v6/slave-a/ict-bj-v6` | ready/authenticated，revision 3 |
| `resources/grafana/bj` | `activations/grafana-http/v6/slave-a/bj-v6` | ready/authenticated，revision 3 |

三个领域资源的属性、revision、集群关联与依赖均保持原值，每个资源现在恰有一个
ready 的 v6 activation。三个旧 v5 activation 均已 stopped。

Observer、Driver、slave-a 更新了匹配的平台代码，没有数据库迁移。运行中代码逐文件
摘要验证通过；Observer 的 skill/SDK 下载 manifest 与升级前完全一致，SDK 仍为 0.12.0。
其他 Slave 无需执行本次 Grafana 包；将 v6 部署到其他 Slave 前须升级其 IO schema 校验器。

## 验证

本地相关回归测试 **176 项通过**，覆盖 schema 注解、网关配置前提、错误响应边界、
插件协议、真实 HTTP 网关到 Run 的错误保留、成功输出验证及任务生命周期。
最终实例 schema 调整后，Grafana 发布与认证链路的 **3 项测试再次通过**；
`scripts/generate_service_contracts.py --check` 和 `git diff --check` 通过。

生产 **13 个验收 Run 全部完成**，JSON 内容摘要均验证通过，验收 Run 均已关闭。
调用使用独立安装的 SDK，从仓库外目录执行，任务与生命周期操作均经公开 Observer 接口。

| 场景 | 结果 | Run |
| --- | --- | --- |
| 南京，仅 hostname：vm-11-0-11-8-ubuntu | ok，2 张 GPU | `run-670a52be2d50` |
| 环保园，仅 hostname：vm-29-0-29-3-ubuntu | ok，4 张 GPU | `run-af8101c0f3c7` |
| 北京，仅 hostname：node-21 | ok，8 张 GPU | `run-d71bdbc5fb0c` |
| 北京，仅 hostname：vm-9-0-9-8-ubuntu | ok，1 张 GPU | `run-8de81d1b5b72` |
| 南京，不存在的节点 | no_data，空 samples | `run-9cfa3a58b3a2` |
| 环保园，不存在的节点 | no_data，空 samples | `run-9de3a8c8fc75` |
| 北京，不存在的节点 | no_data，空 samples | `run-820b6dbc3d6c` |
| 南京 GPU Arena describe / query | 均 ok | `run-4f18ae1b2a1a` / `run-d963984e3b43` |
| 环保园 GPU Arena describe / query | 均 ok | `run-825e04f04712` / `run-2bd937fa711f` |
| 北京 GPU Arena describe / query | 均 ok | `run-edfa42376af7` / `run-d992543eb56c` |

三个新实例的 DESCRIBE、输入 schema，以及它们引用的包的 `how-to-use` 均已回读，
确认默认映射前提、显式覆盖规则和错误语义可见。缺映射 HTTP 400 的错误透传由本地
完整执行链路测试验证；没有为该负例额外创建生产待决 Run。

## 部署中的情况与回滚

首次平台切换因容器清单差异自动回滚。核对部署前的 Observer 记录后确认，差异来自
Slave 正常清理的 4 个旧容器：普通 VM SSH v5、两个云 VM SSH v5、云资源包 v6，
它们的 desired_state/state 均已是 stopped。记录这 4 项后重新切换成功；其他正在使用
的业务容器 ID 和镜像保持不变。

新 Grafana 实例的浏览器认证会话已验证并持久化，但就绪投影曾停留在 pending_auth，
统一认证助手因此超时。通过公开 stop/activate 生命周期在同一个 activation URI 上
完成重启后，三个实例都复用已保存的会话恢复 ready/authenticated，未重新扫码。
认证门户最终均显示 completed。该就绪同步现象的根因尚未确认，本次没有声称修复它。

运维材料保存在忽略目录 `production/releases/2026-10-30-grafana-gpu-preconditions/`，
生产对应目录 `/opt/loom-production/releases/grafana-gpu-preconditions-20261030/`。
保留三个平台角色的旧镜像标签 `before-grafana-gpu-20261030`、原 compose 配置、
旧 Grafana 包/实例配置及状态卷。必要时先通过公开接口停止新实例、恢复旧实例，
再用发布目录的 `deploy_core.py rollback` 恢复平台代码。
