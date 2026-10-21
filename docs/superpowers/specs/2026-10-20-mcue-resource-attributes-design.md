# MCUE 资源类型与扩展属性

日期：2026-10-20。状态：已实现，并完成本地验证及 9.0.16.66 生产部署验证。
生产记录见 [MCUE 生产升级报告](../reports/2026-10-20-mcue-production-upgrade.md)。
遵循 [spec 设计原则](../spec-design-guidance.md)，协议依据 `/root/computility_named_resource/grip协议整理v2.md`。

## 1. 设计结论

引入 **MCUE（Multipurpose Computing Utility Extension）**，用 `computing/vm` 这类标签标识资源的领域类型。
每个类型定义自己公开的扩展属性及其 Schema。`display_name`、`description`、`labels`、CPU、内存、系统、磁盘等都属于扩展属性，通用资源模型不逐个定义这些字段。

GRIP 继续只有 DESCRIBE 和 INVOKE。将 `resource.kind` 直接替换为 `resource.mcue`，在 metadata.mcue_ref 中引用有版本的定义；类型定义本身也是具有稳定 GRIP URI 的资源。
原有 `package`、`managed_by`、`state` 保留为平台维护的字段，扩展值统一进入 `attributes`，避免混在同一层。
公开哪些扩展属性由对应 MCUE 定义决定，平台不按 host、user、port 等字段名统一禁止公开。凭据一般不进入公开属性；激活配置也不会自动投影为资源属性。

设计前的实现把上述平台字段投影到 DESCRIBE.metadata，但 ManagedResource 只有 `package_ref`、`managed_by_ref`、`state`。本次实现补上类型引用、属性值和持久化更新路径。

## 2. 三种身份各自表达什么

| 概念 | 示例 | 含义 |
|---|---|---|
| 资源 URI | `grip://loom.local/workspaces/workspace-default/resources/vm/dev-vm-01` | 具体是哪一个资源；修改属性不改变它 |
| resource.mcue | `computing/vm` | 资源的领域类型；由 MCUE 定义决定公开属性的含义 |
| MCUE 定义引用 | `grip://loom.local/workspaces/workspace-default/mcue-types/computing/vm/v1` | 明确使用哪一版属性契约 |

`computing/vm` 是分类标签，不是资源 URI；有版本的类型定义才是可 DESCRIBE、read 的 GRIP 资源。
mcue 使用小写 `category/subtype`；MCUE 是术语，JSON 字段统一写为小写 `mcue`。版本由定义 URI 显式标识，不写成可变的 latest。首版不引入类型继承。
当前 VM 的 `kind: "resource"` 改为 `mcue: "computing/vm"`；服务端从 MCUE 定义读取 mcue，不要求客户端重复提交类型字符串。
这是对现有 GRIP 资源描述字段的直接修改，需要同步协议文档、服务端、SDK、Schema、示例和 skills。删除资源级 kind，不保留别名，不同时输出两个字段。
目录输入从 `{kind: ...}` 改为 `{mcue: ...}`，SDK 从 `resources(kind=...)` 改为 `resources(mcue=...)`；不再接受旧过滤参数。

替换范围是资源类型字段。GRIP 消息外层的 `kind: "reply"` / `"error"` / `"stream-open"` 表示消息种类，继续保留；
执行器的 execution.kind、capability_kinds、Kubernetes 清单的 kind 和 JSON Schema 的 type 也各自保留原语义。

MCUE 不决定执行动作。VM 能调用哪些 action，仍由实际绑定的 ready activation 和 offers 决定。
现有 `compute.vm.remote_shell` 等 capability_kinds 表示动作能力，不改名为 MCUE 类型。
MCUE 也不复用 ContentStore 的 media_type：前者解释资源的语义属性，后者仍用于内容的编码和解析。

## 3. DESCRIBE 的目标结构

以下只展示拟议 DESCRIBE 的 resource 部分，属性值为示例数据。
完整返回仍包含 offers，沿用现有动作及 binding 结构，并加入下文的属性更新动作。

```json
{
  "resource": {
    "uri": "grip://loom.local/workspaces/workspace-default/resources/vm/dev-vm-01",
    "mcue": "computing/vm",
    "metadata": {
      "mcue_ref": "grip://loom.local/workspaces/workspace-default/mcue-types/computing/vm/v1",
      "package": "grip://loom.local/workspaces/workspace-default/packages/vm-ssh-http/v3",
      "managed_by": null,
      "state": "running",
      "activations": [
        "grip://loom.local/workspaces/workspace-default/activations/vm-ssh-http/v3/slave-a/dev-vm-01"
      ],
      "attributes_revision": 3,
      "attributes": {
        "display_name": "开发测试机",
        "description": "用于项目开发与验证",
        "labels": {
          "project": "loom",
          "environment": "development",
          "owner": "alice"
        },
        "cpu_cores": 4,
        "memory_bytes": 8589934592,
        "os": {"name": "Debian", "version": "12"},
        "disks": [{"name": "system", "size_bytes": 53687091200}]
      }
    }
  }
}
```

