# Kubernetes 认证恢复修复生产升级

## 当前状态

2026-10-25 北京时间 08:58：用户完成登录后，AutoPaper 南京实例升级与验收完成。
`kubernetes-http/v8` 为 `ready / authenticated`，revision/applied_revision 均为 1；
embedding 激活已应用指向 v8 的依赖，revision/applied_revision 均为 2，状态为 ready。
旧南京 v7 激活已停止，revision 为 2。停用旧实例后的实际模型调用和现有向量库检索通过。

新实例：
`grip://loom.local/workspaces/workspace-default/activations/kubernetes-http/v8/slave-autopaper/nanjing`

## 已完成

- 工作区内网恢复后，确认 Observer 和两台主机可访问；保存现有资源描述、实例配置、
  凭据 profile 选择与 embedding 依赖作为切换及回退依据。
- 以此前 v7 的不可变镜像为基础构建修复镜像，仅替换 Kubernetes 能力包目录。
  镜像内 `server.py` 和 `service_access.py` 的 SHA-256 与已验证源码一致。
- 经公开 SDK 发布 v8，包摘要为
  `58890e911842d1a86ac6fd973a098d72b99f9e4440151198607825d983601fc6`。
- 两台主机的 registry 均已具备完整、相同的 OCI 镜像图，AutoPaper 主机实际拉取成功：
  `localhost:5000/loom/kubernetes-gateway@sha256:96a4fbea8509cafc307a71f2ac44c2c09065ac5d372894ea1b9c5d1cb701b286`。
- 保留南京 v7 的 namespace、Service selector、工作负载 UID 和凭据 profile 选择，
  通过公开 `activate-package` 创建 v8 的独立激活；未复制另一版本的认证缓存。
- 在生产主机以实际 v8 镜像运行隔离冒烟检查，网络关闭、使用本地假 kubectl：首次
  返回 `transport_error`，恢复条件后自动变为 `authenticated`，未调用 `auth-start`。
  这是镜像中的恢复逻辑检查，不是生产 Kubernetes 故障注入或实际登录验收。
- 用户通过统一 SDK 登录入口完成新南京 v8 实例的独立授权。

核心主机使用 OCI index，旧版 Docker 的 save/load/push 会产生不同的 manifest 摘要。
部署时通过 SSH 回环隧道补齐原始 manifests 与缺失 blobs，并校验摘要；未改写已发布
包的镜像引用。凭据未放入镜像、包内容或公开资源描述。

## 切换与验收

升级前南京 v7 为 `degraded / auth_failed`，AutoPaper 的本地 embedding `/health`
返回 `embedding_unavailable`。slave-a 上三个 v6 K8s 实例也已处于 degraded，
本次未升级 slave-a 上这三个旧实例。

| 验收项 | 实测结果 |
|---|---|
| Kubernetes action | resources-list 和 services-ensure 的 Run 均完成；模型 Pod 继续在 `vm-11-0-11-6-ubuntu` 运行 |
| Service 身份 | UID 仍为 `d7de1e6c-8cb6-4a57-816f-0c811e164ecc`，未重建模型 Pod 或 Service |
| v8 WSS 通道 | 读取实际模型信息并完成双文本推理，模型为 Qwen/Qwen3-Embedding-4B、CUDA、2560 维 |
| 直接与中转调用 | 模型资源的直接和 Observer 中转 info 一致；直接 embed 与同批上游结果最大绝对差为 0 |
| AutoPaper 原生客户端 | /health、/info、单条和批量 embedding 均通过；输出有限、2560 维、归一化检查通过 |
| 单条与批量比较 | 同文本最大绝对差 0.001953125，小于验收容差 0.01 |
| 现有向量库 | 检索返回 5 条，检查前后均为 22,880 条；未重建索引 |
| 应用入口 | 实际配置仍为 `http://127.0.0.1:18001`；首页和 OpenAPI 为 200，未登录 /api/me 为 401 |
| v8 容器重启 | 约 2.816 秒恢复，观察到 4 次失败探测；复用持久认证缓存，没有再次调用 auth-start |
| 重启后复查 | 超过一个 30 秒认证复查周期后仍 ready/authenticated，服务访问 ready、last_error 为 null |
| 停用旧实例 | v7 stopped 后，再次通过原生客户端、向量库检索、直接及中转调用，确认未依赖旧实例 |

配置切换期间首次模型资源 info 调用返回 internal_error，之后的新请求恢复成功；
验收继续执行时没有再次修改 embedding 配置。配置已应用和容器存活不等同于上游立即可用，
因此以实际模型调用、原生客户端和重启后复查作为本次完成依据。

Service 与模型资源的 `attributes.kubernetes_ref` 均更新为新 v8 激活，属性修订均为 2。
Service 的原始 `metadata.package` 仍为 v7：该关联在当前协议中不可改写；实际连接选择由
新的 `kubernetes_ref`、v8 的 service_targets 与 embedding 依赖共同确定。资源 URI 不变。

本次没有向真实 Kubernetes 集群注入网络故障，也未主动撤销用户认证；网络错误自动重试
的证据来自本地回归和生产镜像内的隔离检查。真实环境验收覆盖登录、访问、切换和容器重启。

## 操作记录与回退

操作材料和配置备份位于工作区 `production/kubernetes-auth-recovery-20261024/`。
切换程序 `finish-cutover.py` 在新实例 ready 且上游试调用成功前不会更新 embedding 依赖。
两台生产主机的镜像材料目录为
`/opt/loom-production/releases/kubernetes-auth-recovery-20261024/`。

最终状态、原生客户端输出及重启结果分别保存在 `final-states.json`、
`autopaper-final-acceptance.json`、`restart-recovery.json`。如需回退，先恢复并验证旧 v7
激活可用，再使用最新 revision 的 configure 恢复备份的 embedding 依赖，并通过
update-attributes 恢复资源关联。旧 v7 在升级前已经 auth_failed，恢复旧配置本身不能
保证旧连接可用。

修复原理及本地 256 项回归结果见 [修复记录](2026-10-24-kubernetes-auth-recovery.md)。
