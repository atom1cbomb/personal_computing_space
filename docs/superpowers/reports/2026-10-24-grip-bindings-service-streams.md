# GRIP binding 与服务流实现验证

日期：2026-10-24。依据 [已评审 spec](../specs/2026-10-23-grip-direct-bindings-and-service-streams-design.md)。
本报告保留本地实现与验证记录。后续已完成生产部署和 AutoPaper 切换，实际结果见
[生产部署记录](2026-10-24-grip-bindings-production.md)。

## 已实现的行为

- SDK 按 DESCRIBE 的受信 binding 选择 INVOKE 入口；支持独立服务、可选 Slave 数据入口和 Observer 中转。
  同一 action 的路径选择不改变 mode、Schema 和结果语义。发送后不自动换路或重放。
- task 保留 Run；request-reply 直接返回业务结果；byte-stream 通过 WSS 建流，SDK 提供 read/write/aclose。
  私有 runtime 协议升级为 `loom.runtime-plugin/2`，复用 FastAPI/Uvicorn 和 websockets。
- Observer 签发按 audience、workspace、target、action、activation revision 限定的 Ed25519 凭据。
  中转重新签发受限后端凭据，保留主体；能力包只获得声明依赖的短期凭据，不获得 owner token。
- configure 持久化配置和依赖，返回 desired_revision/applied_revision/state；同修订参数冲突拒绝执行。
  修订变化撤销相关连接。容器配置摘要匹配后才报告 ready，避免重启继续使用旧配置。
- Slave 在 Observer 暂不可用时仍检查本地容器；可恢复故障退避重试，失败配置等待显式修复。
  Driver 的持久任务路径完整转发已有激活依赖配置。
- kubernetes-http/v7 持久保存认证缓存，增加 services-ensure、service-access-status、open-service-stream。
  Service 校验工作负载 UID、ownerReference、selector 和保存的 Service UID；转发进程退出后恢复新连接，
  旧流中止。正常 kubectl 连接日志不会误报配置故障。登录等待、RBAC 拒绝和身份故障分别显示。
- embedding-http/v1 通过具名 K8s 依赖访问模型，使用 HTTPX/httpcore 解析通道中的 HTTP；
  提供直接 embed/info 与独立 AutoPaper HTTP 接口。新请求要求凭据剩余寿命覆盖执行预算。
  operator 可以将消费端口发布到宿主回环地址，私有 runtime 端口不能作为消费端口。
- 新增 network/service、model/embedding MCUE；部署配置支持第三个及更多具名 Slave、签名密钥、
  可选数据端口和批准的消费端口，并检查端口冲突和签名/验证密钥匹配。

## 测试结果

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests -q --disable-warnings --maxfail=5
```

结果：**801 passed，34 skipped，221.35 秒**。跳过项包含依赖外部 Docker/Compose 环境的验收，
以及现有已停用协议用例；不能据此宣称完成生产或真实 Docker 验收。

最后补齐 Driver 的依赖转发后，相关回归：

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/driver tests/api/test_grip_service_bindings_api.py -q --disable-warnings --maxfail=2
```

结果：**135 passed，9 skipped，69.82 秒**。其中包含新增慢消费者场景；该场景单独运行也为 **2 passed**。
这些数字与全仓测试重叠，不累加为通过总数。

主要证据：

| 验证边界 | 证据 |
|---|---|
| 不依赖接收端角色的 SDK | 独立 GRIP 服务的 HTTPS JSON 受信路由、凭据隔离、Schema/描述缓存；独立服务真实 WS 双向通信 |
| 真实流与背压 | Slave 直连、Observer 中转均通过真实 ASGI/WebSocket；32 MiB 写入在消费者阻塞时受阻，恢复后完整接收，运行时仅建流一次 |
| TLS 与授权 | 自签测试 CA 验证 WSS；到期流关闭；错误受众、workspace、action、过期凭据和旧修订均在 runtime 调用前拒绝 |
| 撤销与依赖 | stop/revoke 中止已建立流；configure 拒绝旧 token；私有依赖上下文仅含批准动作，0400 权限，删除依赖后旧上下文失效 |
| runtime 传输 | 实际 Unix socket WebSocket 经过 ProcessRuntimePlugin、PluginServer 到 TCP 上游 |
| 重启恢复 | 保存新配置后仍存在旧容器时禁止调用，reconcile 重建并应用新配置；本地恢复退避、failed 状态不被启动 reconcile 清除 |
| K8s 转发 | 实际测试子进程监听回环端口，模拟 kubectl 日志与连接；登录等待不发起集群业务调用，完成后自动恢复；杀进程恢复，Service UID 更换停止 |
| 模型适配 | 实际 HTTP 上游 → K8s WS → HTTPcore → embedding 接口；90 KiB 文本、单条/批量、顺序、normalize/instruction、输出校验和错误不重放 |
| 现有包 | 原有七个能力包的当前模板及生命周期仍经过 GRIP/runtime 测试，K8s 覆盖新增三种交互导出 |
| 部署 | 多 Slave inventory、端口冲突、私钥仅分发 Observer、公钥安装及密钥不匹配拒绝 |

Docker、外部 Kubernetes API/模型响应及 provider 在本地测试中仍有替身。
背压测试验证阻塞传播、消息分块和单次调用，不是生产 RSS 或吞吐基准。

其他检查通过：生成契约 `--check`、Python compileall、`git diff --check`、skill 格式校验。

## SDK、skill 与文档

- 独立 SDK 版本为 **0.8.0**，构建产物在 `dist/loom_orchestration-0.8.0-py3-none-any.whl`。
  新建隔离虚拟环境安装 wheel，确认导入 LoomClient、DependencyContext、流接口、HTTP transport，
  且环境中不存在 loom_v2 服务端包。
- skill 发布包为 `dist/personal-computing-space.zip`，包含 SDK wheel。
  本机 `/root/.codex/skills/personal-computing-space` 链接到仓库 skill，说明已同步生效；
  与压缩包中的说明逐文件一致。构建 wheel 只保留在 dist 与发布包中。
- 使用说明见 [GRIP 服务 binding](../../grip-service-bindings.md)、
  [部署与 AutoPaper 切换](../../service-bindings-deployment.md)，能力包 README 与 SDK/认证制作 skill 已更新。

## 本地验证与生产验收的边界

实现时通过已授权 VM SSH action 只读核对了 AutoPaper 的实际 HTTP 客户端和模型服务源码，
适配 `/health`、`/info`、`/embed`、`/embed-one` 字段。没有将新镜像或新入口部署到生产。

后续生产验收已完成新数据入口 TLS/密钥、Slave 安装、包发布、南京 K8s 首次授权、普通
Service 与 EndpointSlice 检查，以及真实模型身份、2560 维输出和数值一致性验证。
同版本恢复、AutoPaper 切换和旧手工转发清理见独立生产记录；本地替身测试不作为这些
生产检查的证据。现有向量库无需重建，也未执行索引重建。