| metadata 字段 | 维护方式 |
|---|---|
| mcue_ref | 保存不可变类型定义引用，resource.mcue 从该定义读取 |
| package | 可选的来源或接入能力包引用，不承担 MCUE 类型定义职责 |
| managed_by | 可选的管理资源 URI，通过资源管理路径维护 |
| state | 生命周期状态，由登记、执行及生命周期路径维护 |
| activations | Observer 根据真实绑定关系计算 |
| attributes_revision | 扩展属性及其 MCUE 定义绑定的修改计数 |
| attributes | 按 MCUE Schema 校验的公开扩展属性 |

通用资源不再要求通过能力包推断类型。手工登记的资源可以先有 MCUE 描述，之后再绑定访问能力包。
package 保留登记来源，因此访问包升级不要求改写资源来源；当前动作以 activations/offers 为准。
首版采用 `display_name` 与 `labels` 作为 computing/vm 的标准字段，不同时引入 name、tags 同义字段。
其他类型可以按自身需要定义属性，通用资源代码不为 VM 名称、CPU 或磁盘增加专用分支。

## 4. 类型定义与 computing/vm/v1

类型定义包括 `mcue`、`version`、说明文本和 `attributes_schema`。其中 mcue 是该定义所描述的分类标签，例如 computing/vm；
定义资源自身的 `resource.mcue` 则为 grip/mcue-type。Schema 随定义一起发布，作为不可变定义的一部分；首版采用自包含 JSON Schema 2020-12，可使用本地 `$defs`。

复用已引入的 jsonschema Draft202012Validator，不为 MCUE 另写校验器。MCUE 属性契约与动作的 io.v1 是不同用途，不因此扩大可执行 IoContract 的 Schema 白名单。

computing/vm/v1 定义以下属性：

| 属性 | 类型与约束 |
|---|---|
| display_name | 非空字符串，最长 256 字符；仅改变显示名称 |
| description | 字符串，最长 4096 字符 |
| labels | 字符串到字符串的映射，键由使用方定义，例如 project、environment、owner |
| cpu_cores | 正整数，表示分配给该 VM 的 vCPU 数量 |
| memory_bytes | 正整数，分配的内存字节数 |
| os | 对象，name 必填，version 可选，均为字符串 |
| disks | 数组；每项含非空 name 和正整数 size_bytes |

顶层及 os、磁盘对象只接受定义过的字段；labels 接受自定义标签键。
所有顶层属性都可选：资源刚登记时允许 attributes 为 `{}`，未知硬件信息保持缺省，之后按实际采集结果补充。
标签 owner 是描述信息，不作为授权主体或所有权证明。

哪些连接或运行时信息可公开，由所引用的 MCUE 定义决定。首版 computing/vm/v1 采用上表的属性集；
需要公开 host、user、port 或运行环境信息时，可以在新版本定义中明确其字段、用途与约束。通用校验器只按该定义校验，不内置这些字段名的黑名单。
例如某类型明确允许 host、port，则显式写入的对应描述属性可以由 DESCRIBE 返回；未声明的字段仍按该类型 Schema 处理。
凭据一般不应设计为公开属性；类型通常只描述认证方式或非敏感状态，实际密钥、密码、令牌仍通过认证与运行时凭据渠道提供。
attributes 是主动登记的公开描述，instance_config 是实际执行配置。即使二者有同名字段也不自动同步，DESCRIBE 不自动展开 instance_config 或凭据文件。
扩展属性更新只修改描述，不执行 SSH、修改硬件配置或调整访问绑定。

同一 `(workspace, mcue, version)` 发布后不能改写。新增结构化属性或改变约束时发布新定义，如 computing/vm/v2；
已有资源继续使用原定义，避免发布一次类型就改变所有现有资源的解释。

## 5. 通过 GRIP 发布、登记和更新

以下均为应用 action，URI 由 Observer 发布并通过 DESCRIBE 发现，不增加 GRIP 核心方法。

| target | action | 行为 |
|---|---|---|
| workspace | publish-mcue-type | 校验并发布不可变类型定义，返回权威 GRIP URI |
| MCUE 类型定义 | read | 返回类型、版本和 attributes_schema |
| workspace | register-resource | 指定 mcue_ref 和初始 attributes，登记稳定资源 URI |
| 已登记资源 | update-attributes | 校验并持久化扩展属性修改，返回新 attributes_revision |
| workspace | list-resources | 使用 mcue 过滤，例如 mcue=computing/vm，覆盖该类型的不同定义版本 |

