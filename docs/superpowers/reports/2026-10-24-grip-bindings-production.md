# GRIP binding 与 AutoPaper 生产部署记录

## 状态

2026-10-24：生产核心已部署，AutoPaper 已切换至本机 embedding 能力包消费入口。
新南京 K8s 与 embedding 激活均为 ready、revision/applied_revision 均为 1。
首次认证完成后，模型调用、现有库检索和五类恢复测试均通过。

## 已完成

- `9.0.16.66` 的 Observer、Driver、slave-a 升级至本次实现，保留数据库和已有
  激活实例。升级前备份配置、代码、镜像和两个 PostgreSQL 数据库，并检查备份可读。
- `9.0.16.113` 部署 `slave-autopaper`，独立数据入口为
  `https://9.0.16.113:8092/.well-known/grip` 和
  `wss://9.0.16.113:8092/grip/stream`。TLS 由本机 Nginx 终止，后端数据端口只绑定回环。
  私有 CA 分发至消费者和 Observer，验证了严格证书校验。
- 发布 `packages/kubernetes-http/v7` 和 `packages/embedding-http/v1`，两个主机的
  registry 保持相同不可变镜像摘要。新南京激活位于
  `activations/kubernetes-http/v7/slave-autopaper/nanjing`，独立认证存储，未复制旧登录缓存。
- 登记 `resources/services/autopapers-embedding`，指向新 K8s 激活。该实例管理
  `dev-yuzishu/autopapers-embedding-access` 普通 ClusterIP Service，UID 为
  `d7de1e6c-8cb6-4a57-816f-0c811e164ecc`，selector 为
  `volcano.sh/job-name=autopapers-embedding`，端口为 8001。EndpointSlice 的 ready
  端点指向节点 `vm-11-0-11-6-ubuntu` 上的 `autopapers-embedding-embedding-0`。
- 登记 `resources/models/autopapers-embedding`，MCUE 为 `model/embedding`，绑定
  `activations/embedding-http/v1/slave-autopaper/autopapers`。通过实际 `/info` 验证
  模型为 `Qwen/Qwen3-Embedding-4B`、2560 维、CUDA。
- AutoPaper 当前配置与旧转发 unit、脚本已备份。数据库现有 `paper_abstracts` 集合
  为 2560 维、22,880 条；切换前后数量相同，没有重建索引。

## 部署中发现并修复

- 嵌入式数据 Uvicorn 服务不再安装自己的信号捕获器，由父服务生命周期处理停止。
- AutoPaper VM 使用 Docker 20.10，不支持一次 create 指定多个网络。运行时改为先
  创建内部网络容器，再连接 operator 批准的 egress 网络，完成后才 start。连接失败
  会删除未启动容器；不会让配置不完整的容器运行。
- 新凭据目录保持私有权限，并赋予能力包运行 UID 读取权限。
- 真机字节流测试发现未绑定业务资源的激活读取了序列化包中不存在的 `version_ref`。
  运行时改用既有包身份解析函数，补充绑定和未绑定两种实际 WebSocket 测试，并部署至
  两个 Slave 的 runtime plugin。

部署增量回归：`test_worker_api.py`、`test_grip_service_bindings_api.py`、
`test_container_http_runtime.py` 合计 **46 passed**。
字节流身份修复后，容器运行时和插件测试合计 **33 passed**。

## 验收与回退

| 验收项 | 实测结果 |
|---|---|
| GRIP 直连及 Observer 中转 | info 结果相同；直接 embed 与上游同批输入最大绝对差为 0；这些服务调用新增 Run 数为 0 |
| 模型资源 URI 调用 | 直接以 `resources/models/autopapers-embedding` 调用 info 成功 |
| AutoPaper 实际客户端 | /health、/info、/embed、/embed-one 通过；2560 维、有限值、归一化检查通过 |
| 单条与批量数值比较 | 同文本最大绝对差 0.001953125，小于验收容差 0.01 |
| 现有库检索 | 切换前后均返回 5 条；集合数量均为 22,880 |
| 应用切换 | `deploy/config.yaml` 改为 `http://127.0.0.1:18001`，重启 backend 后用该实际配置再次验收通过 |
| HTTP 检查 | 公网首页、本机首页和 OpenAPI 返回 200；未认证 /api/me 返回 401 |
| 旧入口清理 | 停用并删除旧 autopapers-port-forward unit 和脚本，保留备份 |

模型数据路径为 AutoPaper → 本机 embedding 消费端口 → 本机 Slave 的 WSS binding →
本机 K8s 激活的受管 port-forward → 南京现有模型 Pod。Observer 负责发现和授权刷新，
Driver 不参与模型数据传输；外部调用也可显式选择 Observer 中转。

对新访问组件执行的恢复测试如下，成功后才切换 AutoPaper；没有删除生产模型 Pod。

| 重启或终止对象 | 恢复耗时 |
|---|---|
| port-forward 子进程 | 2.365 秒 |
| K8s 能力包容器 | 2.933 秒 |
| embedding 能力包容器 | 2.779 秒 |
| runtime plugin 进程 | 2.310 秒 |
| Slave 容器 | 4.482 秒 |

每项均观测到短暂请求失败，后续新请求恢复；认证缓存复用成功，无需再次登录，激活 URI
与 revision 保持不变。上述耗时是这次恢复检测的实测值，不是 SLA。未执行生产 Pod 替换、
网络故障注入或主动撤销用户认证，也未借用用户网页会话调用带登录要求的完整搜索 API；
检索验收使用 AutoPaper 原生 EmbeddingClient 与现有 Chroma 集合。

新增数据入口使用 HTTPS/WSS，严格验证私有 CA。现有 Observer 控制入口仍为部署原有的
`http://9.0.16.66:8080`，本次没有把该既有入口迁移为 HTTPS。

消费地址 `http://127.0.0.1:18001` 只发布 embedding 的独立消费端口。备份在 VM 的
`/opt/loom-production/releases/grip-bindings-20261024/autopaper-backup/`；核心部署备份
在 `9.0.16.66:/opt/loom-production/releases/grip-bindings-20261024/backup/`。
需要回退应用时恢复其中的 config.yaml、旧 unit 与脚本，daemon-reload 后重启 backend
并恢复旧转发 unit。旧手工入口在切换前已连接失败，恢复配置本身不能保证旧模型入口可用。

升级前北京旧 K8s 实例已为 degraded，算力云 SSH 旧实例已等待认证；升级后没有把
这些既有状态算成本次新增故障。其余原先 ready 的业务实例仍为 ready。
