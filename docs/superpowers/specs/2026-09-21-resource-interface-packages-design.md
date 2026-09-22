# 资源与接口能力包

状态：实现；遵循 `../spec-design-guidance.md`。

## 1. 模型

资源的使用能力与管理能力分开。VM 引用描述其使用方法的包，管理者引用算礼云资源；启停、删除属于算礼云资源的包，不属于 VM 使用包。Slave 也可以用同一资源模型描述，不把资源等同于 Worker。

复用 CapabilityPackageVersion 的坐标、版本、capability_exports、descriptor、IoContract、发布和内容寻址。新增接口包形态：`package_type=interface, execution={kind:"none",version:"1"}, body=null`，export 的 `runtime_binding={}`。`none/1` 是保留的接口标记，不对应 runtime provider，也不参与激活；真正表示“没有可执行实现”的字段是 `body=null`。它只有接口，没有可执行实现；不伪造 runtime provider。现有可执行包仍必须满足已注册三元组契约，不能用 null 绕过 image/program 校验。接口包不支持 provision、dispatch 或后台运行健康状态。

资源正文只包含：

```text
Resource {
  resource_ref: ResourceRef
  package_ref: ResourceRef
  managed_by_ref: ResourceRef | null
  state: string
}
```

`package_ref` 精确引用描述该资源使用方式的已发布 workspace 包（接口包或实现包）；管理者是同 workspace 的另一个资源，不复制其 package、descriptor、IoContract 或 Slave 地址。查询包的 exports 后，调用时才选择单个 descriptor。state 是登记方报告的领域状态，不等同于运行时确认可执行。资源身份稳定，不使用状态 JSON 的 content digest 作为 VM 身份。

不新增 ResourceDescriptor、资源类型 registry、独立 schema 引用或重复管理能力列表。workspace 由 Observer 请求上下文和数据库分区确定，不接受正文指定 owner_scope。实例连接参数及凭据绑定等 SSH 细节另议，本次不放入公共资源正文。

## 2. 登记与查询

`POST /api/v1/resources` 登记上述正文。包必须已发布且当前 workspace 可见；package_ref 的 digest 必须与包一致。managed_by_ref 必须已经登记在当前 workspace，不能自引用。重复登记相同内容幂等；同一资源改变包或管理者返回 conflict，不能借重试偷偷重绑。已有资源允许更新 state。管理关系创建时只能指向既有资源，且禁止重绑，因此不需要另设图引擎防环。

`GET /api/v1/resources` 列出本 workspace 已登记资源；`GET /api/v1/resources/{resource_id:path}` 返回单个资源。`GET /api/v1/capability-packages/{package_ref:path}` 返回包；descriptor、IoContract、schema 沿现有 ContentStore API 获取。资源查询不复制契约，不新增旁路内容上传工具。

登记表示用户/调用方提供了资源事实，不是外部资源存在性或权限证言。仅查询到接口不代表调用者获授权，也不代表 Loom 已有对应执行器。

SQLAlchemy 新增 workspace_resources 表，workspace 和 resource_id 联合主键；用 Alembic 新 revision 演进已部署数据库，不能修改发布 baseline。资源状态与包定义分开持久化，重启后仍能解析相同引用。

## 3. 执行边界

接口包上传、查询、内容验证、digest 校验和发布复用现有包流程；实现为空不等于空 IoContract。接口包进入 activation/readiness/dynamic-node/Slave 执行入口时返回 `capability_package_not_executable`，不得先生成 activation 或调用 runtime。发布接口包不需要安装；普通包的执行规则保持不变。

## 4. 首版交付边界与验收

本次交付通用接口包和资源登记/发现链路，不自动解析第三方结果登记资源。Suanli 创建结果投影、Slave/activation 自动资源投影、VM SSH 包具体接口与执行实现是后续接入工作；现有 Slave API 保持不变，不声称生产 VM 已自动入册。

后续接入见 [命名能力包实例与 Suanli VM 访问](2026-09-21-named-activations-and-suanli-vm-access-design.md)：Suanli VM 将直接引用可执行的 SSH-over-HTTP 使用包，每台 VM 对应一个独立访问实例；纯接口包仍保留，不重复创建同义的 VM 接口包。

验收：接口包可上传、重读并保持 digest；可从已登记 VM 找到使用包和 IoContract，从 managed_by_ref 找到云资源包；不存在或跨 workspace 引用拒绝；重复登记幂等、状态更新与重绑冲突可测；SQL 重启读取通过；接口包不能激活或当可执行节点使用；普通 service/function 包回归通过。
