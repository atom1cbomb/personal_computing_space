# 火山方舟 Docker 与生产验收

2026-10-31，使用用户提供的 `production/secrets/ark_api_key` 完成真实 Docker 和生产联调。
生产 Observer 为 `http://9.0.16.66:8080`，当前实例保留运行。

## 部署结果

- 包：`grip://loom.local/workspaces/workspace-default/packages/ark-http/v1`
- 包摘要：`aa4dde82a8a8fe650ed01e93b7162e28f331d14461535adfa971f12584f0703d`
- 激活：`grip://loom.local/workspaces/workspace-default/activations/ark-http/v1/slave-a/ark-personal`
- 状态：`ready` / `authenticated`，revision 1，11 个业务 action 全部可用。
- 镜像：`localhost:5000/loom/ark-gateway@sha256:3c25ffeb78fbbdf6d0a1777cb968cf6e37eb94610c42d7d2ff996d7b7c277566`
- 配置：`api_mode=agent-plan`、`timeout_seconds=120`；选择 `ark-personal` credential profile。

镜像在本地构建并通过实际 Docker 测试，随后用 Skopeo 经 SSH 隧道推送到生产现有 registry，
以 registry 返回的真实不可变摘要发布。生产由 Slave 拉取镜像并激活，发布前 `check-package`
通过。Observer、Driver、Slave 平台镜像、数据入口与 TLS 配置没有改动。

API Key 仅写入生产 operator 凭据目录
`/var/lib/loom/runtime-credentials/ark-personal/api_key`，目录权限 0700、文件权限 0400，
owner UID 65534；runtime 将 profile 只读挂载。Key 不进入镜像、普通实例配置、包、Run 输入或报告。
最终扫描本地发布与验收资料，没有发现该 Key 的明文字节。

生产容器以 UID 65534 运行，根目录只读，credential mount 只读。
镜像内的 **51 个源码、说明与契约文件**逐文件 SHA-256 比对通过；发布的诊断源码也已通过
公开 API 回读并与本地文件一致。独立安装的 SDK 0.12.0 与生产 Observer 下载 manifest 匹配，
业务调用均使用公开 GRIP offers 和 schema。

## 验证结果

| 检查 | 实际结果 | Run |
|---|---|---|
| Docker 运行策略与重启 | 无 Key 时 pending；非 root、只读根目录、重启后恢复 | 本地真实 Docker |
| Docker 真实 Key | 认证及视频任务列表读取成功，重启后认证仍成功 | 本地真实 Docker |
| Docker 视觉调用 | `doubao-seed-2.0-mini` 将测试图片识别为“红色” | 本地真实 Docker |
| `chat_completions` | 原生图片输入，回答“红色” | `run-7b81902852b6` |
| `responses_create` | 原生 input_image，回答“红色”，返回可读取 Response ID | `run-45c57162baaa` |
| `responses_get` / `responses_delete` | 读取内容与原响应一致，并删除本次测试 Response | 直接请求回复 |
| `images_generate` | Seedream 5.0 Flash 生成红色正方形，实际下载 JPEG 103154 字节 | `run-4f9a5aec7e64` |
| `embeddings_multimodal` | 图文组合输入，实际返回 1024 维 dense embedding | `run-d740ee49ec6e` |
| `audio_speech` | Agent Plan HTTP TTS 返回完整 MP3，15021 字节，MP3 文件头有效 | `run-9c8950475f5b` |
| `videos_create` | Seedance 2.0 Mini，4 秒、480p、16:9、无音频，返回供应商 task ID | `run-5fe520127abb` |
| `videos_get` | 从 running 轮询到 succeeded，下载 MP4 782990 字节，文件头含 ftyp | 直接请求回复 |
| `videos_list` / `videos_delete` | 按本次 task ID 筛选命中成功记录；产物保存后删除该测试任务记录 | 直接请求回复 |

供应商视频 task ID 为 `cgt-20261031164431-bfgxl`。只删除本次创建的测试 Response 和视频任务记录，
生成图片、语音、视频及验收证据保存在本地发布目录。六个验收 Run 的 JSON 内容摘要均验证通过，
全部已关闭；生图修复保持同一个 Run URI。

本轮相关单元、契约和 GRIP 测试 **90 项通过**；另有 **2 项真实 Docker 测试通过**。
契约生成 `--check`、`git diff --check` 通过。前一轮完整能力包/GRIP 回归为 450 项通过、14 项跳过。

可复现 Docker 的只读认证验证：

```sh
LOOM_ARK_TEST_IMAGE=loom/ark-gateway:acceptance-20261031 \
LOOM_ARK_TEST_KEY_FILE="$PWD/production/secrets/ark_api_key" \
.venv/bin/pytest -q tests/capability_services/test_ark_docker.py
```

## 联调中的情况与实际边界

首次生图请求显式发送 `sequential_image_generation=disabled`，模型返回 HTTP 400：该模型不支持
这个可选参数。已移除参数，通过公开 Run `resubmit` 在原 Run 上修复并完成真实生成；没有切换
模型、API 模式或计费入口。该过程不需要修改网关代码，说明调用方必须按所选模型支持的参数调用。

首个视觉 Run 的初始 300 秒客户端等待超时，随后复用原调用回执取得成功结果，没有再次提交生成。
联调期间观察到 Driver 多次租约冲突（HTTP 409）及较长的排队/执行等待；验收客户端后续使用
10 秒轮询。任务最终全部完成，本次没有认定或修复该平台调度延迟的根因。

后续专项排查已定位并修复 Observer 无关历史加载与 Driver 恢复失败后停止领任务两处问题，
单独更新了 Observer/Driver；生产前后对比、测试与回退记录见
[调度延迟与 Driver 租约冲突根因及生产验证](2026-10-31-driver-lease-root-cause.md)。

真实供应商验收使用 Agent Plan Key。普通 API 模式由本地转发与隔离测试覆盖，未使用另一把
普通 Key 验收。此次真实理解输入为图片；PDF、视频、音频理解的原生内容转发由契约/网关测试
覆盖，不等同于这些场景均完成了真实模型验收。专用语音接口本次验证 HTTP TTS。

## 证据与回退

私有资料位于 `production/releases/2026-10-31-ark/`，包括 registry 摘要、发布包、公开契约、
调用账本、原始拒绝和修复回执、产物及 `final-acceptance.json`。该目录由 Git 忽略。

最终复核保留了部署前全部 2273 个资源 URI，原有 ready activation 状态保持不变。
生产实例仍 ready/authenticated。需要停用时，通过该 activation 的公开 `stop` offer，传当前
`expected_revision` 和新幂等 key；保留包、镜像、profile 和验收账本供复查，无需回滚平台或数据库。
