# 工作空间能力包直接上传设计

> 2026-10-20 术语更新：本文保留当时的设计或验收记录；现行能力包业务接口统一为 action，字段、命名和实例描述以 [action 设计](2026-10-20-grip-actions-and-activation-description-design.md) 为准。旧示例需按新契约重新生成后使用。

> 资源接口历史说明（2026-10-19）：本文记录当时设计；其中资源发现、登记、激活、停用、认证启动和 promote 连带激活的接口已由 [GRIP 资源设计](../specs/2026-10-19-grip-resource-invocation-design.md) 取代。当前使用方法见 [GRIP 指南](../../grip-resource-usage.md)。其余执行与持久化设计按各自适用范围保留。

**日期：** 2026-09-19
**状态：** 实现中

## 1. 目标

支持用户在没有 Run 的情况下，将已准备好的能力包注册到当前 Observer 绑定的工作空间，并供后续 Run 发现、绑定和激活。Run 内由 coding agent 生成的能力缺口仍沿用 `run_bound` candidate 流程。

## 2. 设计决策

### 2.1 两种来源，两个生命周期

| 来源 | scope | publication_state | 来源引用 |
| --- | --- | --- | --- |
| Run 内物化 | `run_bound` | `candidate` | 必须有 `source_run_ref` |
| 用户直接上传 | `workspace_reusable` | `published` | 不绑定 Run |

直接上传仍必须经过已有 package contract、export、IoContract 引用和 digest 校验；上传成功只注册包，不自动在任何 Slave 上启动容器或进程。激活继续使用现有 provision/deprovision 流程。

### 2.2 HTTP API

新增：

```http
POST /api/v1/capability-packages
```

请求体提交 runtime-neutral package manifest：`package_id`、`package_version`、`package_type`、`execution`、`capability_exports`、`body`，`package_digest` 可省略。Observer 为请求补充工作空间作用域、发布状态和上传 provenance，并按现有 JCS/digest 逻辑计算或校验 digest。

请求不接受 `scope`、`publication_state`、`source_run_ref` 或 `source_closure_version_ref`，避免调用方伪造生命周期或跨工作空间来源。响应返回完整 `CapabilityPackageVersion`。

已有接口保持语义：

- `GET /api/v1/capability-packages` 同时列出当前工作空间的 Run-bound 包和直接上传的 reusable 包；
- `POST .../{package_ref}/promote` 只处理 Run-bound candidate，已发布 reusable 包幂等返回；
- `promote` 的 `target_slaves` 仍可触发显式激活，上传本身不激活。

### 2.3 持久化与查找

新增 Observer 角色的 `workspace_capability_packages` 表，按 `(workspace_id, package_ref)` 保存完整不可变 manifest。它与 `runs.capability_packages` 分开，防止为直接上传包伪造 Run。表同时保存该包的 activation 状态和健康事件，使现有 fencing、幂等和 `/api/v1/capabilities` 投影可覆盖无 Run 包。

内存测试仓库使用等价的 workspace package 映射。包查找优先匹配当前 workspace 的 published reusable 包，并继续按 digest 精确校验；Run-bound 包仍只对来源 Run 可见。

### 2.4 模型变化

`CapabilityPackageVersion.source_run_ref`、`source_closure_version_ref` 和 `package_closure_version_ref` 改为可选；新增 `workspace_id` 可选字段。package digest 只覆盖既有 execution manifest，不覆盖这些生命周期字段，因此直接上传和 promotion 不改变 digest 规则。

## 3. 范围

本次实现包含直接上传 API、workspace package 持久化、查找/列表/废弃、activation health/fencing 投影和 API/数据库测试。不新增第二套 runtime 协议、上传二进制专用端点或自动调度策略；程序、Schema 和 IoContract 正文仍通过现有 `POST /api/v1/content` 上传并以 `ResourceRef` 引用。
