# 能力包 action 术语与激活资源描述

日期：2026-10-20。状态：已实现并验证。遵循 `../spec-design-guidance.md`，不保留旧字段别名。

## 用户模型

通过 Slave 资源的 activate-package 动作激活已发布能力包后，Observer 自动提供激活实例资源，返回其 URI、状态、revision。实例资源由既有激活记录投影，不新增第二份注册记录。仅手动启动容器不等于向 Loom 激活资源。run-package 是执行一次动作，返回 Run 资源；持续使用服务包应显式激活实例。

DESCRIBE 返回当前调用者可见的资源说明，以及该资源的动作契约。实例 metadata 包含包、宿主、实例名、状态、revision、绑定目标、全部已声明的业务 actions。每个业务 action 包含名称、描述、描述符与 IoContract 引用、输入输出 Schema 引用、权限与副作用、可用状态和不可用原因。`arguments_schema` / `result_schema` 引用业务输入输出 Schema，`input_schema` / `output_schema` 引用 GRIP 调用包装和 Run 回执 Schema；包资源的 `metadata.actions` 提供相同的动作声明。管理动作通过 offers 发布。Schema 正文和引用对象按 GRIP 继续描述/读取，不将凭据、实例配置、内部容器地址混入资源说明。

metadata.actions 表达包声明了哪些业务动作；offers 表达当前可调用的动作。认证未完成、激活失败、停止时保留全部业务动作说明，但 available=false，并不给出业务调用 binding。ready/running 时业务 actions 全部出现在 offers。动作可用性最终仍由执行时的 revision/状态校验裁决。

## 统一术语

- 能力包导出：ActionExport / action_exports。
- 动作描述：action.v1、application/vnd.loom.action-descriptor+json、action_descriptor_ref、get/register/validate_action_descriptor。
- 动作引用：action_ref，清理动作使用 cleanup_action_ref；源身份位于 GRIP actions 路径。
- Slave、Worker、运行时和能力服务使用 action/actions；网关业务响应使用 `{action, result}`。
- 异步动作返回 `{run: <Run URI>}`，不使用 action 指代执行记录。
- 任务计划的 patch.ops 是修改指令集合，数据库迁移中的历史列名属于历史结构；它们不代表能力包业务动作，不机械修改。当前 Slave attempt 的动作列迁移为 action。

类型、SDK、API、Worker 消息、运行时插件、五个服务包、生成器、示例、脚本、文档和测试一起迁移。IoContract 和 descriptor 内容变化后重新生成全部摘要。旧包和服务镜像需按新契约重新生成、构建并以新包版本发布；不转换旧包、旧 Run/Closure JSON 内容。数据库 migration 005 只将 Slave attempt 的历史列重命名并保留值。历史 spec/验收记录描述当时版本，以本设计和当前使用指南为现行入口。

## 验证

1. 实际发布并激活算力云包，得到一个可描述的实例 URI；其 metadata.actions 精确包含全部 12 个业务动作及说明，ready 时 offers 包含这些动作。
2. pending_auth/stopped 状态仍保留全部动作说明，禁止执行业务动作；凭据和内部运行地址不出现在描述中。
3. 五包共 32 个业务动作经发布、发现、调用、Run 查询和停止验证，所有字段使用新术语。
4. 新数据库及既有数据库列迁移通过；生成器 --check、SDK/server wheel 和全仓测试通过。

验证结果见 [验收记录](../reports/2026-10-20-grip-actions-and-activation-description-validation.md)。
