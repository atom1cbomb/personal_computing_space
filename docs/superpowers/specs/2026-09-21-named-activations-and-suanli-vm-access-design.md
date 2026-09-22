# 命名能力包实例与 Suanli VM 访问

状态：部分实现，尚未完成端到端验收；远程登录接入按 [浏览器回调代理设计](2026-09-22-browser-auth-redirect-proxy-design.md) 实施。遵循 [设计规范](../spec-design-guidance.md)。

## 1. 目标与选择

单用户 workspace 中，每创建一台 Suanli Cloud VM，在 Slave 上部署一个独立的访问容器。容器包含官方 opkssh、OpenSSH 客户端和小型 HTTP 服务，通过 SSH 在该 VM 上执行命令。复用 `service + container:http/1`、通用外部认证、资源登记和内容存储，不新增 SSH runtime。

每台 VM 复用同一个已发布包和镜像，但拥有不同的命名 activation、目标绑定、登录状态及持久化目录。不是为每台 VM 生成一个 package，也不是在 VM 内再部署 HTTP 服务。VM 服务端 opkssh 由 cloud-backend 的 cloud-init 安装；访问容器安装的是客户端。

本设计不保留旧单实例 API 兼容层。纯接口包仍可用于描述资源；本次 VM 直接引用有 `body` 的访问包，不额外维护一份重复接口包。

## 2. 最小实例模型

扩展现有 `CapabilityPackageActivation`，不新增实例 registry 或第二套生命周期模型：

```text
CapabilityPackageActivation {
  ...现有字段
  instance_name: string
  target_resource_ref: ResourceRef | null
  instance_config: JSON object
}
```

实例唯一键为 `(workspace, package_version_ref, target_slave, instance_name)`。`instance_name` 必填，采用 1–63 位小写字母、数字、连字符，首尾为字母或数字，例如 `vm-<instance-id>`。名称在该键范围内不可修改；不再同时引入 UUID、别名和显示名。

`target_resource_ref` 是可选的通用资源绑定；VM 访问实例必须绑定同 workspace 已登记的 VM，且资源的 `package_ref` 与实例使用包的精确引用一致。绑定创建后不可更换，避免复用已有 SSH 信任记录访问另一台 VM。普通服务实例可不绑定资源。

`instance_config` 保存非敏感的实例输入；VM 包使用 `{host, port, user}`。host 从该 VM 的 `DescribeInstances` 结果选取，port 首版为 22，user 为 root。它不进入共享 package digest，也不能承载 token、私钥或任意宿主机挂载路径。Observer/Driver 只保存和传递，具体格式由访问服务校验。

IP 变化通过显式更新实例配置、递增现有 `activation_revision` 并重建访问容器生效；保留 SSH 信任与认证存储。相同幂等键、不同配置必须冲突，不能被现有“desired_state 相同即返回”逻辑忽略。首版不实现包升级或跨 Slave 迁移。

## 3. 实例选择必须贯穿全链路

provision、deprovision、auth/start、health report、查询与事件均携带 `instance_name`。原有锁、幂等、revision fencing、持久化、健康重试及重启 reconcile 按实例隔离；不增加新的 generation 或 digest。

执行绑定必须选择确定实例。复用 `ComputeBinding.activation_ref` 表达上述实例键，统一其引用格式及解析；动态节点请求与持久化节点补充同义的实例引用，并透传到执行绑定。不根据包名、operation 或“第一个 ready 实例”猜测目标。Observer 校验 workspace、包、Slave 和实例绑定一致，Slave 只调用该实例。

实例引用是运行身份，不是新的内容寻址对象。package exports、descriptor 和 IoContract 仍只保存在包中。资源查询可附带由 activation 记录派生的实例引用列表，`ManagedResource` 本身不重复存储绑定；VM `package_ref` 描述使用，`managed_by_ref` 指向提供生命周期管理的算礼云资源。

Docker 容器名和 labels 纳入完整实例身份，不能继续只按 package digest 合并容器。认证、inspect、invoke、删除、并发限额及 reconcile 使用同一实例身份；删除旧的同 digest 容器共享逻辑。容器名可以用实例键的短编码生成，但 labels 必须验证完整身份，不能把名称截断当作唯一性保证。

