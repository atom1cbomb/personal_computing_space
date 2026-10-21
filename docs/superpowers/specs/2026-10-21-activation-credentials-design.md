# 激活实例的凭据配置

日期：2026-10-21。状态：已实现并验证，尚未部署生产。
遵循 [spec 设计原则](../spec-design-guidance.md)。
验证记录见 [测试报告](../reports/2026-10-21-activation-credentials-validation.md)。

## 设计结论

`credential_profile` 从能力包配置移到激活实例。相同包可以在同一 Slave 上创建多个实例，
分别引用 operator 配置的不同凭据目录。例如 Kubernetes 实例 `beijing` 使用 `k8s-bj`，
实例 `nanjing` 使用 `k8s-nj`；各自 kubeconfig 和 policy 决定集群、namespace 和认证方式。

`credential_profile` 是激活参数的独立可选字段，与 `instance_config` 并列。
它属于运行时挂载配置；`instance_config` 继续提供能力包所需的连接参数。
凭据文件内容、目录路径和 profile 选择不投影到资源 DESCRIBE 或 MCUE attributes。
MCUE 的公开属性策略保持独立。

## 接口与生命周期

Slave 的 GRIP `activate-package` 输入示例：

```json
{
  "package": "grip://loom.local/workspaces/workspace-default/packages/kubernetes-http/v3",
  "instance_name": "beijing",
  "credential_profile": "k8s-bj",
  "idempotency_key": "activate-k8s-beijing"
}
```

名称规则沿用现有 profile 规则：1–96 个 ASCII 字符，首字符为字母或数字，其余允许字母、
数字、点、下划线和连字符。不接收目录路径。新实例省略或传 null 表示不挂载凭据。
既有实例省略/null 保留已绑定 profile。相同实例名不可改绑 profile；变更账号配置使用
新实例名，避免幂等重试意外切换凭据或复用旧容器的认证状态。

Observer 在创建实例时验证、持久化 profile，并把它传入 provision/deprovision 命令。
Driver 的编排 `ctx.ensure_activation(..., credential_profile=...)` 支持同一字段，
并将它纳入 step 幂等比较。停止、重试、自动恢复和 Slave reconcile 使用持久化的选择。
Slave 的重复 provision 必须拒绝相同 revision/key 携带不同绑定的请求。
相同 digest 和实例名的不同包坐标可能复用容器，这种复用也必须保持相同 profile；
否则拒绝激活，改用新的实例名。

运行时仅从激活命令或持久化激活读取 profile。复用既有目录包含关系、符号链接检查和
只读挂载逻辑，挂载到 `/run/loom/credentials`。缺失目录在 Docker 修改前报错。
不同实例拥有独立容器和认证状态，包摘要不随凭据选择变化。

## 能力包与历史数据

当前模板移除 `body.credential_profile`，版本调整为 Kubernetes v3、Outline v2、
算力云 API v4、普通 SSH v4。算力云 SSH v3 通过实例认证取得凭据，无固定 profile，
继续使用 v3。新包发布、候选包物化和提升入口拒绝旧字段，报
`credential_profile_requires_activation`。公开 schema 和指南引导调用方使用激活字段。

本设计需要保持向后兼容的唯一部分是历史不可变包的读取和已激活服务的执行能力：保留旧字段的存储契约校验，
仅用于读取旧包、摘要核验和历史执行预检；旧字段不再用于运行时选择，也不接受新发布。
这项兼容不改写任何旧包的内容、版本或摘要。

Alembic `007_activation_credentials` 在 Observer 和 Slave 数据库中执行一次 JSON 数据回填：
从旧包的 profile 复制到已有激活记录（workspace 包、Run 内激活、Slave 副本）。
已有明确字段，包括 null，保持不变；迁移不修改包、资源 URI、激活 revision、Run outcome。
迁移应先于新版进程加载激活；SQLite 开发测试库使用新模型，不提供旧部署数据迁移。
由于旧版本不能表达同包不同实例的凭据绑定，降级应从升级前备份恢复。

## 验证与交付

- 同包同 Slave 两个实例选择不同 profile，得到不同只读挂载，包摘要相同。
- GRIP 激活字段经 Observer、Driver、Slave 传递完整；DESCRIBE 不公开 profile/连接配置。
- 非法名称、缺失目录及符号链接被拒绝；无 profile 的实例不挂载凭据。
- 相同实例改绑被拒绝；幂等重试、停止再启动、Slave 重启/reconcile 保留 profile。
- 编排确保激活的 profile 参与 step 幂等检查，并经过真实 sandbox 传递。
- PostgreSQL 真实迁移覆盖两种角色、历史 Run 和不覆盖已有选择，保留旧包摘要。
- 四个变更模板通过发布及真实 HTTP gateway 生命周期测试，所有 action 回归通过。
- 同步 SDK、skill、示例和部署说明，生成 SDK 0.6.0 分发包。

本次先完成代码和测试；生产发布使用此迁移与对应新包版本进行协调升级。
