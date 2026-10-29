# 资源依赖 metadata 生产发布

2026-10-29 将功能提交 `dec1dd5` 和历史描述符兼容修复 `acf6885`
部署到现有生产环境。生产源码版本为 `acf68855d1da6057f38ff128bd8594a6c4e7b3e3`。
本记录随后提交，不改变已部署的应用源码。

## 部署范围

| 主机 | 已升级组件 |
| --- | --- |
| `9.0.16.66` | Observer、slave-a、Driver |
| `9.0.16.113` | slave-autopaper |

从各组件原有镜像离线构建，保留依赖环境与业务服务镜像。切换前保存 compose、
环境文件和镜像回退标签，并对 Observer 与两个 Slave 的 PostgreSQL 做了
`pg_dump -Fc` 备份及归档目录校验。业务数据未清空。

两个宿主机的发布目录为
`/opt/loom-production/releases/resource-dependencies-20261029-acf6885/`，
包含源码归档、构建日志、组件状态、备份和操作脚本。源码归档 SHA-256：
`fbac22c7ece80ff7f4cc4b14b7ad026a45ba3bb5e6991163733e4ef8a1deab2a`。

## 已应用的依赖

节点使用不可变的 `computing/slave/v2` 类型，关系由该类型的 schema 声明。

| 节点 | `metadata.dependencies` 指向 | 验证时状态 |
| --- | --- | --- |
| slave-a | `resources/hosts/pcs` | 可用 |
| slave-autopaper | `resources/vm/9.0.16.113`（autopaper-vm） | 可用 |
| slave-b | `resources/vm/demo2zy` | 不可用，租约过期 |

表中资源路径相对于 `grip://loom.local/workspaces/workspace-default/`。
主机 pcs 按已核验的 hostname 和系统 UUID 登记为 `computing/machine/v1`；
没有创建新的宿主机。在线节点通过部署配置提交 metadata，离线 slave-b 根据
已核验的原始部署记录仅补写 metadata，未改变租约、epoch、心跳或更新时间。
demo2zy 仍为 `terminated`。

发布了 `embedding-http/v2` 作为依赖声明示例，沿用线上 v1 的不可变镜像引用。
现有 autopapers 实例继续运行 v1，未因示例发布被替换。

## 验证

- 最终目录包含 1403 个资源；原有 1376 个资源 URI 全部保留，所有原有
  activation 的状态一致，其中 45 个原本期望运行的实例保持原状态。
- DESCRIBE 与 `list-resources` 返回一致的节点依赖；how-to-use 仅包含字段意义
  和必填声明，不包含实际宿主 URI。
- v2 示例的 check-package 对缺失、非法 service_ref 返回未就绪，对有效引用
  返回就绪；预检没有创建 activation。
- 生产 Observer 健康检查通过，execution/orchestration 均开启。
- 通过公开 GRIP 调用现有 embedding 实例的 info，返回
  `Qwen/Qwen3-Embedding-4B`、2560 维、cuda。
- 跨节点验证 Run `run-41f0735d5c4d` 完成：slave-a 返回 34，
  slave-autopaper 返回 46，slave-a 的远程汇总返回 80；三个节点均完成，
  持久化结果的摘要校验通过。验证后停用了本次创建的三个函数 activation；
  三个不可变验证包、内容及 Run 保留为执行证据。
- 四个应用容器的关键源文件 SHA-256 与提交内容一致。除已停用的 demo2zy-ssh
  残留容器被启动协调器清理外，原有业务容器 ID 和镜像均保持一致。
- 发布前历史包兼容回归 73 项通过；Docker 与其他功能回归见
  [实现验证记录](2026-10-29-resource-dependency-metadata-validation.md)。
  全仓库 Pyright 既有问题按用户要求暂不修改。

## 发布时处理的问题

历史 suanlicloud-http/v1、v2 描述符保留了旧 resource_type 形式。发现它们时
使用校验摘要后的历史读取，不再套用新包创作规则；新发布仍执行严格校验。
没有重写旧包或发明类型别名。

生产原有核心镜像缺少服务 CA，导致 Observer 转发业务调用时报 TLS 信任失败。
在保留的旧镜像中也复现了相同错误。将已有生产 CA 安装到系统及 certifi
信任库，并通过证书校验和真实 GRIP 调用复验；没有关闭 TLS 校验。
CA SHA-256 指纹为
`EC:62:1D:FA:30:EB:A2:A9:19:69:AF:DE:A2:F2:97:BF:B2:C6:D5:68:20:86:80:14:D7:1E:08:85:C9:EB:BA:11`。
最终镜像标签以 `resource-dependencies-20261029-acf6885-ca` 结尾。

slave-a 第一次切换时，demo2zy-ssh 残留容器清理触发其他容器变化保护，自动回退。
核验其所属 VM 已删除、activation 已停止且其他业务容器未变后，重试切换通过。

首次跨节点验证 `run-90d647c69719` 使用 180 秒执行预算，两个 map 完成后，
reduce 在 accepted 阶段触及预算，最终报 `execution_deadline_exceeded`。
保留该失败证据，复用相同函数包并使用 SDK 通常的 600 秒预算重跑，完整通过。

## 回退资料

各组件原始配置在发布目录 `backup/<role>/`，旧镜像标签为
`loom-production-<role>:before-resource-dependencies-20261029`。
若必须回退应用，可恢复相应 compose 后重建该组件；此次 schema 和资源记录为
追加式，不需要仅为应用回退恢复整库。整库备份只用于经过单独评估的数据恢复，
避免覆盖发布后的新数据。回退旧镜像也会带回缺失 CA 的既有问题。
