# GRIP 统一资源使用与能力包执行

日期：2026-10-19。状态：已实现。遵循 [spec 设计原则](../spec-design-guidance.md)，协议依据 `/root/computility_named_resource/grip协议整理v2.md`。

> 2026-10-20 更新：业务术语、Run 回执与完整实例描述以 [action 和激活描述设计](2026-10-20-grip-actions-and-activation-description-design.md) 为准。
> 2026-10-20 补充：资源命名以 [全部资源统一命名设计](2026-10-20-grip-all-resource-names-design.md) 为准，包内引用也必须使用可读 GRIP 名称。

## 目标与决策

使用资源只需要 `DESCRIBE(target)` 与 `INVOKE(target, action, input)`。调用方不再把 Slave 列表、包导出、激活配置与 Worker 地址拼成执行请求。资源的逻辑名称与网络地址分开；Observer 是当前部署的可信目录与执行代理，运行时继续使用已有 Worker/plugin 协议。

不增加通用会话或另一套任务数据库。复用包与内容的不可变身份、Slave lease、激活 revision、IoContract、Closure/Run 持久化及 Driver 执行队列。资源目录从这些权威记录投影，重启后可重建，不双写一份易失步的资源注册表。已有未提交的资源元数据与外部执行工作作为本次实现基础。

## 资源与名字

配置 `LOOM_GRIP_AUTHORITY`（默认 `loom.local`）作为稳定命名域。资源统一位于 `grip://<authority>/workspaces/<workspace>/...`。每个路径组成部分分别做 URI 编码，不通过 URI 尾段猜测动作或 Slave。部署移动只改 binding endpoint。

| 资源 | 身份与含义 | 可用动作 |
|---|---|---|
| workspace | workspace 根资源 | list-resources、register-resource |
| packages | 包 ID、版本及 digest | read；metadata 给出导出动作与执行类型 |
| slaves | 注册 Slave ID | run-package、activate-package |
| activations | 包 ID、版本、digest、Slave、instance_name | 业务导出动作、stop、按需 auth-start；metadata 给出 revision、state、package、host、target |
| resources | 已登记领域资源的稳定 ID | 经显式 target_resource_ref 绑定的唯一 ready activation 提供的业务动作 |
| runs | 已有 Run ID | read、cancel |
| actions | 内建动作或完整 descriptor identity | read |
| schemas | 内建 Schema 或绑定包 digest/descriptor 的调用 Schema | read |

包是可传递的不可变资源；激活是独立的有状态资源。激活 URI 不含 revision；revision 放在描述 metadata，调用激活动作必须显式提供 expected_revision，由执行 admission 再次验证。停用后仍可描述，但不可继续调用业务动作。领域资源存在多个可用实例时不任意选一个；目录展示实例 URI，调用方选定实例。

## 动作与执行

`INVOKE(slave, run-package, {package, action, arguments, idempotency_key})`：package 是不可变包资源 URI，action 是该包导出的完整 GRIP 动作 URI，arguments 是业务 JSON 对象。服务端解析包和 export、校验输入，构建单节点完整 Closure 并提交现有持久化执行队列。返回 `{run: <Run URI>}`；这不是执行完成。Driver 按已绑定 Slave 执行，不允许隐式迁移到另一 Slave。run-package 面向由 Slave 承载的普通可执行包；Driver 编排包继续由完整计划提交入口承载。

`INVOKE(slave, activate-package, {package, instance_name, instance_config, target?, idempotency_key})`：通过现有 activation 执行边界准备长期实例，返回 activation 资源 URI 和实际状态；pending_auth 不是 ready。停止在 activation 上调用 stop；认证在提供该动作的 activation 上调用 auth-start，输入 expected_revision。浏览器认证辅助通道复用原有实现。绑定领域 target 时验证 workspace 和显式登记关系；访问包可与领域资源自身的包不同。

`INVOKE(activation, exported-action, {arguments, expected_revision, idempotency_key})`：复用同一提交执行函数，但固定已有实例与 revision，禁止重新 provision 或回退到 default 实例。领域资源调用还要明确匹配其绑定实例；没有执行绑定的接口包不宣称存在可调用 binding。导出的 GRIP 动作为异步 Loom 执行契约：输入是上述包装（arguments 按原 IoContract 校验），输出是任务 URI；原业务输出在 Run outcome 中，避免把任务句柄冒充业务输出。

