# 能力包资源效果与后续使用关系

**日期：** 2026-10-09  
**状态：** 设计中，尚未实现  
**适用范围：** capability package、operation descriptor、IoContract、外部 agent 能力发现和编排 preflight  
**遵循：** [spec 设计规范](../spec-design-guidance.md)

## 1. 问题

当前能力包能够声明操作名称、输入输出 schema、效果类别和权限，但不能让外部
agent 可靠地知道一个操作产生的资源如何继续使用。

对 Suanli VM 来说，`instances-run` 的结果只应被视为创建请求的结果。外部 agent
还需要知道：

```text
instances-run
  → 从结果提取 VM 身份
  → 用 instances-status 等待 VM 可用
  → 登记 VM resource
  → 激活 VM SSH capability package
  → 完成认证并等待 authenticated/ready
  → 执行远程命令
  → 终止 VM 并停用访问实例
```

这些关系目前主要存在于开发者文档和固定编排知识中，未成为外部 agent 可以查询、
验证和组合的 metadata。当前 Suanli 输出 schema 中的实例对象也没有明确声明
`InstanceId`、`State`、`Ip` 等字段，agent 无法仅依靠 package 和 contract 生成可靠
的编排程序。

## 2. 目标

本设计增加一套通用的资源效果 metadata，使能力操作可以声明：

1. 操作创建、更新或销毁哪一类资源；
2. 如何从输出中取得资源的稳定身份；
3. 如何调用另一个操作判断资源是否就绪；
4. 哪一种能力可以使用该资源；
5. 使用能力前是否必须完成认证；
6. 生命周期结束后使用哪个清理操作。

Suanli VM 是这套 metadata 的第一个实例。通用模型不包含 Suanli 分支，Suanli
包只提供具体的 operation ref、资源类型、字段路径和访问包引用。

## 3. 非目标

- 不在 Observer、Driver 或 Slave core 中增加 `if suanli` 之类的 provider 分支。
- 不让 metadata 直接执行网络请求、shell 命令或绕过现有 capability package。
- 不把完整 Python 编排程序写进 package manifest；metadata 只提供 agent 生成程序所需的语义和引用。
- 不把运行时镜像、端口、凭据 profile 或认证 callback 写入资源效果 metadata。
- 不由 metadata 取代 IoContract 的输入输出 schema 校验。

## 4. Metadata 的放置

### 4.1 Capability descriptor 是资源关系的规范位置

每个 capability export 已经通过 `capability_descriptor_ref` 指向该操作的描述。
资源效果属于操作语义，因此放在对应 operation descriptor 中，而不是放入
`container:http` 的 `body`。

descriptor 保持现有 content-addressed 身份，并增加可选的 `resource_effects` 字段：

```json
{
  "schema_version": "operation.v1",
  "resource_id": "loom://suanlicloud/instances-run",
  "name": "instances_run",
  "resource_effects": [
    {
      "effect": "create",
      "resource_type": "compute.vm",
      "identity_path": "result.Instances[].InstanceId",
      "readiness": {
        "operation_ref": {
          "resource_id": "loom://suanlicloud/instances-status",
          "version_or_digest": "<descriptor-digest>",
          "identity_criterion": "descriptor_digest"
        },
        "input_mapping": {
          "InstanceIds": "$resource.identity"
        },
        "conditions": [
          {"path": "State", "operator": "equals", "value": "RUNNING"},
          {"path": "Ip", "operator": "present"}
        ],
        "polling": {
          "max_attempts": 60,
          "interval_seconds": 5
        }
      },
      "access_requirements": [
        {
          "capability_kind": "compute.vm.remote_shell",
          "package_ref": {
            "resource_id": "capability-package://suanli-vm-ssh-http/v2",
            "version_or_digest": "<package-digest>"
          },
          "target_binding": "target_resource_ref",
          "instance_config_mapping": {
            "host": "Ip",
            "port": {"constant": 22},
            "user": {"constant": "root"}
          },
          "requires_authentication": true,
          "ready_state": "authenticated"
        }
      ],
      "cleanup": {
        "operation_ref": {
          "resource_id": "loom://suanlicloud/instances-terminate",
          "version_or_digest": "<descriptor-digest>",
          "identity_criterion": "descriptor_digest"
        },
        "input_mapping": {
          "InstanceIds": "$resource.identity"
        }
      }
    }
  ]
}
```

