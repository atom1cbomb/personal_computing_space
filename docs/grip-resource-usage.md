# 使用 GRIP 调用 Loom 资源

Loom 的资源使用入口是 `DESCRIBE` 和 `INVOKE`：先描述目标，从 `offers` 选择完整动作 URI，再向该 offer 的 binding endpoint 发起调用。你不需要拼接 Slave 内网地址、runtime 路径或 ComputeBinding。每个公开目录项还提供平台内建的 `how-to-use` 只读动作；当资源关系、MCUE 自定义属性或激活使用方式不清楚时，先调用它取得结构化说明和 Markdown 指引。

| 对象 | 作为资源的含义 |
|---|---|
| 能力包 | 不可变的程序与契约，可以作为 action 的输入传递 |
| Slave | 承载执行的资源，提供 run-package、check-package 和 activate-package |
| 激活实例 | 一个包在某 Slave 上运行的命名实例，提供业务动作、stop，必要时提供 auth-start |
| VM 等登记资源 | 提供 update-attributes，并通过显式绑定的激活实例提供业务动作 |
| Run | 一次异步执行的任务资源，提供 read 和 cancel |

能力包和激活实例是不同的资源。同一个包的两个命名实例有不同 URI；实例停止后 URI 仍可描述。激活 revision 表示当前生命周期版本。

## 可读名称与内容引用

所有资源引用统一使用 GRIP，包括包内的 Schema、IoContract、descriptor、validator 和程序内容。名称先表达用途，摘要只标识不可变内容版本。例如：

```text
grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-list
grip://loom.local/workspaces/workspace-default/packages/suanlicloud-http/v5
grip://loom.local/workspaces/workspace-default/content/suanlicloud-http/schemas/instances-list-output/sha256/<摘要>
grip://loom.local/workspaces/workspace-default/activations/suanlicloud-http/v5/slave-a/default
```

同一个 URI 在包内引用、SDK 和资源目录中保持一致，不再将资源地址编码后嵌入另一层地址。内容摘要仍用于校验与存储去重；完整名称绑定会持久化，不能只换一个名称就读取同一摘要。包版本不可变，包摘要保留在契约中；激活实例的变更通过 revision 管理。

上传内容时指定可读用途，或让服务端按媒体类型命名：

```python
schema_ref = await client.upload_content(
    {"type": "object", "properties": {"instances": {"type": "array"}}},
    media_type="application/schema+json",
    name="cloud/schemas/instances-output",
)
await client.describe(schema_ref.resource_id)
schema = await client.read_json(schema_ref)
```

内容的 `read` 动作返回 `{content, media_type, encoding}`，支持 JSON 对象、数组、标量和文本；SDK 的 `read_json` 取得其中的 JSON 内容。二进制以 base64 返回。Schema 资源用于调用契约时，内建与派生 Schema 的 `read` 直接返回 Schema 对象。

ServicePackage 会根据包名和源文件名自动命名各个工件，并把默认模板命名空间替换成发布目标的 authority/workspace，重新计算受影响的引用和摘要。直接拼接裸摘要不能创建已登记的内容资源。旧资源名称和旧持久化数据不提供兼容解析，应使用新的生成器和发布器重新上传、注册。

## 部署

在 Observer 设置：

```sh
export LOOM_GRIP_AUTHORITY=loom.example.com
export LOOM_PUBLIC_BASE_URL=https://loom.example.com
export LOOM_GRIP_TOKEN='<由部署方配置的独立 bearer token>'
```

远程部署 TOML 的 `[cluster]` 对应设置 `grip_authority`、`public_base_url` 和 `grip_token_file`；部署工具读取 token 文件，只挂载到 Observer，并从 dry-run 输出中隐藏。

`LOOM_GRIP_AUTHORITY` 是稳定资源命名域，不是 Slave 拨号地址。`LOOM_PUBLIC_BASE_URL` 是调用者可达的 Observer 外部地址，TLS 可以在反向代理终止。`POST /.well-known/grip` 同时承载描述和调用。SDK 使用传入的 Observer URL 作为该命名域的显式可信引导映射。未配置 token 时 GRIP 拒绝访问；HTTP Authorization 和 Cookie 不补齐消息中的 auth。