## 4. 配置与持久化

container-http runtime 将 activation 的目标引用和 `instance_config` 写成只读 JSON，挂载到容器固定路径 `/run/loom/instance.json`。路径由 runtime 管理，不允许调用方指定。SSH 服务只使用该文件中的目标，不接受业务请求覆盖 host、user、SSH options 或凭据路径。

HTTP package body 增加可选 `persistent_state: boolean`，默认 false；VM 访问包设置 true。runtime 为每个实例建立独立 Docker named volume，挂载到 `/var/lib/loom/service`，供容器 UID 65534 写入。保留已有只读根文件系统、非 root、cap-drop 等约束。

该目录保存 opkssh 会话、私钥和 `known_hosts`，不上传 ContentStore，不进入运行结果或日志。重启、配置更新、deprovision 后再次启动同一实例均保留该卷；deprovision 仅停用并移除容器，不清空信任记录。

首版在确认 VM 已终止后，由部署/测试收尾按实例完整 labels 显式清理其专属卷和配置文件，并记录清理结果；不增加通用卷管理 API。实例名不得复用于新的 VM。宿主机丢失导致的状态丢失需重新授权，不声称支持跨机恢复。

## 5. SSH 访问与认证

首版只导出有界的 `exec` 操作：输入 `command` 和受上限约束的 `timeout_seconds`；输出 `stdout`、`stderr`、`exit_code`、`timed_out`。用 OpenSSH CLI 执行远端命令，本地使用参数数组调用，不拼接本地 shell。远端命令执行本身是显式授权的能力，不能宣称它是沙箱执行。

契约、schema 和 descriptor 放在新访问包自己的目录。操作声明远程执行权限和 `NotReplaySafe`；连接中断或超时不能自动重放命令，也不能把本地 SSH 退出等同于远端进程已终止。限制输出大小，截断必须显式标记；验收的成功判断同时检查退出码、超时与期望输出，不以 HTTP 200 代替任务成功。结果仍经已有 Slave 校验和 Observer ContentStore 落盘链路返回。

沿用 `authentication.start_path/status_path`、`pending_auth` 和 Observer 的重新登录 API，并明确选择实例。单用户 workspace 不新增多用户会话分配；每个 VM 容器仍独立保存凭据，不暗中复制其他实例的登录会话。

认证配置采用 [算礼 SSH 文档](../../../production/suanli_cloud_ssh.md) 的 issuer、client_id 与 scopes。AK/SK 的云 API 权限不等于 OIDC 的 VM SSH 权限。

**远程登录接入：** 采用 localhost callback；用户先建立本地端口转发到 Observer，浏览器访问
`http://localhost:3000/login`，认证数据路径为 `Observer → Slave → VM 容器`，不经过
Driver。Observer 只转发固定的认证 Worker 路径，不接受任意 URL，也不要求为每个 IdP
注册新的远程 callback。生产前仍必须确认用户访问控制和端口转发边界；这不是已经完成
真实登录的声明。

容器存活、OIDC 已登录、VM 可 SSH 访问是不同事实。登录完成后还需对固定 VM 执行无副作用探测才报告可用；VM 启动期间有限等待 SSH，避免把网络未就绪误报成需要重新登录。认证过期走现有重新登录链路。

## 6. 首次信任（TOFU）

本地 cloud-backend 当前没有 VM host key 获取/查询实现。首版接受首次连接可能遭遇冒充的 TOFU 风险，采用官方 OpenSSH：

```text
StrictHostKeyChecking=accept-new
UserKnownHostsFile=/var/lib/loom/service/known_hosts
HostKeyAlias=<固定 VM resource_ref 的稳定别名>
```

首次自动接受并保存服务器公钥，之后拒绝变化。HostKeyAlias 按 VM 稳定身份建立，不能只按 IP；因此 IP 变化不会自动建立一条新信任记录。`known_hosts` 保存服务器公钥，不是只有指纹文本。

host key 不匹配时拒绝执行、报告明确错误并将实例标记 degraded；重启或重新登录不得清除记录。VM 重装等需要重置信任时，由 operator 核实后显式清理该 VM 的记录，首版不新增重置 API。网络必须实际允许访问 VM 的 SSH 端口及 IdP；`egress` 配置本身不保证路由可达。

