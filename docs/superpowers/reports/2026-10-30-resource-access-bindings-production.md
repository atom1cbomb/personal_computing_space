# 资源与访问包解耦：生产部署

2026-10-30，按用户要求将 [PR #5](https://github.com/yzs15/personal_computing_space/pull/5) 的合并提交 `278f4d29491fc32e1a1f5e0eb80cf65587f9ff27` 部署到生产。[Issue #4](https://github.com/yzs15/personal_computing_space/issues/4) 随合并关闭。

## 发布内容

Observer、Driver 使用同一源码快照，镜像标签为 `resource-access-bindings-20261030`。资源登记和领域资源不再携带包引用；访问包仍由 activation 引用，资源上的 activations 和业务 offers 继续动态投影。数据库升级到 `008_resource_access_bindings`。

服务镜像及本机独立 SDK 升级到 `loom-orchestration 0.11.0`，项目 skill 同步更新。Slave、业务网关、数据库和 MinIO 容器保持原实例，生产依赖和 CA 保留。除 Observer、Driver 外，同主机其余 22 个容器的 ID、镜像和状态未改变。

| 组件 | 部署镜像 ID |
| --- | --- |
| Observer | `sha256:59272fc71529210384349f189764c0f3451bf76324898956f13bcda26d5d4a20` |
| Driver | `sha256:d2261a5a7d0b79c5dc63b8a79b3ec0cd89e4d61c431b2b5a89d187aae7cf543a` |

源码归档 SHA-256：`85d571b5981e02a10ca8b9de9779a6732a6d6a64414c58f1b5510e1168e9a227`。

SDK wheel SHA-256：`24d6f4ad854cfaa3e84fe39293577769d993c9e3c6e4ebf3062fe126e97f14f4`。交付物为 `dist/loom_orchestration-0.11.0-py3-none-any.whl` 和 `dist/personal-computing-space.zip`；未发布到包注册表。

## 迁移与数据验证

合并前完成 620 项单元及组件回归，6 项跳过、13 项取消选择；另行完成 PostgreSQL 迁移测试、SDK/服务构建及相关类型检查。部署阶段基于生产现有镜像离线构建，分别验证 SDK 版本、领域模型和迁移文件。

先将生产数据库备份恢复到独立演练数据库，运行真实迁移。验证 13 条原资源 JSON 和属性修订号完整进入归档，在线资源仅移除 `package_ref`；90 个包及 510 条历史 Run 的整表内容摘要不变。演练数据库随后删除。

切换前没有 `queued`、`committed`、`running`、`cleaning_up` 或 `awaiting_auth` 的 Run。停止旧 Observer、Driver 写入后，再次检查在途任务，备份数据库及内容存储，然后启动新版本。迁移与归档在同一事务中完成，生产验证结果与演练一致。

| 备份 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `production-before-008.dump` | 42,533,501 | `d3fa051b93dc9541924297dcd06412cb1ec026254d8a6569928faf408e9683ea` |
| `content-store-before.tar.gz` | 9,019,870 | `bfe43e0411663a9d4c1a1d8d26171582f770ccd5ec475b5059bf3ebf4d62048d` |

数据库备份通过 `pg_restore --list` 检查。两份备份均已复制到本地并校验摘要，权限为 0600。

## 公开接口验收

验收从 `/tmp` 使用实际安装的独立 SDK，经公开 GRIP 执行。

| 检查 | 结果 |
| --- | --- |
| 资源迁移 | 13 条资源 URI、MCUE、属性、属性修订号、状态、管理者及激活绑定不变；领域资源不再返回 `metadata.package` |
| 登记契约 | 输入 Schema 不含 `package`/`package_ref`；对现有 VM 使用无包引用的相同登记成功，资源及属性修订号保持不变 |
| AutoPaper VM | 原 URI `grip://loom.local/workspaces/workspace-default/resources/vm/9.0.16.113`，属性修订号仍为 4，业务 offers 不变 |
| v5 访问 | 原 activation `grip://loom.local/workspaces/workspace-default/activations/vm-ssh-http/v5/slave-a/vm-9-0-16-113` 的认证和配置不变；`run-b9044bf5dac8` 执行只读验证命令，返回 `loom-resource-access-v5-ok`，退出码 0；冻结计划仍引用 v5 包 |
| Driver 编排 | 复用已有测试包，`run-6bed6b6d3042` 实际完成动态节点，返回 `{"value":42}` |
| 任务收尾 | 两个验收 Run 均已关闭；验收使用的 `grip-native-double/v1/slave-a/default` 函数实例恢复停止状态，修订号由 2 递增至 4 |
| 历史数据 | 原 90 个包摘要、510 条历史 Run 状态及 11 个待决任务保持不变；切换时 95 个 activation 描述不变，验收后仅测试函数实例修订号发生上述变化 |
| 编排调用方 | 读取生产中 18 个已发布 Python 编排包的源码，均未调用 `register_resource`，无需为本次参数移除发布替代版本 |
| 运行状态 | Observer、Driver 健康；重启计数均为 0；本轮启动和验收日志无 ERROR/Traceback |

业务资源无需重新创建；验收仅对现有 VM 重复提交相同登记以验证幂等性。业务实例没有重新认证，历史冻结计划没有改写。验收复用已有包，只增加两个已关闭的 Run。

## 运维证据与回退限制

本地私有资料位于 `production/releases/2026-10-30-resource-access-bindings/`；生产主机资料位于 `/opt/loom-production/releases/resource-access-bindings-20261030/`。包含发布清单、部署脚本、迁移演练、完整快照、公开接口回执、健康检查、配置及备份。凭据和完整私有输出未提交到仓库。

旧镜像保留为 `loom-production-{observer,driver}:before-resource-access-bindings-20261030`，原 Compose 和环境文件保存在生产发布目录的 `backup/` 下。本次涉及数据迁移，**不能使用上次发布的仅镜像回退脚本**。若需回退，应停止写入，评估迁移后的新增数据，再协调恢复应用、SDK、数据库及内容存储；不得直接以旧备份覆盖后续业务数据。完整约束见 [迁移说明](../../../migrations/alembic/README.md#008-resource-access-bindings)。
