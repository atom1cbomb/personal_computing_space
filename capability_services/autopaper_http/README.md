# AutoPaper 论文查询能力包

`autopaper-http/v1` 连接 [AutoPaper](https://paper.cs.ac.cn)，提供论文搜索、详情、
会议/年份浏览和会议目录。接口按 2026-10-26 读取的
[Auto-Papers API 0.2.0 OpenAPI](https://paper.cs.ac.cn/openapi.json) 实现。
发布与激活遵循[通用服务包流程](../README.md)。

| action 名称 | 输入 | 返回的 result | 调用方式 |
|---|---|---|---|
| `papers_search` | `keywords` 或 `description`；可选 `venues`、`start_year`、`end_year`、`min_similarity`、`top_k`、`session_id` | `session_id`、`total`、`papers`、`messages` | task，返回 Run |
| `papers_get` | `paper_id` | 论文详情 | request-reply |
| `papers_list` | `venue`、`year` | 论文列表 | request-reply |
| `venues_list` | `{}` | 已入库会议和论文数量 | request-reply |
| `years_list` | `venue` | 已入库年份和论文数量 | request-reply |
| `venues_catalog` | `{}` | 会议缩写、全名、CCF 分类和别名来源 | request-reply |

所有业务结果为 `{action, result}`。论文保留上游的作者、摘要、DOI、来源链接、
PDF 链接、已有结构化摘要和相似度等字段；缺失的可选字段不补造。
`papers_get` 返回已有摘要和链接，不触发 PDF 下载或摘要生成。

搜索默认 `top_k=50`、`min_similarity=0.5`，`top_k` 范围为 1–200。
关键词或研究描述至少有一个非空；年份范围必须有序。搜索固定发送 `auto_ingest=false`，
仅查询已有论文，避免 AutoPaper 默认的自动抓取入库。上游仍会保存搜索会话/历史，
所以该动作使用 `autopaper.search`、`NotReplaySafe`，由 task 执行并返回 Run。
其余动作使用 `autopaper.read`、`SafeRead`，直接返回业务结果。
会议目录包含尚无论文的会议；查询已有数据时先使用 `venues_list` 和 `years_list`。

上游按会议和年份列出论文的接口没有分页参数，能力包保留完整结果；响应超过 32 MiB
时明确失败，不返回截断列表。业务请求超时为 120 秒，认证探测为 10 秒，失败不自动重试。

## 配置与认证

默认 `instance_config={}` 即连接 `https://paper.cs.ac.cn`。其他部署可设置
`{"api_origin":"https://paper.example"}`，必须为无路径、查询串或内嵌凭据的 HTTPS origin。
API 与浏览器登录使用同一 origin，业务输入不能覆盖地址。

宿主 Slave 在节点接入时准备 GRIP 数据入口或 Observer 中转上游，供该节点所有服务包复用。
部署 AutoPaper 时先调用 `check-package`；仅有 task 执行通道的节点会报告
`binding_unavailable` 并拒绝激活，不应通过 SSH 临时修补节点。
节点配置方法见[服务入口接入步骤](../../docs/service-bindings-deployment.md)。

激活时省略 `credential_profile`，初始状态为 `pending_auth`。在用户浏览器所在机器
安装 SDK 的 `auth` 可选依赖与 Chromium，然后使用[统一登录客户端](../../docs/authentication.md)：

```sh
loom-auth --observer "$LOOM_OBSERVER_URL" --token-file /private/loom-token \
  --activation "$AUTOPAPER_ACTIVATION_URI"
```

激活 URI 使用 Observer 返回的值。HTTP Observer 显式加 `--allow-http`。
在本地登录页选择该实例，浏览器打开 AutoPaper `/oauth2/login`，完成原站登录。
SDK 从它自己的隔离浏览器上下文读取 `ap_sid`，经认证桥交给网关。
网关实际读取 `/api/papers/venues` 并验证结果后才保存会话；等待实例 `ready/authenticated`
后调用业务动作。无需在聊天、manifest、instance_config 或业务输入中填写 Cookie。

登录挑战五分钟有效且只接受一次回调；重新认证替换旧挑战。
已验证会话以 0600 权限保存在当前激活的持久卷 `/var/lib/loom/service/autopaper-session.json`，
同时绑定 API origin。同版本重启后重新验证会话，修改 origin 后需要重新登录。
凭据失效或登录跳转返回 `auth_required`；网络、权限、限流和异常响应返回 `auth_failed`。
实际会话失效时在统一登录页重新认证同一实例即可。

## 构建、发布和调用

从仓库根目录构建，构建上下文使用包目录：

```sh
docker build -t "$AUTOPAPER_IMAGE_TAG" capability_services/autopaper_http
docker push "$AUTOPAPER_IMAGE_TAG"
python scripts/publish_service_package.py capability_services/autopaper_http \
  --image "$AUTOPAPER_IMAGE_REF"
```

`AUTOPAPER_IMAGE_REF` 使用推送后仓库返回的不可变 `registry/repository@sha256:...`。
发布器上传 Schema、descriptor 和 IoContract，再由 Observer 返回包 URI。
镜像内包含契约校验使用的 `schemas/`；无需在容器内安装浏览器。

在已有 `LoomClient` 中激活（`slave_uri` 来自资源发现，`package_uri` 来自发布结果）：

```python
check = await client.invoke(slave_uri, client.names.action("check-package"), {
    "package": package_uri, "instance_name": "autopaper",
})
if not check["ready"]:
    raise RuntimeError(check["issues"])

activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "autopaper",
    "instance_config": {},
    "idempotency_key": "activate-autopaper",
})
target = activation["resource"]
```

完成登录后重新 DESCRIBE，按动作名称选择当前可用 offer：

```python
description = await client.describe(target, refresh=True)
metadata = description["resource"]["metadata"]
assert metadata["available"] and all(a["available"] for a in metadata["actions"])
offers = {offer["action"]["uri"] for offer in description["offers"]}
actions = {a["name"]: a["uri"] for a in metadata["actions"] if a["uri"] in offers}

venues = await client.invoke(target, actions["venues_list"], {})
submitted = await client.invoke(target, actions["papers_search"], {
    "arguments": {
        "keywords": ["retrieval augmented generation"],
        "description": "检索增强生成的检索质量与评估方法",
        "venues": ["ACL"], "start_year": 2023, "end_year": 2026, "top_k": 10,
    },
    "expected_revision": metadata["revision"],
    "idempotency_key": "rag-search-1",
})
run = await client.invoke(submitted["run"], client.names.action("read"), {})
# 继续轮询同一 Run，完成后从 run["outcome"]["value"]["result"] 读取搜索结果。
```

同一次提交重试复用原参数和幂等 key；新的搜索使用新的 key。
搜索结果的论文 ID 可传给 `papers_get`，例如 `{"paper_id":"<返回的 id>"}`。

## 验证

```sh
python scripts/generate_service_contracts.py --check
python -m pytest -q tests/capability_services/test_autopaper_gateway.py \
  tests/capability_services/test_autopaper_grip.py \
  tests/capability_services/test_service_contract_artifacts.py
```

测试涵盖六个动作、参数和结果校验、浏览器挑战隔离/失效/重放、会话持久化与过期恢复，
以及实际 GRIP 发布 → 激活 → 认证桥 → HTTP runtime → 网关 → Run/直接结果的完整路径。
上述 GRIP 集成测试的 Docker 和上游服务使用替身。

构建镜像后可显式启用真实 Docker 验收：

```sh
docker build -t loom/autopaper-gateway:worktree-test capability_services/autopaper_http
LOOM_AUTOPAPER_TEST_IMAGE=loom/autopaper-gateway:worktree-test \
  python -m pytest -q tests/capability_services/test_autopaper_docker.py
```

该测试启动真实网关容器和 HTTPS 上游替身，使用新建的独立网络和持久卷，完成后清理。
容器按 runtime 的 UID 65534、只读根文件系统和权限限制启动，校验全部六个动作、登录、
0600 会话文件、重启和删除重建后的会话恢复、实例隔离、过期重新认证及上游故障。
HTTPS 使用测试专用 CA，保持证书校验。默认测试不启用 Docker 验收，须设置上述镜像变量。

2026-10-26 真实 Docker 验收发现并修复了新持久卷的写权限问题：镜像现在预先创建
`/var/lib/loom/service` 并设置 UID/GID 65534，使 Docker 新卷继承正确的目录所有者。
修复后真实容器验收通过。容器内公网检查也通过：公开目录返回 95 个会议并符合输出契约，
受保护的会议接口未登录返回 401，登录入口返回 HTTPS 307 跳转。
生产账号的论文查询与检索验收结果见下节。

## 生产部署（2026-10-26）

以下记录当时生产版本的验收。新的激活前检查需升级 Observer 后生效；
其中手工补数据入口的操作属于节点接入修补，不是后续部署 AutoPaper 的步骤。

代码已合并至主分支，合并提交 `d1cc0d4`。包通过 Observer 发布，在 `slave-a` 激活：

```text
grip://loom.local/workspaces/workspace-default/packages/autopaper-http/v1
grip://loom.local/workspaces/workspace-default/activations/autopaper-http/v1/slave-a/autopaper
```

镜像为 `localhost:5000/loom/autopaper-gateway@sha256:538fe875e4d2a6e3624726f3228236b23b9acd521b497f3050162226e2308137`。
用户完成统一浏览器登录后，实例为 `ready/authenticated`，revision 为 1，六个动作均可调用。

本次补齐了 slave-a 的数据监听和 HTTPS 上游，查询通过 Observer 中转。
仅重新配置 Observer 和 Slave 主进程，未更换核心镜像；有效激活的容器和认证卷保持不变。
Slave 恢复时清理了此前已经标记 `stopped` 的六个历史容器（Grafana v4、Kubernetes v6
各三个）；原先所有 `ready` 激活仍为 `ready`。
配置及回退副本在部署主机 `/opt/loom-production/releases/autopaper-capability-20261026/`，
TLS 代理配置在 `/opt/loom-production/slave-a-grip-tls/`。

| 验收项 | 实测结果 |
|---|---|
| 合并后回归 | 317 passed、1 skipped；跳过项为需显式设置镜像的 Docker 验收，之前已单独通过 |
| 会议目录 / 已入库会议 | 95 / 37 |
| 年份与论文列表 | ACL 2025，3,379 篇 |
| 论文详情 | 成功读取 GraphNarrator 的标题、作者、摘要等已有字段 |
| RAG 检索 | Run `run-757269a17256` completed，共 46 条匹配，本次返回前 5 篇，包括 MultiRAG、RAG-Critic |
| 生产容器重启 | 原激活 URI 和 revision 保持不变，会话复用，重启后查询返回 37 个会议 |
| 会话文件 | UID 65534，权限 0600 |

本次传入 `top_k=5`；响应中的 `total=46` 是匹配总数，`papers` 包含实际返回的 5 篇论文。