publish-mcue-type 输入包含 mcue、version、description 和 attributes_schema。Observer 检查 Schema，保存规范化定义，并返回命名资源 URI；重复发布相同定义返回原 URI，内容不一致则发生版本冲突。
定义通过 ContentStore 保存，注册表记录 `(workspace, mcue, version)` 到不可变内容引用的关系。客户端不计算或覆盖服务端摘要。
服务初始化时通过相同发布逻辑登记仓库携带的 computing/vm/v1；自定义类型使用同一入口。

新资源的 register-resource 业务输入示例：

```json
{
  "resource_id": "grip://loom.local/workspaces/workspace-default/resources/vm/dev-vm-01",
  "mcue_ref": "grip://loom.local/workspaces/workspace-default/mcue-types/computing/vm/v1",
  "package": "grip://loom.local/workspaces/workspace-default/packages/vm-ssh-http/v3",
  "state": "running",
  "attributes": {
    "display_name": "开发测试机",
    "labels": {"project": "loom"}
  }
}
```

mcue_ref 必需；package 和 managed_by 可选。Observer 解析类型定义并校验初始属性后持久化。
重复登记相同资源仍返回同一 URI；不能利用重复登记替换 MCUE 定义或扩展属性。
现有 state 的登记更新行为可以保留，但必须保留未修改的属性，不能把缺省 attributes 当成空对象覆盖已存值。

例如对该资源调用 `grip://loom.local/workspaces/workspace-default/actions/update-attributes/v1`，input 为：

```json
{
  "expected_attributes_revision": 3,
  "patch": {
    "description": "开发环境，兼作数值计算验证",
    "labels": {"environment": "development"},
    "cpu_cores": 8
  }
}
```

patch 语义固定为：对象递归合并，未提供的字段保留，数组整体替换，null 删除对应字段。
Observer 对合并后的完整 attributes 校验，成功后原子写入并递增 attributes_revision；revision 不一致则返回冲突，客户端读取新值后重新决定修改。
无实际变化的重复 patch 保持修订号；使用已经过期的 expected_attributes_revision 仍返回冲突，不隐式重放。

返回业务输出示例：

```json
{
  "resource": "grip://loom.local/workspaces/workspace-default/resources/vm/dev-vm-01",
  "attributes_revision": 4
}
```

这是 Observer 内的元数据事务，直接返回结果，不创建 Driver Run，也不要求 VM 在线或存在 ready activation。
该 action 的 offer 由 Observer 添加，与访问包导出的 exec 等业务动作合并，支持在未激活资源上补充描述。
patch 只进入 attributes；不能借它覆盖 state、package、managed_by、activations 或实际使用的 instance_config。
若 MCUE 定义了 host、port 等公开属性，patch 可以修改这些描述值，但不会改变实际连接目标。

升级类型定义时，update-attributes 可额外携带新的 mcue_ref。只允许相同 mcue 的显式版本切换，
按新 Schema 校验本次合并后的属性，并原子更新定义绑定、属性和修订号。这样可同时补齐新定义的必需字段。
首版不提供跨类型转换。

attributes_revision 不复用 GRIP Description.revision，也不复用激活实例的 revision。
GRIP revision 标识完整可见描述快照；激活 revision 保护执行实例；attributes_revision 只保护本方案的属性及类型定义绑定。
更新说明、标签或硬件描述不会改变 activation_revision，不导致正在执行的业务动作失效。

## 6. 持久化与资源产生链路

ManagedResource 增加 mcue_ref、attributes 和读取时的 attributes_revision，package_ref 改为可选。
现有 WorkspaceResourceRow.resource JSON 保存类型引用、属性和原有平台字段；独立 attributes_revision 列作为 CAS 的权威计数，避免在 JSON 和列中重复维护计数。
新增按 workspace、mcue、version 唯一的 MCUE 定义注册记录，正文复用 ContentStore。

登记、state 更新、清理和 attributes 更新统一通过 repository 的资源修改入口，在数据库事务中读取并修改当前行，
仅更新本次负责的字段。不能从旧内存快照整段覆盖 JSON，否则 state 更新可能丢失刚提交的 attributes。
属性更新使用数据库修订比较及行级事务，内存锁仅服务无数据库测试场景。

资源创建 descriptor 的 resource_effects 用显式 mcue_ref 替代自由字符串 resource_type；类型标签从定义读取。
SDK/编排中的资源登记同步传递 mcue_ref 和 attributes，不能从 URI 路径、来源包或 capability_kind 猜测领域类型。
现有 readiness、cleanup、access_requirements 继续描述资源的生命周期和访问能力，MCUE 不代替这些契约。
首版创建时可登记空属性，之后由有权限的调用方将 provider 结果或采集结果转换成类型允许的公开属性再更新；
DESCRIBE 只读取已登记信息，不隐式执行硬件采集，不新增属性映射表达式语言。

