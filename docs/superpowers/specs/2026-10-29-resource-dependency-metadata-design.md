# 由类型和能力包声明资源依赖

## 问题与目标

`slave-b` 的心跳来自 VM `demo2zy`，但公开节点描述没有承载关系。删除 VM 后，
只能从日志确认节点下线原因。本改动让资源作者声明依赖字段，并在公开 metadata
中提供实际引用；部署前检查声明和已知配置，资源登记时检查最终值。

## 契约

复用 MCUE `attributes_schema` 和包描述符的 `instance_config_schema_ref`，在表示
依赖 URI 的属性上增加 `"x-loom-dependency": true`。字段自己的 `description`
解释业务含义，JSON Schema 的 `required`、`minItems` 等约束决定是否必填。
字段可以是单个 URI 或 URI 数组，可以位于显式嵌套 object/array 中。依赖标记
仅支持显式 properties/items 路径；含依赖标记的条件分支、模式属性或引用定义
在发布时拒绝，避免扫描器漏掉关系。其他属性仍支持已有 JSON Schema 功能。

例如 computing/slave/v2 定义必填 `host_resource`，解释为承载节点的资源；其他
类型可采用不同名称、数量和说明。核心不枚举 hosted_on 等业务关系，也不根据
IP 或名称推测关联。URI 校验只确认合法资源命名，目标存在性和可用状态通过
后续 DESCRIBE 获取，不在静态检查里探测业务服务。

`DESCRIBE` 与 workspace 的 `list-resources` 动作使用同一目录投影：

- 已登记资源的真实值保存在 `metadata.attributes`。
- `metadata.dependencies` 是按声明提取的 `{path, resource, description}` 列表，
  不另行持久化；没有依赖声明时为 null，表示未知。
- 激活实例从包声明的配置字段中只投影依赖 URI，不公开其余私有配置。
- SDK inventory 保留 mcue_ref、attributes 和 dependencies，方便反向查找。
- `how-to-use` 返回字段释义和 dependency_fields，不复制实际 attributes 或
  运行时 relationships。具体值以当前 DESCRIBE / list-resources 为准。

协议方法保持 DESCRIBE / INVOKE。反向查找可扫描公开目录的 dependencies；本次
不新增协议方法或持久关系图。

## 写入路径与校验

### 节点部署

部署配置允许 Slave 设置 `resource_metadata = {mcue_ref, attributes}`，通过环境
配置传入注册请求。Observer 在签发租约前按所选 MCUE 验证，确认类型是
computing/slave，并把公开 metadata 存入已有 capabilities JSON。节点过期后
仍能读取这份 metadata；重注册更新为本次配置。未配置的节点继续显示未知依赖。
新 v2 类型要求 host_resource；已有不可变 v1 定义不覆盖。

### 能力包

创建动作的 `resource_effects[].attributes_mapping` 声明登记属性从哪里填写。
每个目标顶层属性对应 `{source, path}`，source 为 input/output/instance_config；
常量使用 `{source: constant, value: ...}`。路径为显式对象字段路径。
SDK 提供纯函数按映射生成 attributes，外部 agent 在创建成功后调用 register-resource。

发布/预检校验创建动作的必填属性都有映射，源字段存在、类型匹配，常量满足
目标属性 schema。输出才生成的值只校验声明，不能在部署时伪装已验证。

`check-package` 增加可选 instance_config，复用激活配置校验；省略时使用已有
实例配置，新实例使用空对象。检查依赖 URI 和来自 instance_config 的创建属性
映射。正式 activate/configure 在修改状态前执行同一校验，避免绕过预检。
资源 register/update 对最终 attributes 执行 schema 和依赖 URI 校验。

实例配置是独立配置 schema，使用现有 JSON Schema 2020-12 validator；业务
输入输出的 io.v1 子集保持其自身契约。静态检查不执行任意包代码。
配置 schema 通过 `application/vnd.loom.instance-config-schema+json` 上传；
ServicePackage 根据描述符的配置 schema 引用自动选择该类型，避免与业务 IO
schema 的上传校验混用。embedding-http/v2 示例声明上游 service_ref 依赖。

## 存储与发布

复用 MCUE 内容存储、workspace resource JSON、agent capabilities 和已有
activation.instance_config，不新增数据库表。旧类型和已发布包不被原地修改；
新类型、带新声明的包和更新后的服务端需配套发布。代码实现不自动切换生产部署。

## 验证

覆盖：多种字段名、数组/嵌套依赖、非法标记与 URI；节点注册、租约过期及持久化
后依赖可见；DESCRIBE/list-resources 一致；how-to-use 只有说明；创建映射缺失或
错误被拒绝；check-package 无副作用，activate/configure 使用同一约束；实例
配置只公开标记字段；旧无声明资源返回未知。示例展示 agent 无需日志即可发现
`slave-b -> demo2zy`。
