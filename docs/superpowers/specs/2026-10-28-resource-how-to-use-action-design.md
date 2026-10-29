# 资源 how-to-use Action 设计

## 问题

GRIP DESCRIBE 能列出资源的 `offers`、`managed_by`、`package` 和
`activations`，但 agent 仍需要自行推断这些字段如何组合使用。VM 资源尤其容易
把使用它的 SSH activation、管理云资源和算礼云管理 activation 混为一谈。

## 目标

每个公开资源提供一个平台内建的只读 `how-to-use` action。agent 调用后可以得到：

- 当前资源的用途摘要和一段适合直接阅读的 Markdown；
- 资源公开属性及其 MCUE schema 字段说明；
- 当前 DESCRIBE 中可用的业务 action、输入输出 schema 和交互模式；
- `managed_by`、`package`、`activations` 等稳定引用及其关系含义；
- 生成这些说明所依据的资源、MCUE 和 action 引用。
- 如果目标是 capability package，还返回包声明的 how-to-use 文档。

返回值必须同时适合程序消费和人阅读。Markdown 是辅助说明，不能替代精确 URI、
schema 和状态字段。

## 接口

新增平台 action：

```text
grip://<authority>/workspaces/<workspace>/actions/how-to-use/v1
```

输入为空对象，交互模式为 `request-reply`。所有目录资源的 DESCRIBE 默认提供该
action；action/schema/content 目录项也提供同一 action，使 agent 不需要先判断资源
种类才能查询使用说明。

输出固定为：

```json
{
  "resource": "<resource URI>",
  "mcue": "<category/subtype>",
  "summary": "<short summary>",
  "usage_markdown": "<short human-readable instructions>",
  "attributes": {"<public field>": "<value>"},
  "attribute_help": [{
    "name": "<field>",
    "type": "string",
    "required": false,
    "description": "<schema description>"
  }],
  "relationships": [{
    "kind": "managed_by",
    "resource": "<resource URI>",
    "description": "<relationship meaning>"
  }],
  "actions": [{
    "action": "<action URI>",
    "mode": "request-reply",
    "input_schema": "<schema URI>",
    "output_schema": "<schema URI>",
    "available": true,
    "description": "<action description>"
  }],
  "documentation": {
    "content_ref": "<Markdown content URI>",
    "markdown": "<package-authored usage document>"
  },
  "sources": ["<resource or definition URI>"]
}
```

`attributes` 只返回资源 DESCRIBE 已公开的 attributes。`attribute_help` 从当前
MCUE definition 的 `attributes_schema` 生成，保留字段的类型、是否必需和描述；不
把实例配置、凭据或私钥投影进结果。`actions` 排除 `how-to-use` 自身，避免递归，
并按实际 DESCRIBE offers 返回；`available` 只表示该 offer 当前是否可调用。

关系的 `resource` 只能是已经在当前 workspace 中出现的 GRIP URI。`managed_by` 是
稳定的资源到资源关系；activation 仍作为 `activations` 关系单独列出。平台不会把
activation URI 改写成 `managed_by`。

Capability package 可以通过顶层 `how_to_use_ref` 声明一个 workspace 内的 Markdown
content ref。`ServicePackage.publish` 会自动把包目录下的 `README.md` 上传为这个
不可变引用；直接注册包时也可以显式提供该字段。Observer 在包的 `how-to-use`
结果中返回 `documentation`，并将包导出的 action declarations 列为需要先激活
package 的声明动作。README 不进入运行时 `body`，不会被挂载到服务容器。

## 生成规则

Observer 在调用时使用目标的当前 DESCRIBE entry 生成结果，不缓存文档正文。资源
属性来自 entry metadata，MCUE definition 通过其精确 `mcue_ref` 读取，action 列表
来自 entry offers。资源关系按以下顺序生成：`managed_by`、`package`、每个
`activation`；缺失关系不输出伪造的 URI。

Markdown 只概括当前实际状态和调用顺序：先引用目标资源，再列出可用 action，若
资源有 activation 则说明应使用已列出的 activation；它不包含 host、user、port、
credential profile、token 或其他运行时秘密。

## 安全和兼容边界

这是 Observer 本地生成的 request-reply 读操作，不经过 Driver、Slave 或能力包，
不会创建 Run、改变 activation revision 或修改资源。现有 action、schema 和资源
URI 保持不变，只在 offers 中增加一个平台 action。返回内容只能使用公开描述和
不可变 schema/descriptor 引用。

## 验收

1. 任一已登记资源的 DESCRIBE 包含 `how-to-use`，且输入输出 schema 可通过 GRIP
   `read` 读取。
2. 调用资源的 `how-to-use` 返回属性字段说明、关系和当前 action offers；自定义
   MCUE 字段的 schema description 可见。
3. activation、package、schema 和 content 目录项也能调用该 action。
4. 调用不会创建 Run，不改变资源 attributes revision 或 activation revision。
5. 输出不包含 activation instance_config 或 credential profile。