模型、存储和属性更新接口面向所有已登记领域资源；computing/vm 是首个领域标准定义。
资源类型字段的替换覆盖整个 GRIP 目录，包括 Slave、activation、package、action、Schema、content 等平台资源。统一映射如下：

| 原资源 kind | 新 resource.mcue |
|---|---|
| workspace | loom/workspace |
| slave | computing/slave |
| capability-package | capability/package |
| activation | capability/activation |
| run | execution/run |
| action | grip/action |
| schema | grip/schema |
| content | data/content |
| resource | 从注册时指定的 mcue_ref 读取，例如 computing/vm |
| 新增 MCUE 定义资源 | grip/mcue-type |

初始化时为上述平台类型同时发布内置 v1 定义，每个资源描述都带有可解析的 metadata.mcue_ref。
平台记录继续从各自权威记录生成系统 metadata；首版其 attributes 为 `{}`，对应定义接受空对象，暂不开放平台属性写入。
grip/mcue-type/v1 也由同一初始化批次写入，其描述引用自身的定义；初始化先保存完整定义，再开放目录查询。
目录和动作处理按权威记录判断实现归属，不通过 mcue 字符串前缀推断执行处理器，也不把类型标签当成授权凭证。

## 7. 落地次序与迁移

1. 更新 GRIP 资源描述契约，Resource.mcue 替代 Resource.kind，目录和 SDK 统一使用 mcue 过滤；覆盖全部资源类型并删除旧参数。
2. 增加 MCUE 定义模型、注册表、computing/vm/v1 及平台类型定义文件与发布/read 动作。
3. 扩展 ManagedResource 和数据库记录，统一资源写入事务，补齐属性 CAS、update-attributes 和 DESCRIBE 投影。
4. 同步 resource_effects、资源登记原语及新发布能力包模板中的 MCUE 引用，删除被替换的自由 resource_type 路径。
5. 更新使用指南、协议示例、SDK 示例和仓库 skills，验证分发包；同步本机安装的 skill。
6. 用明确的资源 URI 清单为已有 VM 回填 computing/vm/v1 与空属性；根据实际信息补充属性后验证描述。

本设计按新契约实现，不设置旧字段别名或双通道描述更新接口。
已有资源的 URI、激活绑定、instance_config 和凭据保持原身份与位置；发布新的能力包版本承载改过的 descriptor，
不改写已有不可变包、内容或历史 Run。现有 URI 中的 vm、compute.vm 或 IP 字符串均不作为 MCUE 类型和连接配置的解析依据。
后续新资源使用可读逻辑名称，不从内部连接配置自动生成资源名。
历史描述符读取只校验不可变内容摘要；新制作、包发布和执行预检继续校验当前契约，不将旧 resource_type 转换为 MCUE。

## 8. 验收条件

1. 发布 computing/vm 定义后可通过目录发现、DESCRIBE 和 read 取得其 Schema；版本不可覆盖，引用均由 Observer 返回。
2. 更新显示名称、标签、CPU、内存等后，DESCRIBE 返回经过 Schema 校验的 attributes，资源 URI 保持不变。
3. 未定义字段、非法数值、错误类型和不满足新版本 Schema 的修改整体拒绝，不产生部分更新。
4. 并发属性更新会发生明确修订冲突；state 更新和属性更新不会相互覆盖；重启后属性、定义引用和修订号保持一致。
5. 同一连接字段在声明允许它的 MCUE 中可作为公开属性写入和读取，在不允许它的定义中被拒绝；没有平台字段名黑名单。instance_config 和凭据不会被自动复制到公开属性，更新描述不改变实际连接配置或触发远程命令。
6. 更新描述后原有 exec 及激活 revision 仍有效；多个访问实例时继续执行既有显式选择规则。
7. 以另一个最小自定义类型验证通用性，无需为其增加资源模型字段或 repository 分支。
8. 全部 GRIP 资源使用 mcue 和可解析的 mcue_ref，目录与 SDK 按 mcue 过滤；资源级 kind 字段和旧过滤参数被拒绝。协议消息的 kind 及其他独立概念不受影响。
9. 仓库 skill、本机安装副本和分发包一致说明 MCUE 字段、定义发现和属性更新，以及匹配服务端版本和迁移要求，不把本地实现误称为已部署接口。

实现包含 SDK 0.5.0、Alembic 006_mcue_attributes、显式资源回填脚本、MCUE 定义文件、
Observer 动作、编排登记原语和 suanlicloud-http/v3 模板。生产升级步骤见 [MCUE 迁移指南](../../mcue-upgrade.md)。
验证结果见 [实现验证记录](../reports/2026-10-20-mcue-resource-attributes-validation.md)。
