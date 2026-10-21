# 激活实例凭据配置升级

`credential_profile` 从包 manifest 移到激活实例，与 `instance_config` 并列。
同一包的多个实例可以选择不同账号或集群，包内容和摘要不受选择影响。
实现依据见 [设计](superpowers/specs/2026-10-21-activation-credentials-design.md)。

## 协调升级

1. 暂停新提交，等待执行中及等待认证的 Run 结束，停止 Observer、Driver 和 Slave 进程，
   备份 Observer/Slave PostgreSQL 数据库及 operator 凭据目录。容器及其持久状态继续保留。
2. 使用同一新版构建更新 Observer、Driver、Slave 和 `container-http-v1` 插件。
   各角色启动时由现有迁移入口执行 Alembic `007_activation_credentials`，再加载激活状态。
   不混用新 Observer 和旧 Worker/插件；现有远程调用封装必须支持新的命令字段。
3. 迁移将历史包 `body.credential_profile` 复制到已有激活记录，覆盖 Observer workspace
   激活、Run 内激活和 Slave 副本。已有字段（包括显式 null）保持原样。迁移不修改包 JSON、
   包摘要、GRIP URI、激活 revision 或 Run outcome。重启后 reconcile 使用保存的实例选择。
4. 分发 SDK 0.6.0 和对应 skill。重新描述 Slave，确认 `activate-package` 的输入 schema
   包含 `credential_profile`。用已有实例验证状态，并以两个新实例验证不同 profile 的挂载。
5. 恢复提交。若需回退，恢复升级前数据库及匹配的软件；不直接执行 downgrade，旧版本
   无法表达同包不同实例的凭据选择。SQLite 仅用于新建测试数据库，不提供旧部署数据迁移。

## 新包和调用方

新发布的包不允许 `body.credential_profile`，返回 `credential_profile_requires_activation`。
历史不可变包仍可读取、核验和执行；运行时不会再从其 body 选取 profile。
新激活历史包时也要显式提供需要的 profile，不能依赖旧字段的默认值。

当前模板版本为 Kubernetes v4、Outline v2、算力云 API v4、普通 SSH v4。
Kubernetes v3 引入实例级凭据，v4 修复 `vcjobs` 到 Volcano API 资源的映射。
算力云 SSH v3 使用浏览器认证，无固定 profile，版本保持不变。
按 [服务包发布流程](../capability_services/README.md) 使用真实镜像 digest 发布，
已有版本不可覆盖。重新发布不替换已有激活；原有资源的 URI 和连接可以继续保留。

```python
activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": kubernetes_package_uri,
    "instance_name": "beijing",
    "credential_profile": "k8s-bj",
    "idempotency_key": "activate-k8s-beijing",
})
```

profile 目录由 Slave operator 提前准备，选择名为 1–96 个 ASCII 字符，首字符为字母或
数字，其余允许字母、数字、点、下划线和连字符。目录只读挂载到 `/run/loom/credentials`。
凭据内容不进入 Observer content、包或输入参数；实例选择不出现在公开 DESCRIBE。
新实例省略/null 表示不挂载；已有实例省略/null 保留原选择。相同实例名不可改绑，
切换账号用新实例名，停止原实例依旧使用其已保存的选择。