`resource_effects` 的字段语义是通用的；`compute.vm`、Suanli operation ref、
`InstanceId`、IP 字段和 `suanli-vm-ssh-http` 都是该包的具体声明值。

### 4.2 IoContract 只描述数据契约

`contracts/instances-run.io.json` 和其他 IoContract 文件继续只描述：

- 输入 schema 引用；
- 输出 schema 引用；
- 成功语义；
- 可选的确定性验证器。

不把跨操作、跨包的后续使用关系直接放进 IoContract。IoContract 的作用是判断
当前节点的 payload 和结果是否符合契约，resource effect 的作用是告诉 agent
如何组合多个节点。

### 4.3 Package manifest 不新增运行时 metadata

`package.example.json` 继续通过 capability export 引用 descriptor 和 IoContract。
不把 `resource_effects` 放入 `body`：`body` 是 `service + container:http/1` 的
镜像、端口、认证和网络配置，当前 package contract 对其使用严格的
`additionalProperties: false`。

第一版不增加顶层任意 `metadata` 字段，避免建立一套与 operation descriptor
重复的第二个语义来源。package 级别的角色和关系由其 exports 的 descriptor 汇总得到。

## 5. Suanli 输出契约补齐

资源 metadata 不能替代输出 schema。必须同步重写以下 schema，使 agent 能校验
metadata 中的路径确实存在：

### `instances-run-output.json`

每个实例至少声明：

```json
{
  "InstanceId": "string"
}
```

允许 provider 额外返回 `State`、`Ip`、`Ips` 等字段；首版编排所依赖的字段必须
具有明确类型和 required 语义。

### `instances-status-output.json`

每个状态项至少声明：

```json
{
  "InstanceId": "string",
  "State": "string",
  "Ip": "string|null",
  "Ips": ["string"]
}
```

实际 provider 字段名以 Suanli API 为准，但 schema 必须覆盖编排程序实际消费的
字段，不能继续使用空的 `properties` 对象。

## 6. 字段约束

### 6.1 `effect`

第一版允许：

- `create`
- `update`
- `delete`

Suanli `instances-run` 使用 `create`，`instances-terminate` 作为 `cleanup` 的
`delete` 操作。

### 6.2 `resource_type`

这是可发现的资源类别字符串，例如 `compute.vm`。它不承载 provider 的控制面
操作名称。provider 信息由 operation ref 和 package ref 体现。

### 6.3 `identity_path`

路径使用受限的 JSON 路径语法：

- `.` 访问对象字段；
- `[]` 展开数组成员；
- 不允许任意脚本、过滤表达式或网络请求。

`result.Instances[].InstanceId` 表示一个 create 操作可以产生多个资源。解析结果
必须是非空字符串列表；重复身份或缺少身份必须使编排节点失败并保留原始结果。

### 6.4 `conditions`

条件使用结构化谓词，不执行自由格式表达式。第一版支持：

- `equals`
- `not_equals`
- `present`
- `absent`

`path` 相对于 readiness operation 的单个结果项。状态值满足条件且资源身份匹配
时，资源才进入可使用状态。

### 6.5 `access_requirements`

每个访问要求必须包含：

- `capability_kind`：抽象能力类别；
- `package_ref`：当前 workspace 可解析的实际包引用；
- `target_binding`：首版固定为 `target_resource_ref`；
- `requires_authentication`；
- `ready_state`：认证完成并通过访问探测后的 activation 状态。

`instance_config_mapping` 只允许从已确认的资源状态读取字段或使用常量，不允许
metadata 指定任意宿主机路径、私钥、token、URL 或 SSH options。

## 7. 外部 agent 的发现和编排流程

外部 agent 获取能力包后，必须按以下顺序读取和校验信息：

