# MCUE 升级与已有资源迁移

本次版本将资源描述的 kind 替换为 mcue，SDK 版本为 0.5.0。服务端和调用方需要一起升级；
资源 URI、包内容、激活绑定和历史 Run 不改写。协议外层 reply/error 的 kind 保留。

`9.0.16.66` 已于 2026-10-20 按此流程完成升级，
见 [生产升级报告](superpowers/reports/2026-10-20-mcue-production-upgrade.md)。
以下步骤用于尚未迁移的部署。

1. 使用原版本停止将被替换的资源生产能力包实例，停止 Observer 和 Driver，保留数据库、ContentStore 和运行时状态备份。
2. 准备显式的资源映射文件，逐一列出已有领域资源 URI、类型定义 URI和可选初始公开属性。不从包名、IP 或 URI 路径推断类型。
3. 在新版本环境中使用原 Observer 的数据库和 ContentStore 配置执行迁移。
4. 启动新版本服务，更新调用方 SDK 和 skill，发布变更过 descriptor 的新包版本，再激活替代实例。
5. 验证旧资源 URI、绑定和状态保留，DESCRIBE 返回 MCUE 与属性更新动作；用新 SDK 验证业务调用。

已知两台 VM 的映射示例：

```json
{
  "grip://loom.local/workspaces/workspace-default/resources/vm/loom-v2-auth-3c474f5bb5": {
    "mcue_ref": "grip://loom.local/workspaces/workspace-default/mcue-types/computing/vm/v1",
    "attributes": {}
  },
  "grip://loom.local/workspaces/workspace-default/resources/vm/9.0.16.113": {
    "mcue_ref": "grip://loom.local/workspaces/workspace-default/mcue-types/computing/vm/v1",
    "attributes": {}
  }
}
```

部署时以数据库中的实际资源清单补全该文件，再执行：

```sh
python scripts/migrate_resource_mcue.py --mapping /path/to/resource-mcue-map.json
```

命令读取既有 `LOOM_DATABASE_URL`、`LOOM_WORKSPACE_ID`、`LOOM_GRIP_AUTHORITY` 和 ContentStore 环境配置，
应用 Alembic `006_mcue_attributes`，登记内置类型定义，然后原子回填映射中的旧资源。
映射遗漏旧资源、包含不存在的 URI、属性不符合 Schema 或与已迁移类型冲突时拒绝资源回填。
重复执行相同映射不会覆盖已经修改过的属性。空数据库可使用 `{}` 作为映射。
新增自定义类型的旧资源应先在定义注册表中准备对应的定义，再回填其引用。

`suanlicloud-http/v3` 的 instances-run effect 使用 computing/vm/v1 的 mcue_ref，
替换旧自由字符串 resource_type；SSH 访问包继续使用现有 v3。
旧包和历史描述符保持原始不可变内容，可按原 URI 读取；新发布入口只接受 MCUE 契约，
不会把旧字段转换成新字段。
