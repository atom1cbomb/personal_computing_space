# 火山方舟 HTTP capability package

`ark-http/v3` 为 Loom 的 `service + container:http/1` 提供火山方舟多模态接入。
用户提供 API Key，operator 将其放入所选 credential profile 的 `api_key` 文件；
网关按实例读取该文件。包包含 11 个业务 action、认证 hooks、输入输出 schema 和 IoContract。
外部 Agent 的使用方式见 [HOW_TO_USE](HOW_TO_USE.md)，部署流程遵循
[能力服务包公共说明](../README.md)。

## API 模式与认证

| `instance_config.api_mode` | 接入地址 | 凭据 |
|---|---|---|
| `agent-plan`（默认） | `https://ark.cn-beijing.volces.com/api/plan/v3` | Agent Plan 专属 API Key |
| `standard` | `https://ark.cn-beijing.volces.com/api/v3` | 普通方舟数据面 API Key |

配置只包含 `api_mode` 和可选 `timeout_seconds`（1–300 秒，默认 120 秒）。
模型由每次请求的 `model` 指定，不写死模型版本，不自动切换模型或 API 模式。
Agent Plan 使用套餐中的模型名称；普通 API 使用已开通的 Model ID 或 Endpoint ID。
模型对模态、参数和账户权限的支持以供应商当前文档为准。

例如，Slave operator 在其 runtime credential 根目录下准备：

```text
<runtime-credential-root>/ark-personal/api_key
```

`api_key` 是只含用户 Key 的文本文件，可以带末尾换行；确保容器 UID 65534 可读。
激活时传 `credential_profile="ark-personal"`，runtime 将该目录只读挂载到
`/run/loom/credentials`。Key 不进入镜像、manifest、instance_config、Run 参数或
Observer 内容；业务输入 schema 也不接收 Key、请求头或自定义 Base URL。
不同账户使用不同 profile 和命名实例。更换 profile 需要新实例名；同一 profile 内更新
Key 后，后续请求和健康探测会重新读取文件。

认证使用系统的 `auth-start` 和 `credential-profile`，私有 hooks 为
`POST /v1/auth/start`、`POST /v1/auth/status`。
探测会使用文档公开的 `GET /contents/generations/tasks?page_num=1&page_size=1`
验证真实访问，不发起推理或生成，也不返回任务内容。缺少 Key 或供应商返回 401 时报告
`auth_required`；403、限流、网络故障或无效响应报告 `auth_failed`。
因此 Key 需要有读取视频任务列表的权限；这一探测不验证全部模型的开通状态或剩余额度。
`GET /healthz` 只反映网关进程可服务，不代表供应商认证成功。

## 构建与发布

从仓库根目录构建镜像：

```sh
docker build -f capability_services/ark_http/Dockerfile -t YOUR_REGISTRY/loom/ark-gateway:v2 .
docker push YOUR_REGISTRY/loom/ark-gateway:v2
```

使用镜像仓库返回的真实不可变镜像引用发布包（`ARK_IMAGE_REF` 由部署者设置）：

```sh
.venv/bin/python scripts/publish_service_package.py capability_services/ark_http \
  --image "$ARK_IMAGE_REF"
```

沿用仓库发布器的 `LOOM_OBSERVER_URL`、`LOOM_GRIP_AUTHORITY`、
`LOOM_WORKSPACE_ID` 和 `LOOM_GRIP_TOKEN` 配置。发布器上传 schema、descriptor、
IoContract 和 HOW_TO_USE，取得 Observer 的正式不可变引用。
本地 HTTP Observer 显式传 `--allow-http`。

激活前先通过 Slave 的 `check-package` 检查配置与 binding，再调用 `activate-package`：

```python
config = {"api_mode": "agent-plan", "timeout_seconds": 120}
checked = await client.invoke(slave_uri, client.names.action("check-package"), {
    "package": package_uri, "instance_name": "ark-personal", "instance_config": config,
})
if not checked["ready"]:
    raise RuntimeError(checked["issues"])
activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "ark-personal",
    "instance_config": config,
    "credential_profile": "ark-personal",
    "idempotency_key": "activate-ark-personal-v2",
})
description = await client.describe(activation["resource"])
```

Slave 需要已配置服务入口和 `egress` 网络；节点与外部 Agent 都使用既有 GRIP binding。
认证允许访问 `ark.cn-beijing.volces.com:443`；语音合成还需允许
`openspeech.bytedance.com:443`。容器以非 root 用户运行，不需要持久状态目录。
进程支持 `LOOM_INSTANCE_PATH`、`ARK_CREDENTIAL_ROOT` 和 `PORT` 的本地测试覆盖。