1. 查询 package manifest 和 capability export；
2. 解析 capability descriptor，读取 `resource_effects`；
3. 解析 `instances-run` 和 `instances-status` 的 IoContract 及 output schema；
4. 校验 `operation_ref`、`package_ref` 和 schema 引用都属于当前 workspace 且 digest 精确匹配；
5. 根据 `identity_path` 从创建结果提取资源身份；
6. 按 `readiness` 生成有界轮询节点；
7. 登记 `ManagedResource`，使用 VM SSH 包作为 `package_ref`，使用 Suanli 包资源作为 `managed_by_ref`；
8. 按 `access_requirements` 创建绑定目标 VM 的具名 activation；
9. 如果 activation 返回 `auth_required`，暂停 Run，展示认证信息并等待用户完成认证；
10. 只有 activation 为 `ready`/`authenticated` 后，才生成 `exec` 节点；
11. 在成功、失败或取消路径执行 `cleanup`，避免遗留付费 VM。

Agent 不根据包名猜测以上步骤，也不把 `instances-run` 的 HTTP 成功当作 VM
已经可以 SSH 使用。

## 8. 公共可见性

外部 agent 必须能从公共 Observer 数据面获得 resource effect，而不能依赖本地
仓库 README 或内部 operation descriptor registry 的隐式知识。

因此 package 查询返回的 capability export 至少要满足以下一种方式：

1. 内联返回已验证的 operation descriptor metadata；或
2. 返回可由公共 ContentStore 解析的 content-addressed descriptor ref。

仅返回 `loom://...` 和 digest、但没有公共解析路径，不满足本 spec。解析失败时，
agent 必须将 capability 标记为 metadata 不完整，不能自行猜测编排关系。

## 9. 校验与一致性

Observer 在发布或解析 package 时校验：

- `resource_effects` 结构符合版本化 metadata schema；
- `identity_path` 能在 output schema 中找到；
- readiness operation 的输入 schema 能接受 `input_mapping` 产生的字段；
- access package 存在、workspace 一致且包含声明的 capability kind；
- access package 的 activation 配置字段与 `instance_config_mapping` 一致；
- cleanup operation 能接受资源身份；
- 所有 descriptor、package 和 content ref 的 digest 精确匹配。

资源 metadata 属于 descriptor 内容，因此会参与 descriptor digest；修改资源关系
必须发布新的 descriptor，并更新 package export 的 descriptor ref。修改输出 schema
或 IoContract 也必须更新 content digest 和 package identity。

## 10. 失败语义

- 创建请求结果不包含合法身份：节点失败，保留原始响应，不能自动重放 create。
- readiness 超过有界轮询：节点进入明确的资源未就绪失败状态；已有 VM 身份仍须进入 cleanup。
- access package 不可用或未认证：Run 进入等待认证或能力不可用状态，不伪造执行成功。
- SSH 探测失败：访问 activation 不得进入 ready；编排继续执行 cleanup。
- cleanup 失败：保留资源身份和 cleanup 错误，结果不得声称资源已释放。

## 11. 验收

### Metadata 和 schema

- `instances-run` descriptor 能被外部 agent 解析到 `resource_effects`。
- `identity_path` 可从一个和多个实例结果中提取稳定身份。
- output schema 能拒绝缺少 `InstanceId` 或状态字段类型错误的结果。
- readiness 条件只允许结构化谓词，不执行任意表达式。
- `suanli-vm-ssh-http` package ref、认证要求和 activation 配置映射可被发现。

### 编排行为

- agent 能根据 metadata 生成 create、status polling、resource registration、activation、auth wait 和 exec 节点。
- 没有 ready/authenticated 的 VM SSH activation 时，exec 节点不会被派发。
- 多个 VM 的身份、activation 和执行目标互不混淆。
- create 后任一步失败都保留 InstanceId，并执行 terminate cleanup。
- metadata 缺失或引用无法解析时，readiness 返回明确 blocker，而不是生成猜测的编排程序。

## 12. 交付顺序

1. 定义并校验 `resource_effects` metadata schema。
2. 补齐 Suanli instances-run/status 输出 schema。
3. 为 Suanli operation descriptors 增加资源效果、访问要求和清理声明。
4. 让公共 package discovery 解析并暴露 descriptor metadata。
5. 增加 observer/driver preflight 校验和外部 agent 编排指导。
6. 增加无真实云资源的 metadata、schema、preflight 和 cleanup 测试。
7. 在获得凭据和预算批准后，再执行真实 VM 的完整端到端验收。

本 spec 只定义 metadata、schema、发现和编排校验的修改方案；本阶段不修改实现、
不发布新镜像、不创建真实 VM，也不声称 Suanli VM 端到端链路已验收。