## 7. Suanli VM 创建到使用

复用现有任务编排/部署流程串联，不在 Observer/Driver 增加 Suanli 专用分支，也不引入通用结果投影 DSL。
这里的“编排程序”是 Run 闭包所引用的
`container:python_orchestrator/1` 能力包中的 `body.program_content_ref`，不是
Run 在启动时临时拼接的脚本。coding agent 通过 draft/commit 流程生成或选择这个
编排包，并把包引用写入 `TaskClosure.program_systems.package_ref`；commit 后的
`ClosureVersion` 固化该引用和闭包摘要，Driver 只执行已提交版本。编排程序通过
`OrchestrationContext.emit_node(package_ref, capability_descriptor_ref, input_refs)`
生成动态 Node，Node 才是实际交给 Observer/Slave 执行的能力包调用。

典型闭包结构为：

```text
Run / ClosureVersion
  └─ orchestration package (program_content_ref)
       ├─ suanlicloud instances-run
       ├─ suanlicloud instances-status (bounded polling)
       ├─ resource registration / VM identity binding
       ├─ suanli-vm-ssh-http activation
       ├─ authentication/readiness wait
       └─ VM exec nodes
```

认证是其中一个可暂停的阶段：编排程序可以得到 `auth_required` 和短时登录信息，
等待用户通过 Observer/Slave 的认证通道完成登录，再继续检查访问实例 ready；不能
把交互式 OIDC 登录伪装成普通同步节点成功。

流程步骤如下：

1. 确认算礼云管理资源和 VM 访问包已登记，确定项目、区域、镜像与测试预算。
2. 调用 `instances-run`，记录返回的 InstanceId；创建请求不自动重放，结果不明确时先查询核对。
3. 按 InstanceId 登记 VM 资源，引用共享访问包，`managed_by_ref` 指向算礼云管理资源。调用方负责登记；失败可用同一稳定资源 ID 重试，不重新创建 VM。
4. 有界轮询实例，等到可用 IP，再创建名为 `vm-<instance-id>` 的访问实例并固定绑定该 VM。
5. 完成必要的用户登录及 SSH 探测后，通过具名实例执行任务。
6. 通过算礼云包管理 VM 生命周期。停用访问容器不删除 VM；VM 终止后停用对应实例并更新资源状态。

补齐 Suanli 输出 schema 中实际消费的 `InstanceId`、`State`、`Ip/Ips` 等字段，不能继续以空对象代替实例契约。`RunInstances` 是异步受理，不代表 VM 已可 SSH 使用；创建结果和访问就绪分别验证。

## 8. 交付顺序与验收

先验证官方 opkssh 远程登录路径，再实现通用命名 activation、实例配置与独立持久化，最后实现 VM 访问包及 Suanli 编排。原有单实例数据在部署维护窗口一次性赋名 `default`；新 API 仍要求显式名称。旧容器需按实例身份重建，升级前列出受影响激活，不维护两套容器寻址代码；涉及持久化变更使用新迁移，不修改发布 baseline。

验收包含：

- 同一 package/digest 在同一 Slave 上部署两个名字，生成两个独立容器；调用、登录、停止其中一个不影响另一个。
- 重复创建同名同配置幂等，改变目标资源冲突；迟到的健康报告和旧 revision 不能影响另一实例。
- Observer/Slave 重启能恢复两实例；容器重建后 `known_hosts` 保留。新 key 首次接受，后续不匹配拒绝，改 IP 不能绕过已有记录。
- 本地 Docker 用两个 SSH 测试目标验证目标隔离、配置更新、超时/输出边界及持久化，不依赖真实 VM 收费资源。
- 获准后创建一台最小规格生产测试 VM，从 Observer 发起任务，经访问包执行唯一标记命令，核验 exit code、输出和结果内容取回；查询资源可发现使用包及对应实例。
- 按约定终止本次测试 VM、确认终止状态，再清理专属容器/卷并汇报。即使 SSH 登录失败也须跟踪已创建实例，不能遗留未报告的付费资源。

本 spec 不代表已完成 VM 创建、OIDC 登录或生产端到端测试。