本地开发可将 public base URL 设为 `http://127.0.0.1:18080`，客户端显式传 `allow_insecure_grip=True`；这使用独立的 `loom-http-json.v1` 绑定。生产 TLS 使用 `https-json.v1`。两个绑定都只支持 request-reply，UTF-8 JSON、HTTP 200 内的 GRIP reply/error；非 200、连接失败和非法 JSON 是承载失败。字节流使用独立的 `loom-wss-stream.v1` 绑定；没有自动重试或重定向。详见 [服务 binding 与字节流](grip-service-bindings.md)。

## 运行能力包

以下示例假设 workspace 已发布一个输入为 `{"value": 21}` 的倍增程序包，且 slave-a 安装了对应 runtime provider。包上传和内容制作仍使用 SDK 的 register_package/upload_content；普通资源调用使用 GRIP。

```python
import asyncio
import os
from loom_orchestration import LoomClient

async def main():
    async with LoomClient(
        "https://loom.example.com",
        token=os.environ["LOOM_GRIP_TOKEN"],
        grip_authority="loom.example.com",
    ) as client:
        slaves = await client.resources(mcue="computing/slave")
        slave = next(r for r in slaves if r["uri"].endswith("/slave-a"))
        packages = await client.resources(mcue="capability/package")
        package = next(r for r in packages if r["metadata"]["package_id"] == "double")
        export = package["metadata"]["actions"][0]

        description = await client.describe(slave["uri"])
        run_action = next(o["action"]["uri"] for o in description["offers"]
                          if o["action"]["uri"] == client.names.action("run-package"))
        result = await client.invoke(slave["uri"], run_action, {
            "package": package["uri"],
            "action": export["uri"],
            "arguments": {"value": 21},
            "idempotency_key": "double-example-1",
        })
        for _ in range(120):
            state = await client.invoke(result["run"], client.names.action("read"), {})
            if state["state"] in {"completed", "failed", "cancelled", "awaiting_decision"}:
                print(state)
                return
            await asyncio.sleep(1)
        print("任务仍在运行，可继续查询同一个 Run URI")

asyncio.run(main())
```

显式运行任务返回 `{"run": "grip://.../runs/..."}`，表示已持久化排队；业务结果在 read 返回的 `outcome` 中。Driver 需要在线领取队列。`request_id` 只关联通信；业务 `idempotency_key` 重用相同输入会返回同一任务，改变输入会拒绝。通信超时不表示任务没有执行。

run-package 在指定 Slave 执行一个普通包导出。`container:python_orchestrator/1` 是 Driver 的编排运行方式，继续通过完整计划提交，不伪装为 Slave runtime。

## 激活后调用

长期服务、命名访问实例先激活：

```python
activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "vm-access",
    "instance_config": {"host": "vm.example.com", "port": 22, "user": "worker"},
    "credential_profile": "vm-ssh-default",
    "target": vm_resource_uri,
    "idempotency_key": "activate-vm-access-1",
})
description = await client.describe(activation["resource"])
```

激活成功后会自动得到实例资源，URI 直接取自返回值。手动启动容器不会产生 Loom 激活记录。
DESCRIBE 的 `metadata.actions` 列出包声明的全部业务动作，包括名称、说明、描述符、
IoContract、权限和副作用，以及 `available` / `unavailable_reason`。其中 `arguments_schema`
和 `result_schema` 是业务输入输出 Schema 引用；`input_schema` / `output_schema` 则描述 GRIP
调用契约：task 使用参数包装及 `{run: ...}`；request-reply 直接使用业务输入输出，byte-stream 使用建流契约。Schema 正文按引用继续读取。
`offers` 列出当前可调用的动作与 binding。实例未就绪或停止时，仍能查看全部业务动作说明。

instance_config 必须符合包的配置 Schema；target 可省略，提供时必须是已登记在当前 workspace 的领域资源。配置仅进入受控激活记录，不放进公开目录。`pending_auth` 表示还需认证，不能执行业务动作；如果描述提供 auth-start，可在该实例上调用它，输入 `{"expected_revision": revision}`，按返回的认证指引完成操作。浏览器 bridge 复用现有认证辅助通道。

