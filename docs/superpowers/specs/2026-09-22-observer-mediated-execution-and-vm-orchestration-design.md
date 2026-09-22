# Observer 中继执行与 Suanli VM 编排

状态：已实现基础链路；Suanli VM 的真实云端 E2E 仍需凭据和可访问环境。遵循
[spec 设计规范](../spec-design-guidance.md)。

## 1. 目标

Driver 只负责 coding agent、闭包执行和调度决策；Observer 是唯一控制面与状态权威；
Slave 执行能力包。生产环境的能力执行、认证和结果回传不允许 Driver 直接持有或调用
Slave endpoint。

```text
Driver → Observer → Slave → runtime-plugin → capability container
Slave  → Observer → Driver（结果引用/状态）
```

Driver 发出的动态 Node 先由 Observer 持久化 `NodeIntent`/`Attempt`、校验 workspace、
package digest、descriptor、实例和 revision，再由 Observer 通过已注册的 Slave lease
endpoint 中继执行。Driver 不选择或缓存 Slave 网络地址作为数据面身份。

## 2. Run、闭包和编排包

coding agent 生成或选择 `container:python_orchestrator/1` 包，并将其引用写入 Run
draft 的 `TaskClosure.program_systems.package_ref`。包的 `body.program_content_ref`
是实际编排程序；`commit` 产生不可变 `ClosureVersion`，`start` 时 Driver 执行该
已提交程序。编排程序通过 `emit_node(package_ref, descriptor_ref, input_refs)` 产生
动态节点，不直接访问网络、Observer 或 Slave。

Suanli VM 编排顺序为：调用 `instances-run` → 读取 `InstanceId` → 有界轮询
`instances-status` → 得到 RUNNING 和 IP → 登记 VM resource → 激活并等待
`suanli-vm-ssh-http` ready → 执行 VM 节点 → terminate 并清理访问实例。

## 3. 中继协议

Observer 增加受内部 token 保护的固定路径：

```text
POST /internal/v1/slaves/{slave_id}/forward
```

请求只有 `{path, payload}`，`path` 只能是已列出的 Worker capability 操作（provision、
dispatch、deprovision、auth/start、auth/browser、inspect、reconcile），不能成为任意
URL 代理。Observer 从当前 workspace 的 active Slave lease 读取 endpoint，向 Slave
发送原始 Worker JSON，并返回受限 JSON 响应。失效 lease、workspace 不匹配和未知路径
在 Observer 拒绝。

动态节点生产命令为 `node.execute`。Driver 只提交已构造的 immutable
provision/dispatch envelope；Observer 先调用 `dispatch_dynamic_node` 持久化 Attempt，
再按固定顺序转发 provision、记录 health、转发 dispatch，并用 terminal report 调用
`record_dynamic_node_result`。任一步失败都会标记该 Attempt/Node 失败。下游 HTTP 错误
状态码不会被中继伪装成 200。

Driver 的 `WorkerSession` 在生产配置中只保存 `observer_url + slave_id`，所有 Worker
调用均发送给上述 Observer endpoint；单元测试可显式使用 direct transport，不作为
生产拓扑。浏览器认证也由 Observer 的 session 路由直接中继至 Slave，不经过 Driver。

## 4. 资源登记边界

`instances-run` 和 `instances-status` 的结果仍由编排程序按 IoContract 解析。资源登记
是 Observer-owned 状态变化，不由能力容器自行写数据库。首个实现提供一个窄的
Observer command `resource.register` 给 Driver，在校验 package/resource/managed-by
引用后幂等写入 `ManagedResource`；HTTP API 和该命令共用同一 `register_resource` 校验，
不引入通用 DSL 或第三方结果投影器。

命令只接受 `{resource: {resource_ref, package_ref, managed_by_ref?, state}}`（也允许同名
字段位于 arguments 顶层），其中 `package_ref` 必须带精确 digest；同一 resource ref
重复登记必须返回同一绑定，换 package 或 manager 则拒绝冲突。

## 5. 失败与安全

- Observer 只中继固定 Worker 路径，不接受 upstream、header、port 或 URL。
- Slave 继续验证内部 token、workspace、target、Driver epoch 和 activation revision。
- 中继不持久化认证 code、Cookie、私钥或业务响应正文；长任务结果仍按现有
  ContentStore/terminal report 落盘。
- Driver 重启不会丢失 Node/Attempt；重复 request 使用 Observer 幂等键，旧 epoch
  不能执行或写回结果。

## 6. 验收

- 生产 WorkerSession 的请求只到 Observer；Slave endpoint 不出现在 Driver 数据面配置。
- 动态 Node 的 provision/dispatch/result 经 Observer 中继并保留 revision fencing。
- 浏览器 `/login`、`/login-callback` 经 Observer→Slave→容器保持 Cookie。
- 同一 VM 创建流程能从 Run 闭包得到 InstanceId、IP、访问实例和最终 VM exec 结果；
  失败时保留已创建 InstanceId，禁止遗留付费 VM。
