# CS101 成绩分析能力包

`cs101-http/v2` 是 `service + container:http/1` 能力包，按 CS101 的
`/api/v2/openapi.json` 提供五个只读动作：

| action 名称 | 功能 |
| --- | --- |
| `papers_list` | 按关键词、类别列出试卷，返回试卷 ID |
| `grades_list` | 分页查询成绩，返回学生 ID、姓名、班组、试卷及原始分数 |
| `paper_statistics` | 获取试卷的上游统计、题目信息和原始评分 context |
| `grades_analyze` | 整卷成绩分布、均值、中位数、总体标准差、班组汇总和可选排名 |
| `student_analyze` | 按学生 ID 分析各试卷成绩及其在同卷样本中的排名 |

AI 的调用规范、字段含义与结果解释见 [HOW_TO_USE.md](HOW_TO_USE.md)。

## 配置与凭据

每个实例必须显式设置一个 HTTPS origin；网关自行追加 `/api/v2`：

```json
{"api_origin": "https://test.cs101.gbu.edu.cn"}
```

可配置不同学校的地址及端口；业务请求不能覆盖地址。网关只访问配置的精确 origin，
禁用环境代理、重定向、OAuth 发现和远程 Schema 解析。配置拒绝
`iaaa.gbu.edu.cn` 及其子域名。上游响应中的链接、题目、`config`、
`program`、`context` 均不会触发额外网络请求或代码执行。固定上游白名单为：

- `GET /api/v2/users/me`，用于验证凭据。
- `GET /api/v2/papers`。
- `GET /api/v2/papers/{正整数}/statistics`。
- `GET /api/v2/paper-statuses`。

测试入口的 OpenAPI 为 `https://test.cs101.gbu.edu.cn/api/v2/openapi.json`；
人工查看页面为 `https://test.cs101.gbu.edu.cn/login?manual=show`。
浏览器登录使用该站点自己的登录页，整个 SSO 跳转由有界面的 Chromium 完成。
网关不访问 IAAA，也不从 IAAA 获取、交换或转发凭据。

管理员在 Slave 的 credential profile 目录中准备 `cs101_token` 文件，
内容为原始 token，不含 `Bearer ` 前缀。凭据文件需允许容器 UID 65534 只读访问，
由 Slave 挂载至 `/run/loom/credentials/cs101_token`；不要放入代码、镜像、
manifest、`instance_config`、Run 输入或文档。有 token 时，激活时传 profile 名称：

```python
config = {"api_origin": "https://test.cs101.gbu.edu.cn"}
check = await client.invoke(slave_uri, client.names.action("check-package"), {
    "package": package_uri, "instance_name": "cs101-test", "instance_config": config,
})
if not check["ready"]:
    raise RuntimeError(check["issues"])
activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "cs101-test",
    "instance_config": config,
    "credential_profile": "cs101-test",
    "idempotency_key": "activate-cs101-test",
})
```

没有 token 时，省略上例的 `credential_profile`，实例进入 `pending_auth`。
在用户桌面安装带 `auth` extra 的 SDK 0.15.0 和 Chromium，运行统一登录入口：

```sh
loom-auth --observer "$LOOM_OBSERVER_URL" --token-file /private/loom-owner-token \
  --activation "$CS101_ACTIVATION_URI"
```

这里的 token 文件是 Loom 自身的访问凭据，不是 CS101 token。打开本地门户
`http://localhost:3000/`，选择实例并点击登录。在弹出的浏览器窗口里使用 CS101 的
统一认证按钮，或者使用页面的手动 token 输入框。客户端只读取 CS101 自身 origin 的
`localStorage["token"]`，通过私有桥回传原始字符串；网关解码 JSON 字符串，并使用
Bearer token 读取 CS101 `/users/me` 验证后才接受登录。

“自动复用会话登录”开关及 `--auto-login` 同样打开有界面窗口，复用私有浏览器档案中的
CS101 本地存储和 SSO 会话。需要点击统一登录、输入密码、扫码或授权时仍由用户操作；
自动尝试超时后在门户保留手动登录入口。两种流程都不使用无头浏览器；显示环境不可用时
报错，不退回无头模式。可用 `--auth-state-dir` 为不同账号选择不同档案。

认证声明为系统统一的 `browser-storage`，需要匹配的 SDK 0.15.0、Observer 和 HTTP runtime。
浏览器 token 经验证后，以 0600 权限保存到实例独占的
`/var/lib/loom/service/cs101-session.json`，同版本实例重启可复用，再次访问时仍真实验证。
新 `auth-start` 会替换旧挑战并删除旧浏览器 token；401 候选 token 返回 `pending`，
窗口继续等待新 token，其他网络、权限和格式错误终止验证。

有挂载 token 的实例由管理员维护：每次请求重新读取该文件，更新后等待健康检查恢复 ready，
不进入浏览器登录；需要切换认证来源时使用省略 profile 的独立实例。挂载和缓存 token 冲突
会明确报错，避免静默切换账号。包中没有默认 token，不保存学生成绩到磁盘。

## 构建与发布

从仓库根目录构建，构建上下文为本包目录：

```sh
docker build -t "$CS101_IMAGE_TAG" capability_services/cs101_http
docker push "$CS101_IMAGE_TAG"
```

取得仓库返回的真实不可变镜像引用后，按[通用发布步骤](../README.md#发布)
设置 Observer 环境变量，再执行：

```sh
.venv/bin/python scripts/publish_service_package.py capability_services/cs101_http \
  --image "$CS101_IMAGE_REF"
```

`CS101_IMAGE_REF` 必须是 `registry/repository@sha256:...`。发布工具上传 Schema、
契约、描述符与使用文档，并返回真实包 URI；发布本身不会激活实例。
Slave 需要已有的 `egress` network 与服务 binding。修改不可变包需升级版本。

本地调试可通过 `CS101_INSTANCE_PATH` 指向配置文件，`CS101_CREDENTIAL_ROOT`
指向私有凭据目录，`CS101_STATE_ROOT` 指向私有可写状态目录，再运行本包 `server.py`。
健康检查 `/healthz` 仅表示网关可服务；
是否认证成功以系统 `auth-start` 和实例状态为准。

## 验证

```sh
.venv/bin/python scripts/generate_service_contracts.py --check
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q \
  tests/capability_services/test_cs101_gateway.py \
  tests/capability_services/test_cs101_grip.py \
  tests/capability_services/test_service_contract_artifacts.py
```

测试覆盖完整分页、重复尝试、零分、同名学生、并列排名、空数据、跨域跳转阻断、
超时、凭据隔离、浏览器存储桥接、私有状态持久化和同实例重新认证。GRIP 集成测试经过真实发布、激活、HTTP runtime、
网关及 Driver 任务流程，Docker 和上游 API 使用测试替身。