`credential_profile` 与 `instance_config` 并列，选择 Slave operator 预先配置的凭据目录名称。
同包不同实例可以选择不同 profile；包 manifest 不再指定凭据选择。不能传凭据内容或任意路径。
新实例省略/null 表示不挂载凭据，已有实例省略/null 保留原绑定；同一实例名不能改绑 profile。
停止再启动、重试和 Slave 恢复均使用已保存的选择。该字段不进入公开 DESCRIBE 或 MCUE 属性。
此接口需要激活凭据版本的 Observer、Driver、Slave 和 SDK 0.6.0；见
[升级说明](activation-credentials-upgrade.md)。

ready 后，从实例的 offers 选定业务动作并按其 Schema 调用。以下 SSH 导出仍为 task：

```python
result = await client.invoke(activation_uri, business_action_uri, {
    "arguments": {"command": "hostname"},
    "expected_revision": description["resource"]["metadata"]["revision"],
    "idempotency_key": "hostname-1",
})
```

这里的完整动作 URI 来自描述；command 只是 SSH 包输入示意，实际字段以所选动作的 Schema 为准。业务动作输入契约包装原始包输入，输出契约为异步任务 URI，业务输出仍由原 IoContract 验证。SDK 会读取 Schema 并校验调用输入及协议输出；服务端也始终校验。

调用已激活实例不会重新 provision，Slave 在生命周期锁内检查 revision 和 ready 状态。停用是在实例上调用 stop，输入 expected_revision 与 idempotency_key。若 VM 明确绑定一个 ready 实例，可以直接对 VM 调用相同业务动作；存在多个候选时，VM metadata 列出实例 URI，由调用者明确选择。

## 领域资源登记和服务包发布

包发布后直接使用 Observer 返回的名称：

```python
published = await client.register_package(manifest)
package_uri = published["resource"]
description = await client.describe(package_uri)
```

`resource` 是完整 GRIP URI，包信息中仍包含 `package_digest`、契约和动作导出。
重复发布相同包会返回同一名称。promote 返回提升后包的名称，应使用其响应中的 `resource`。
包列表、详情和 GRIP read 也返回这个字段；SDK 与脚本不再自行拼接发布包的名称。
`ServicePackage.publish` 直接返回该 URI，可作为 `activate-package` 的 `package` 输入。

先从 `grip/mcue-type` 目录取得类型定义，再对 workspace 调用 `register-resource`。包引用可选：

```python
types = await client.resources(mcue="grip/mcue-type")
vm_type = next(r for r in types if r["metadata"]["mcue"] == "computing/vm"
               and r["metadata"]["version"] == "v1")
registered = await client.invoke(client.names.root, client.names.action("register-resource"), {
    "resource_id": client.names.name("resources", "vm", "development"),
    "mcue_ref": vm_type["uri"],
    "package": ssh_package_uri,
    "state": "running",
    "attributes": {"display_name": "开发测试机", "labels": {"project": "loom"}},
})
vm_resource_uri = registered["resource"]
```

可选 `managed_by` 指向当前工作空间另一已登记资源的 GRIP URI。重复相同绑定登记幂等，
改变包或管理关系会拒绝。登记不创建 VM、不激活包；随后以该 URI 作为 activate-package 的 target。
完整编排内的 `ctx.register_resource` 等原语属于持久执行内部协议。

## MCUE 与动态描述属性

MCUE（Multipurpose Computing Utility Extension）使用 `category/subtype` 描述资源类型，取代资源级 `kind`。
所有资源的描述都包含 `resource.mcue` 和 `metadata.mcue_ref`；后者指向不可变类型定义。
目录过滤使用 `resources(mcue="computing/vm")`，GRIP 的 list-resources 输入使用 `{"mcue":"computing/vm"}`。
消息外层 `kind: reply/error` 仍表示消息种类。