`INVOKE(run, read, {})` 返回持久化运行状态与 outcome；`INVOKE(run, cancel, {})` 复用取消流程。业务去重使用 input.idempotency_key，独立于 GRIP request_id；相同 key 改变参数冲突，不自动重放 INVOKE。

## 线协议与安全

严格实现 grip.v1 请求与 reply/error：拒绝未知核心字段、重复 JSON 键、错误类型、非完整 URI 动作、query/fragment、非法 authority。认证只读取消息 auth，不能由 HTTP header/Cookie 补齐。新配置 `LOOM_GRIP_TOKEN` 为本 Observer 单 workspace 的 bearer credential；未配置时 GRIP 关闭认证访问，不接受任意字符串。可选 tenant 必须等于当前 workspace URI。此单用户部署的已认证主体可描述和调用当前 workspace 中公开的已发布资源。

入口 `POST /.well-known/grip` 同时处理 DESCRIBE 和 INVOKE。描述 bindings 使用明确配置的 `LOOM_PUBLIC_BASE_URL`，避免信任请求 Host。生产使用 TLS 反代和 `https-json.v1`；本地开发显式声明 `loom-http-json.v1`，不能把明文 HTTP 声称为 HTTPS。仅实现 request-reply，不发布未实现的字节流绑定。HTTPS binding：UTF-8 JSON、HTTP 200 包含关联的 GRIP reply/error、无重定向重试，描述带 `Cache-Control: private, no-cache`。无法取得合法 request_id 的坏消息使用 HTTP 400。

Schema 使用 JSON Schema 2020-12，复用 Pydantic/jsonschema；独立 SDK 声明 jsonschema 依赖。内建 read 契约预装，Schema 资源可 DESCRIBE 后 INVOKE read 获取；未知 Schema 不猜测。SDK 校验版本、request_id、资源 target、描述形状、有效期、完整 action 和受信 binding 地址；不跨受众转发 token、不自动跟随重定向。没有 expires_at 的描述只用于当次调用。

## 代码边界与旧路径

协议契约与 SDK 放在可独立发布的 loom_orchestration 包；Observer 的 GRIP 服务负责投影、域动作与现有 repository 的连接。新的资源使用指南和 SDK 使用 describe/invoke/resources；删除 SDK 原有 resource/resource_activations/slaves/start_auth 专用资源使用路径。包上传、完整编排计划提交和管理 UI 的 REST 属于制作/管理边界，继续服务各自现有调用方；Worker 与 runtime plugin 是内部执行边界，不作为第二套公共资源使用 API。

## 验收

1. Slave、不可变包、不同激活实例和登记资源在同一目录可发现，URI 不泄漏 token、内网 Worker 地址或 instance_config。
2. 从 Slave DESCRIBE 到 run-package，再通过 Run read 取得真实子进程结果；请求关联和业务幂等分别验证。
3. 已激活命名实例的业务动作使用正确实例，不触发重新 provision；失效 revision、停用、跨 workspace 与模糊绑定拒绝。
4. 输入 Schema、重复键、未知字段、认证、deadline、action identity、binding 选择、SDK 关联和 token 转发边界都有针对性测试。
5. 持久化 repository 重载后仍可描述资源和读取任务；运行既有 API、Driver、Slave 与 SDK 回归。


## 实现落点

- `loom_orchestration/grip.py`：严格请求、回复、描述、URI 与 JSON 解码契约。
- `loom_orchestration/client.py`：可信引导、describe/invoke、Schema 校验与统一资源列表。
- `loom_v2/observer/grip.py`：权威数据投影、完整动作身份匹配和持久化任务提交。
- ComputeBinding 增加显式 instance_name 和 expected_activation_revision；Driver 对固定实例跳过 provision，Slave 在生命周期锁内验证并执行。stop 的 revision 比较进入 repository 的 desired-state 锁内。
- Slave 对普通进程包支持停用激活记录；不同命名实例停止时不再误判为同一个共享 runtime。
- 使用指南为 `docs/grip-resource-usage.md`；Compose 提供三个 GRIP 环境配置项。现有数据库 JSON 记录自然承载新增 binding 字段，不新增重复资源表或 migration。

