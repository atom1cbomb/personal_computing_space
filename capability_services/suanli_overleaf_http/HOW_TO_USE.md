# Suanli Overleaf 项目同步

一个激活实例连接一个 Overleaf 账号，可管理该账号可访问的多个项目。通过 Loom
统一登录入口完成一次网页登录，并由激活者选择含 Git token 的 credential profile。
外部 Agent 在自己的机器编辑、检查和编译源码，再把明确修改同步到 Overleaf。

## 动作与本地助手

| 动作 | 交互 | 用途 |
|---|---|---|
| `projects.list` | request-reply | 实时项目列表，可传 `query`、`offset`、`limit`；权限只取上游实际字段 |
| `projects.download` | byte-stream | 项目 ID → 源码快照、基准 Git 版本及文件清单 |
| `projects.status` | request-reply | 项目 ID + `base_revision` → 最新版本、是否变化及可获得的路径差异 |
| `projects.upload` | byte-stream | 更新已有项目：修改 ZIP、明确删除、基准版本、说明及幂等键 |
| `projects.import` | byte-stream | 完整源码 ZIP + 名称 + 幂等键 → 新项目 ID 和 URL |

先 DESCRIBE 激活并读取其准确 action、Schema 和 binding。查询返回
`{status: "ok", result: ..., error_code: null}` 或
`{status: "error", result: null, error_code: ...}`。包内助手自动发现动作并检查结果。
项目名称不是唯一身份；后续操作始终使用返回的项目 ID。

源码仓库的 `capability_services.suanli_overleaf_http.client` 提供 Python 助手和 CLI，
依赖 `loom-orchestration` SDK。它只在外部 Agent 机器处理本地路径。

```python
from loom_orchestration import LoomClient
from capability_services.suanli_overleaf_http.client import OverleafClient

async with LoomClient(observer_url, token=loom_token) as sdk:
    overleaf = await OverleafClient.discover(sdk, activation_uri)
    projects = await overleaf.list(query="论文")
    await overleaf.download(project_id, "./paper", "./paper.sync.json")
    # 在 ./paper 编辑。状态记录必须放在源码目录之外。
    latest = await overleaf.status("./paper.sync.json")
    result = await overleaf.upload("./paper", "./paper.sync.json",
        message="修改实验部分", idempotency_key="experiment-edit-001")
    # 创建新项目是独立操作，不会覆盖已有项目：
    created = await overleaf.import_project("./new-paper", name="新论文",
        idempotency_key="new-paper-001")
    # 已有 ZIP 可使用 import_zip("./sources.zip", name=..., idempotency_key=...)。
```

CLI 用 `LOOM_OBSERVER_URL`、`LOOM_GRIP_TOKEN` 和已发现的激活 URI：

```sh
python -m capability_services.suanli_overleaf_http.client --activation "$ACTIVATION" list
python -m capability_services.suanli_overleaf_http.client --activation "$ACTIVATION" \
  download "$PROJECT_ID" ./paper --state ./paper.sync.json
python -m capability_services.suanli_overleaf_http.client --activation "$ACTIVATION" \
  status --state ./paper.sync.json
python -m capability_services.suanli_overleaf_http.client --activation "$ACTIVATION" \
  upload ./paper --state ./paper.sync.json --message '修改实验' --key experiment-edit-001
python -m capability_services.suanli_overleaf_http.client --activation "$ACTIVATION" \
  import ./new-paper.zip --name '新论文' --key new-paper-001
```

下载要求源码目录和状态文件尚不存在，避免覆盖本地工作。重新下载用于合并时选择
另一个目录。上传助手对照原始 SHA-256 清单生成新增、修改和删除；服务端只执行
归档内的修改和 `deleted_paths`，未打包的文件保持原样。

助手忽略未跟踪的 `.git`、常见 LaTeX 中间文件（aux、log、toc 等），已跟踪文件
仍保留；PDF、图片、`.bib` 和 `.bbl` 默认保留。建议在源码目录外编译，或以
`exclude=["main.pdf", "build/*"]` / CLI `--exclude` 排除已知生成文件。
不要自动删除自己不认识的项目文件。源码中的符号链接、子模块不支持。

## 冲突与重试

`status` 是查询；`upload` 会重新 fetch，并只在远端版本等于基准时提交和普通推送。
返回 `conflict` 时包含远端 `revision`，本地状态不会前移。下载新快照到另一个目录，
在外部 Agent 机器合并，基于该快照重新提交。禁止自动强推。Git 回写可能影响
Overleaf 的批注和修订，协作项目应先协调编辑范围。

每次新修改使用新幂等键；网络重试必须保持同一键、相同参数、相同 ZIP 字节。
助手的 ZIP 打包是确定性的。更新在推送前持久化提交 ID；响应丢失时先检查远端
是否包含该提交，不创建重复提交。`upload_outcome_unknown` 可用相同输入重试；
`git_authentication_failed` / `git_credential_missing` 应修复 Git 凭据，重新网页登录
不会修复它。查询的 `base_available=false` 表示不能给出该基准的路径差异。

导入成功后立即保存项目 ID，后续 Git 访问失败不改变创建结果。相同键返回同一结果；
同键不同内容返回 `idempotency_conflict`；并发处理中返回 `pending`。
`import_outcome_unknown` 表示请求可能已经创建项目，服务端不会再次发送。请核查
Overleaf 项目列表和网页，必要时人工处理；不能自动换新键重试，也不能只按同名项目
猜测创建成功。不同实例或包版本不共享此账本。

## 字节流契约 overleaf-archive.v1

所有流的 stream-open 输出为 `{protocol: "overleaf-archive.v1", direction: "download" | "upload"}`，
仅表示接纳请求，**不表示操作成功**。数据均为二进制帧，复用 Loom 原有 byte-stream。

服务端结果采用 `4 字节网络字节序无符号 JSON 长度 + UTF-8 JSON`，JSON 最多 64 KiB。

- 下载：服务端先发送结果，再发送 ZIP，最后正常 EOF。成功结果为
  `{status: "downloaded", project_id, revision, archive_size, archive_sha256}`。
  ZIP 根目录含 `manifest.json` 和 `project/<源码路径>`；manifest 包含同一快照的
  `project_id`、`revision`、`branch` 和 `files: {路径: {sha256, size}}`。
- 上传/导入：输入 Schema 包含 `archive_size`、`archive_sha256` 和业务参数。
  客户端在 stream-open 后精确发送声明长度的**源码 ZIP**，不要发送下载信封或
  依赖半关闭。服务端完整接收并核验后才产生副作用，随后发送上述长度前缀 JSON
  结果并 EOF。`uploaded` / `unchanged` 返回 `project_id, revision`；`imported`
  返回 `project_id, url`；冲突、pending 和 error 必须单独处理。
- 失败结果为 `{status: "error", error_code: ...}`。下载失败不附带 ZIP。
  长度不符、摘要不符、截断、无完整终态、非正常断连均不能当作成功。

单个源码 ZIP 最多 50 MiB、解压后最多 64 MiB、最多 2000 项，下载信封另允许一项
清单；拒绝越界路径、重复路径、符号链接和 `.git`。同时最多两条传输，默认单条
最长五分钟。完整接收后的写入会继续记录结果，即使调用方已经断开；不自动重放流。