对 MCUE 定义调用 read 返回 `mcue`、`version`、`description` 和 `attributes_schema`。
computing/vm/v1 定义 `display_name`、`description`、`labels`、`cpu_cores`、`memory_bytes`、`os`、`disks`；
这些字段全部可选。公开值统一位于 `metadata.attributes`。
`package`、`managed_by`、`state` 和 `activations` 是系统 metadata。

```python
description = await client.describe(vm_resource_uri)
metadata = description["resource"]["metadata"]
definition = await client.invoke(metadata["mcue_ref"], client.names.action("read"), {})
updated = await client.invoke(vm_resource_uri, client.names.action("update-attributes"), {
    "expected_attributes_revision": metadata["attributes_revision"],
    "patch": {"description": "开发与数值计算验证", "labels": {"environment": "test"}},
})
```

patch 对对象递归合并、对数组整体替换，null 删除对应字段。Observer 校验合并后的完整属性；
非法值整体拒绝，修订过期返回 `attributes_revision_conflict`，应重新读取并决定修改。
无变化的有效 patch 不递增修订号。更新直接返回资源 URI 和 attributes_revision，无需 VM 在线，也不创建 Run。
资源 URI、访问绑定、执行配置和 activation revision 保持稳定；完整描述的 revision 随可见内容变化。

公开哪些连接或运行时字段由 MCUE Schema 决定。平台不统一禁止 host/user/port，
也不会自动公开 instance_config。凭据通常保留在认证和运行时凭据渠道。
修改描述中的同名字段不改变实际 SSH 连接配置。

自定义类型通过 workspace 的 `publish-mcue-type` 发布：

```python
published_type = await client.invoke(client.names.root, client.names.action("publish-mcue-type"), {
    "mcue": "network/endpoint",
    "version": "v1",
    "description": "公开服务端点",
    "attributes_schema": {
        "type": "object",
        "properties": {"host": {"type": "string"}, "port": {"type": "integer", "minimum": 1}},
        "additionalProperties": False,
    },
})
```

MCUE 支持自包含 JSON Schema 2020-12 和本地 `$defs` 引用，不解析远程 Schema；它与动作 io.v1 的 Schema 子集独立。
同一类型版本不能覆盖。发布新版本后，已有资源仍绑定旧版本；
在 update-attributes 中同时传入同类型的新 mcue_ref 和属性 patch，可原子迁移定义版本。
平台类型包括 computing/slave、capability/package、capability/activation、execution/run、loom/workspace、
grip/action、grip/schema、data/content 和 grip/mcue-type；平台系统字段由各自权威记录维护。

已有部署使用 SDK 0.5 和对应服务端时，需要先执行[MCUE 迁移](mcue-upgrade.md)。

七个服务包都有完整可上传产物，统一使用 `ServicePackage.publish` 或
`scripts/publish_service_package.py` 发布；参见 [服务包指南](../capability_services/README.md)
和 [可运行示例](../examples/grip/README.md)。

## 名字、发现和旧接口

所有 URI 位于 `grip://<authority>/workspaces/<workspace>/...`；SDK 的 `client.names.root` 是目录根。包 URI 带包名和不可变版本，摘要保留在契约中；激活 URI 带包、Slave 和实例名，动作 URI 绑定完整导出身份，不根据尾段分派。

Schema 也是资源：DESCRIBE 后调用内建 read 取得 JSON Schema 2020-12。read 的引导契约预装，不递归依赖自身 Schema。描述不带 expires_at，因此仅用于本次调用准备；SDK 不缓存或跨主体共享描述。

REST 的内容上传、包制作、完整计划提交与管理 UI 仍承担制作和管理职责。旧公开 REST 资源发现、登记、激活、停用和认证启动路径已删除，资源使用统一改用 resources/describe/invoke。包 promote 仅负责发布候选包，不再携带激活参数。Worker/plugin 保持内部协议，通过既有调度和持久化执行账本承载 GRIP 动作。

设计和验证范围见 [GRIP spec](superpowers/specs/2026-10-19-grip-resource-invocation-design.md)
及 [action 与实例描述 spec](superpowers/specs/2026-10-20-grip-actions-and-activation-description-design.md)。

统一登录和过期后的重新认证使用 SDK 0.7 的 `loom-auth`，参见 [认证说明](authentication.md)。