首版 SDK 明确绑定一个 Observer/workspace 及其同源 endpoint；跨受众凭据交换和字节流没有发布为可用 binding。完整编排程序提交继续使用既有制作/执行 API，本次统一的是资源发现和调用入口。

## 首轮验证记录（全仓补齐前）

2026-10-19，本地环境验证：

- 全仓 `PATH=/root/loom_v2/.venv/bin:$PATH .venv/bin/pytest -q --disable-warnings --maxfail=5`：516 passed、30 skipped。跳过项包含依赖外部部署环境的测试及仓库原有禁用流程。
- 最后补充认证动作与 stop 幂等重试后，`tests/api/test_grip.py`：10 passed；协议契约测试此前 34 passed。
- 已覆盖真实子进程执行、HTTP Worker 命名实例传递、SQLite 重载后的资源/任务发现、停用后的最终 admission、跨 workspace、JSON/URI/认证/Schema、SDK 关联与凭据受众校验。
- `git diff --check` 与模块 compileall 通过。未运行真实云服务、TLS 反代或 Docker 部署验收；本次使用本地 ASGI/Worker 与实际子进程验证执行链路。

## 全仓收敛补充（2026-10-19）

上一轮仅完成通用入口，不能作为各真实服务包的接入验收。本轮将资源使用入口收敛应用到全仓：

1. workspace 提供 register-resource 动作；删除公开 REST slaves/capabilities/resources 发现、登记及 activation 查询端点。包制作、内容上传、完整计划提交保留制作接口；promote 只发布包，不再同时激活。移除 REST deactivate/auth/start。
2. 五个 capability_services 均提供完整 descriptor、Schema、IoContract、认证声明和可验证的发布示例。认证属于 activation 的 auth-start 和状态描述，不重复导出认证 HTTP 端点为业务 action。内部 HTTP 路由仍由 container:http runtime 解释。
3. SDK 提供从服务目录上传完整包的 ServicePackage，上传实际 Schema/validator/contract/descriptor 并使用服务器返回的精确引用；跨包依赖必须显式传入已发布包资源 URI，内部 descriptor 依赖按图解析，拒绝缺失或环依赖。示例镜像摘要由发布者明确提供，不假造可用镜像。
4. 每个服务用其实际 manifest、真实 gateway handler 和 container:http runtime 走发布→激活→DESCRIBE→业务 INVOKE→Run read→stop 的集成测试。外部云 API、kubectl/SSH 进程和 Docker runner 使用可断言的替身；不会将此结果标成真实云/Docker 验收。覆盖所有业务导出、输入/输出契约失败、认证等待、命名实例及版本 fencing。
5. README、五个服务指南、认证助手说明、SDK/skill、可执行示例和部署说明改为 GRIP。历史 spec/report 保留当时事实，在相关旧接口说明处标记已被本设计替代，不把历史实测结果改写成新协议的实测。
6. 内部编排绑定使用明确 instance_name，删除 ComputeBinding 中已被替代的 activation_ref 字符串拼接。NodeIntent 的 activation_ref 是内部持久化资源身份，继续服务既有编排账本。

验收需包括全仓旧入口引用扫描、五包参数化集成测试、生成物 --check、SDK wheel 安装、部署配置和全仓回归；交付报告分别列出自动化覆盖与尚需真实基础设施的验收条件。

实现补充：远程部署 TOML 增加 `grip_authority`、`public_base_url`、`grip_token_file`，token 只挂载到 Observer 并从预览隐藏。smoke 脚本改为 GRIP 发现；原 band-gap 的废弃 MCP/服务端 agent 模式删除，保留 SDK 完整计划验收和显式故障注入。两种 SSH 包的实例配置均要求 host/port/user，命名实例保持独立配置文件和容器生命周期。

## 全仓补齐后的验证

五个服务包共 32 个业务导出已通过实际包发布、GRIP、Driver/Slave、container:http 和本机 HTTP gateway 集成验证。
全仓回归 533 passed、30 skipped；最后补充浏览器认证 revision 并发保护后，GRIP 与五包定向复验 17 passed。
生成物检查、SDK wheel 安装、部署配置、CLI 和 diff 检查通过。测试替身和未执行的真实环境验收详见
[全仓迁移验证报告](../reports/2026-10-19-grip-repository-migration-validation.md)。