## 行为与限制

普通 JSON 动作直接返回业务结果，也可放入显式任务计划以记录和恢复执行。
供应商视频任务创建返回 task ID，随后需调用 `videos_get` 观察生成状态；
即使通过 Loom Run 执行创建动作，Run 完成也只表示已取得供应商 task ID。
完整的业务示例与状态解释见 [HOW_TO_USE](HOW_TO_USE.md)。

网关接收原生多模态 JSON；媒体使用供应商可访问的 URL、Base64 data URL 或已存在的
File ID。不提供文件上传接口，不在网关下载或代理用户指定的媒体 URL。
Chat、Responses 和生图均采用非流式响应，`stream=true` 会在请求供应商前拒绝。
音频理解可由支持该模态的 Chat／Responses 模型处理；专用语音接口仅提供 Agent Plan
HTTP TTS，使用独立 OpenSpeech 地址和 `X-Api-Key` 鉴权，不提供实时 WebSocket ASR/TTS。
TTS 将 Base64 音频片段分别解码再合并，收到成功结束码后才返回完整的 `audio_base64`。

请求体上限 32 MiB，供应商响应上限 64 MiB；较大的媒体优先使用 URL。
HTTP timeout 是供应商连接/读取的等待限制，不是视频生成的总时长。
认证探测最多等 10 秒。网关不跟随重定向、不自动重试、不在两种计费地址之间回退。
超时后的生成请求可能已被供应商接受，不能据此直接重试；视频可先用 `videos_list` 核实。
认证/传输错误只返回稳定错误码，保留业务结果中的模型输出、usage、任务状态和产物地址。

Agent Plan 官方文档将文本和向量模型限定在 AI 工具使用场景；调用方需按套餐权益使用。
Small／Medium 的视频权限、模型列表及超额后付费设置由供应商控制，网关不推断套餐档位。
官方开通的超额后付费仍可能在 `/plan` 地址产生费用；不切换地址并不等于强制禁止后付费。

## 验证

```sh
.venv/bin/python scripts/generate_service_contracts.py --check
.venv/bin/pytest -q tests/capability_services/test_ark_gateway.py \
  tests/capability_services/test_ark_grip.py \
  tests/capability_services/test_service_contract_artifacts.py
```

测试覆盖真实本地 HTTP gateway、GRIP 发布/激活/认证、runtime 转发和每个业务
导出。Docker 和供应商由测试替身提供；真实 Key、真实套餐权限及供应商计费需部署后验收。

## 接入依据

核对日期：2026-10-31。

- [Agent Plan 套餐概览](https://console.volcengine.com/ark/region:cn-beijing/docs/agent-plan-personal-plan-overview)
- [Agent Plan 接入视觉模型](https://ark.volcengine.com/region:cn-beijing/docs/agent-plan-personal-visual-models)
- [Agent Plan 接入语音模型](https://ark.volcengine.com/region:cn-beijing/docs/agent-plan-personal-voice-model)
- [Agent Plan 接入向量化模型](https://ark.volcengine.com/region:cn-beijing/docs/agent-plan-personal-vectorized-models)
- [Base URL 及鉴权](https://ark.volcengine.com/region:cn-beijing/docs/base-url-and-authentication)
- [Chat API](https://ark.volcengine.com/region:cn-beijing/docs/chat-api) / [Responses API](https://ark.volcengine.com/region:cn-beijing/docs/create-model-responses-api)
- [多模态理解](https://ark.volcengine.com/region:cn-beijing/docs/multimodal-understanding) / [音频理解](https://ark.volcengine.com/region:cn-beijing/docs/audio-understanding)
- [图片生成](https://ark.volcengine.com/region:cn-beijing/docs/image-generation-api) / [多模态向量化](https://ark.volcengine.com/region:cn-beijing/docs/multimodal-vectorization-api)
- 视频任务：[创建](https://ark.volcengine.com/region:cn-beijing/docs/create-video-generation-task-api)、[查询](https://ark.volcengine.com/region:cn-beijing/docs/get-video-generation-task-api)、[列表](https://ark.volcengine.com/region:cn-beijing/docs/list-video-generation-tasks-api)、[取消或删除](https://ark.volcengine.com/region:cn-beijing/docs/cancel-or-delete-video-generation-tasks-api)
