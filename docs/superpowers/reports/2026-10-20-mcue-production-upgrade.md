# MCUE 生产升级

日期：2026-10-20。目标：`9.0.16.66`，Observer：`http://9.0.16.66:8080`。
发布目录：`/opt/loom-production/releases/mcue-20261020-115804`。

## 发布内容

Observer、slave-a、Driver 已升级到 MCUE 实现。数据库迁移至 `006_mcue_attributes`，
登记 10 个内置 MCUE 定义，并按显式映射回填两台已有 VM。保留原数据库、内容存储、
资源 URI、来源包、激活绑定、认证状态及历史 Run。

使用当前工作区源码快照构建镜像，包含此前的 SSH 限制调整和本次 MCUE 修改。
基准提交为 `117a13fc019ce99efcaec7c2f3848b2fd75ad721`；快照包含 512 个文件，
SHA-256 为 `dd74d591d7c16aaa4e739019cee839b97cf64ab80d8dcfc7a115fa44ca1adf03`。
源码归档保存在发布目录的 `source.tar.gz`。此次部署未提交或推送 Git。

新版 `suanlicloud-http/v3` 已通过 Observer 发布并在 slave-a 激活，状态 `ready`、revision 1。
其 `instances-run` 描述符引用 `computing/vm/v1`，访问依赖为 `suanli-vm-ssh-http/v3`。
沿用既有算力云运行镜像；旧 `suanlicloud-http/v2` 激活已停止，revision 2，
原包及不可变描述符继续保留。SSH v3 激活保持运行，未重建已有 VM。

包 URI：`grip://loom.local/workspaces/workspace-default/packages/suanlicloud-http/v3`。
Observer 返回的包摘要：`97499b22a54f990c54540fc8a425a4b8de155f4020a03b4d8d70cb13245275f8`。

新版 skill 分发包已归档到发布目录，内含 SDK `0.5.0` wheel；文档与仓库源码逐文件一致。
分发包 SHA-256：`247cfcc32cbcfd150623e78829d93c62035f8a0c2ea9d71848f30c84d7197f82`。
调用方须使用 MCUE 契约和对应 SDK；不再接受资源级 `kind` 及旧目录过滤字段。

## 生产验证

所有资源和业务验证均通过公开 Observer 接口完成。SSH 到部署主机仅用于基础设施部署、
备份及数据库迁移，没有绕过 Observer 直接连接目标 VM。

| 检查 | 结果 |
|---|---|
| Observer、slave-a、Driver 健康 | 全部正常；Driver epoch 12，execution/orchestration 均可用 |
| 数据库版本 | Observer、Slave 均为 `006_mcue_attributes` |
| 目录及 DESCRIBE | 验证时的 219 个资源全部可描述，资源字段使用 mcue，类型引用均可解析 |
| MCUE 定义 | 10 个定义均可读取，目录中的类型与其定义一致 |
| 历史 Run | 11 个历史 Run 的公开读取结果与升级前逐项完全一致 |
| 两台已有 VM | 原 URI、state、package、managed_by、SSH 激活绑定均保留 |
| 属性修改 | 两台 VM 均验证合并、无变化更新、旧修订冲突、非法属性拒绝及 null 删除 |
| 修改与执行的隔离 | 测试标签撤回，attributes 恢复 `{}`，attributes_revision 为 3；SSH 激活仍为 revision 1 |
| 认证与运行时文件 | 升级前 9 个文件的校验和全部不变 |
| SSH 执行限制 | worker 3720 秒、capability 3660 秒、运行时响应上限 256 MiB，保留既有扩容设置 |
| 算力云查询 | 新 v3 实例查询成功，原云 VM 仍为 running |

通过各 VM 资源直接调用其已发现的 `exec`，输出校验标记、`root` 和预期主机名，退出码均为 0：

| VM URI 后缀 | 验证 Run |
|---|---|
| `resources/vm/9.0.16.113` | `run-0dd3bffbae83` |
| `resources/vm/loom-v2-auth-3c474f5bb5` | `run-53322f5f5df3` |

算力云 v3 查询 Run：`run-9073970a68ea`，状态 `completed`。
查询确认实例 `9a93d1b1-76ac-41fc-a2fc-f754fbe742fb` 仍运行。
未在生产执行新建或销毁云 VM；资源创建描述符及依赖已验证，创建、登记和清理链路的执行验证见
[本地验证报告](2026-10-20-mcue-resource-attributes-validation.md)。

最终目录共 222 个资源，其中 2 台 VM、1 个 Slave、9 个能力包、6 个激活、
14 个 Run、10 个 MCUE 定义；其余为 workspace、action 和 schema。
6 个激活中 3 个 ready、3 个 stopped。

## 备份与审计

发布目录的 `backup/` 为仅 root 可访问的备份，包含：

- Observer、Slave 的 PostgreSQL 自定义格式 dump，已通过 pg_restore 清单读取检查。
- 原角色配置、secrets 和源码归档，以及原角色 source 目录。
- 暂停 MinIO 后生成的内容存储快照。
- 运行时凭据、持久化认证状态及 Driver 工作区归档。
- 旧核心镜像 ID、保留标签及文件校验和清单；压缩归档均已检查可读取。

旧镜像保留标签为 `before-mcue-20261020-115804`。
需要回退时应先停止写入，按同一备份点恢复角色源码/配置、数据库和运行时状态，
再切回这些镜像；不能仅降级服务镜像而继续使用 MCUE 数据库。
备份时旧算力云 v2 实例已经停止，回退后应通过 Observer 按实际激活 revision 恢复该实例。

发布目录同时保存资源映射、迁移结果、升级前快照、验证结果和本报告。
工作区的忽略目录 `production/` 保存对应的 `mcue-*.json` 审计文件。
