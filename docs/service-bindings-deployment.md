# 服务 binding 部署与 AutoPaper 切换

本文件是配套部署步骤；2026-10-24 的实际部署与 AutoPaper 切换结果见
[生产记录](superpowers/reports/2026-10-24-grip-bindings-production.md)。SDK 0.8、Observer、Slave 和私有
runtime-plugin/2 应同时升级；Driver 保留任务执行职责。K8s 当前源码包版本为 v8
（v7 的认证恢复修复），embedding 为 embedding-http/v1。AutoPaper 南京实例已于
2026-10-25 升级到 v8，见 [认证恢复修复生产记录](superpowers/reports/2026-10-24-kubernetes-auth-recovery-production.md)。
按包摘要隔离的认证卷不会跨版本自动复制。

## 入口与密钥

用 operator 的密钥管理流程生成 Ed25519 PEM 私钥和公钥。私钥只安装到 Observer；
验证公钥安装到接收服务。部署工具读取以下配置，将 PEM 作为 Compose secret
分发，并从预览输出中隐藏。无需把 owner GRIP token 交给能力包。

```toml
[cluster.service_access]
signing_key_file = "secrets/service-signing.pem"
signing_key_id = "service-access-v1"
verification_keys = { service-access-v1 = "secrets/service-verify.pem" }
access_token_seconds = 3600
allow_insecure = false

[cluster.service_access.data_bindings]
"grip://loom.local/workspaces/workspace-default/slaves/slave-autopaper" = [
  { protocol = "https-json.v1", endpoint = "https://autopaper-grip.example/.well-known/grip", audience = "grip://loom.local/workspaces/workspace-default/slaves/slave-autopaper" },
  { protocol = "loom-wss-stream.v1", endpoint = "wss://autopaper-grip.example/grip/stream", audience = "grip://loom.local/workspaces/workspace-default/slaves/slave-autopaper" }
]
```

这些域名是示例，替换为调用者、服务容器及 Observer 可达且具有有效证书的地址。
同一机器上运行反向代理，将数据入口转到该机器的 `127.0.0.1:8091`，支持 WebSocket
Upgrade 和长连接，代理空闲超时大于 ping 周期。不要把 Worker 控制入口作为上游。
若 Observer 需要不同的后端地址，另设同结构的 `upstream_bindings`；该地址不会
覆盖 DESCRIBE 已显式给出的 endpoint。只发布 Observer 中转时可仅配置 upstream。

在新增 Slave 的 `[[machines]]` 项中配置唯一 service_id，例如 `slave-autopaper`，
以及 `grip_data_port=8091`、`grip_data_publish_host="127.0.0.1"`。新部署工具允许任意
符合 `slave-...` 命名规则的 ID。数据监听独立于内部服务端口；TLS 由该回环入口前的
代理终止。明确批准的隔离开发环境可启用 allow_insecure 并使用 HTTP/WS。

native AutoPaper 入口由该 Slave 的 machine 配置批准：

```toml
[machines.consumer_ports."grip://loom.local/workspaces/workspace-default/packages/embedding-http/v1:autopapers"]
host_port = 8001
container_port = 8081
```

该表必须跟在对应 `[[machines]]` 项后。runtime 固定发布到宿主机 127.0.0.1，拒绝
将私有 runtime 端口作为消费端口。包 manifest 不包含宿主机监听请求。

## 构建和发布

两个新 SDK 服务镜像均以仓库根目录作为构建上下文：

```sh
docker build -f capability_services/kubernetes_http/Dockerfile -t <registry>/kubernetes-http:v8 .
docker build -f capability_services/embedding_http/Dockerfile -t <registry>/embedding-http:v1 .
```

推送镜像后使用实际 registry digest，通过 `ServicePackage.publish` 或
`scripts/publish_service_package.py` 发布目录。不要把示例 image_ref 占位符用于发布。
SDK wheel/skill 由 `scripts/build_agent_skill.py` 构建。

## 配置服务与依赖

1. 在 AutoPaper VM 上部署 Slave，并为南京 K8s 选择它自己的 credential_profile。
   激活 kubernetes-http/v8 后按 auth-start 完成首次登录；后续同版本重启复用
   `/var/lib/loom/service/kube` 的 kubelogin 缓存。
2. 通过 GRIP 登记一个 `network/service` 资源，例如
   `resources/services/autopapers-embedding`，attributes 指向对应 Kubernetes 资源。
   用 K8s 激活的 configure 设置 service_targets，例如：

```json
{
  "service_targets": {
    "grip://loom.local/workspaces/workspace-default/resources/services/autopapers-embedding": {
      "namespace": "dev-yuzishu",
      "name": "autopapers-embedding-access",
      "port": 8001,
      "selector": {"volcano.sh/job-name": "autopapers-embedding"},
      "workload": {"resource": "vcjob", "name": "autopapers-embedding", "uid": "<从集群查询的实际 UID>"}
    }
  }
}
```

3. 调用 services-ensure 并查询其 Run；确认普通 ClusterIP Service 的 UID、selector、
   端口和 EndpointSlice，查询 service-access-status 到 ready。现有 headless Service
   保持用途不变。当前账号需有读工作负载/Service、创建或更新该 Service，以及
   pods/portforward 权限；能力包不会修改 RBAC。
4. 激活 embedding-http/v1，instance_config 指定 service_ref 和 dependency 名，
   dependencies 授权目标 K8s 激活的完整 open-service-stream 动作 URI（可加状态查询）。
   Slave 在 30 秒周期内写入并刷新该实例的私有上下文。检查应用连接状态；owner token
   和 kubeconfig 都不进入 embedding 容器。
5. 查询模型 info 和单条/批量 embedding，核实实际模型名、2560 维和有限数值后登记
   `model/embedding`。若要绑定该资源为激活 target，应先通过 K8s 的 GRIP 字节流查询
   模型并登记资源，再执行步骤 4 的首次激活。用实际结果填写 model_name、dimensions、
   kubernetes_ref，不能从目录名称推断或试图修改已有激活的 target。
6. 在 AutoPaper VM 测试 localhost:8001 的 /health、/info、/embed-one、/embed，并运行
   AutoPaper 客户端检查。只有新入口通过后才停止并删除旧手工转发 systemd 服务及脚本。
   记录原配置和切换点；不修改向量库数据，不触发重建索引。

故障恢复测试使用隔离工作负载：替换 Pod、终止转发进程、短暂网络中断、同版本
容器/插件/Slave 重启。旧请求应失败，新请求应恢复。授权撤销后重新认证同一实例，
确认资源 URI 保持不变。不要为了测试删除生产模型 Pod。
