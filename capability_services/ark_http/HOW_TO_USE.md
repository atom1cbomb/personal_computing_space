# 火山方舟多模态模型

这个包用用户提供的 API Key 调用火山方舟。先选择已配置的 credential profile 激活命名实例，
认证成功且 activation ready 后，读取 activation 的 DESCRIBE，按名称选 action，并使用返回的
精确 URI、交互方式和 schema。Key 仅由 profile 挂载，不放进业务参数。
包不声明资源依赖，也不创建 Loom MCUE 资源。

默认 `api_mode=agent-plan` 使用 Agent Plan 专属 Key；`api_mode=standard` 使用普通方舟 Key。
两种模式分别对应 `/api/plan/v3` 和 `/api/v3`，不会自动切换。
同一账户的视频任务和 Response 必须在创建时的 API 模式和凭据下继续访问。
模型由调用者传入 `model`；Agent Plan 使用当前套餐支持的模型名称，standard 使用已开通的
Model ID 或 Endpoint ID。模型的模态、参数、额度和套餐权限由火山方舟验证。

| action 名称 | 交互 | 用途与结果 |
|---|---|---|
| `chat_completions` | request-reply | 原生 `messages` 多模态对话，返回 `result.choices`、usage、工具调用 |
| `responses_create` | request-reply | 原生 `input` 多模态理解，返回 `result.id`、`result.output`、usage |
| `responses_get` / `responses_delete` | request-reply | 按 `response_id` 读取或删除已存储 Response |
| `images_generate` | request-reply | 文生图、参考图编辑、组图和图层拆分，返回 `result.data` 中的 URL／Base64 |
| `embeddings_multimodal` | request-reply | 图文／视频向量化，返回 `result.data` 和 usage |
| `videos_create` | request-reply | 提交生视频任务，返回供应商 `result.id`，随后查询生成状态 |
| `videos_get` / `videos_list` | request-reply | 查询任务状态、产物 URL 或分页列表 |
| `videos_delete` | request-reply | 取消排队中的视频任务，或删除任务记录 |
| `audio_speech` | request-reply | Agent Plan HTTP 语音合成，返回 `result.audio_base64`、格式及 usage |

业务输入和输出遵循各 action 的 Schema，业务结果为 `{action, result}`。
以下 JSON 均为业务参数。

## 参数和结果的单位

| 字段 | 单位和含义 |
|---|---|
| `max_tokens`、`max_completion_tokens`、`max_output_tokens` | token 个数，不是字符数或字节数；是否含推理 token 及可用上限由模型决定 |
| `temperature`、`frequency_penalty`、`presence_penalty`、`logit_bias` | 无量纲采样/惩罚系数；temperature 不表示摄氏度，允许范围按模型文档 |
| `top_p` | 0–1 的累计概率，`0.9` 表示 90% 的概率质量，不填 `90` |
| `top_logprobs`、`max_tool_calls` | token 候选数量 / 工具调用次数；`seed` 是无量纲随机种子 |
| `responses_create.expire_at` | 存储过期的绝对 UTC Unix **秒**时间戳，不是毫秒或相对时长 |
| `images_generate.size` | 像素宽高，例如 `"2048x2048"`，或模型支持的 `"2K"` 等分辨率档位；K 不表示 KB |
| `images_generate` 结果的 `created` | 创建时刻的 UTC Unix 秒时间戳 |
| `embeddings_multimodal.dimensions` | 向量分量个数；分量本身是无量纲数值，不是百分比 |
| `videos_create.duration` / `frames` | 秒 / 总帧数；例如 `duration=5` 表示 5 秒；`frames` 不是 fps；`duration=-1` 仅用于支持自动时长的模型 |
| 视频 `resolution` / `ratio` | `"720p"` 等像素分辨率档位 / `"16:9"` 等无量纲宽高比 |
| `videos_create.execution_expires_after` | 从任务 `created_at` 开始计算的过期时长，单位秒，包含排队和运行时间；不同于生成视频时长 |
| 视频任务原生结果 | `created_at`、`updated_at` 为 Unix 秒；`duration` 为秒，`frames` 为帧数，`framespersecond` 为帧/秒 |
| `videos_list.page_num` / `page_size` / `total` | 从 `1` 开始的页码 / 每页任务条数 / 匹配任务总条数 |
| `audio_speech.req_params.audio_params.sample_rate` | Hz，即每秒采样数；`24000` 为 24 kHz；额外传入 `bit_rate` 时其单位是 bps（比特/秒） |

本包保留供应商字段含义，不自动换算单位或为全部模型指定统一默认值/范围；模型支持的
参数、特殊值及限制以当前模型文档为准。输入输出 schema 的字段说明随包一起发布。
原生 `usage` 中的 token 字段按 token 计数，其余供应商字段按各自定义解释。
单位依据：[Chat](https://docs.volcengine.com/docs/ark/chat-api?lang=en)、
[Responses](https://docs.volcengine.com/docs/ark/create-model-responses-api?lang=en)、
[图片生成](https://console.volcengine.com/ark/region%3Acn-beijing/docs/image-generation-api)、
[视频生成](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)及
[语音参数](https://docs.volcengine.com/docs/doubao-voice/task-sub-mission?lang=zh)。

## 多模态理解

`responses_create` 示例（替换 `model` 和媒体地址）：

```json
{
  "model": "YOUR_VISION_MODEL",
  "input": [{
    "role": "user",
    "content": [
      {"type": "input_text", "text": "描述这张图，指出主要物体的位置。"},
      {"type": "input_image", "image_url": "https://YOUR_MEDIA_HOST/photo.png"}
    ]
  }],
  "max_output_tokens": 1024
}
```

支持图片、视频、PDF 和音频的模型可使用该模型文档规定的原生内容对象。
例如 PDF 为 `input_file`，图片可提供 `image_url` 或现有 `file_id`。
多轮调用将 `result.id` 作为下一轮的 `previous_response_id`。
`chat_completions` 则使用 `messages[].content` 中的 `text`、`image_url`、`video_url`、
`input_audio` 等对象。保留原生模型参数，包括 thinking、reasoning、tools 等；遵守所选 action
schema。所有生成动作均非流式，`stream=true` 不受支持。

## 生图与向量

`images_generate`：

```json
{
  "model": "doubao-seedream-5.0-pro",
  "prompt": "一张水墨风格的雪山海报",
  "size": "2K",
  "response_format": "url",
  "watermark": true
}
```

`image` 可选，传参考图 URL、Base64 data URL 或图片列表，用于图生图与编辑。
普通生图需要非空 `prompt`；图层拆分可用 `layer_decomposition=true` 与 `image`。
组图的单图错误保留在 `result.data[].error`，应逐项检查；没有任何成功图片时网关报错。
返回地址有有效期，取得产物后及时下载或转存；包不代为下载。

`embeddings_multimodal`：

```json
{
  "model": "doubao-embedding-vision",
  "input": [
    {"type": "text", "text": "雪山日出"},
    {"type": "image_url", "image_url": {"url": "https://YOUR_MEDIA_HOST/mountain.png"}}
  ],
  "dimensions": 1024
}
```

这里的 `input` 元素共同构成一次多模态输入，不是 OpenAI 文本批量 embedding 格式。
`result.data` 是对象，可能包含 `embedding`、`multi_embedding`、`sparse_embedding`。
跨次检索固定使用相同的模型版本和维度。

## 生视频任务生命周期

1. 调用 `videos_create`，业务输入示例：

   ```json
   {
     "model": "doubao-seedance-2.0",
     "content": [{"type": "text", "text": "无人机航拍雪山日出，镜头缓慢平移"}],
     "duration": 5,
     "ratio": "16:9",
     "generate_audio": true
   }
   ```

2. 从业务响应的 `result.id` 取供应商 task ID；任务执行时该响应位于 Run 的 `outcome.value`。
   这一步只表示任务被供应商接受。
3. 直接调用 `videos_get`，输入 `{"task_id": "上一步的ID"}`。
   `queued`、`running` 时按有界间隔继续查询；`succeeded` 才表示视频生成成功，
   读取 `result.content.video_url`。`failed`、`cancelled`、`expired` 为终态，检查
   `result.error`，不要将 HTTP 200 或 Loom Run 完成解释为生成成功。
4. 及时保存产物。供应商任务记录和产物 URL 都有有效期。

列表使用 `videos_list`：`{"page_num":1,"page_size":20,"filter":{"status":"succeeded"}}`。
`filter.task_ids` 支持多个 ID；分页由调用者处理。
调用 `videos_delete` 传 `task_id`。只有排队中的任务可取消，删除记录不代表运行任务已停止。
网关不自动重试生成或重复创建任务。网络超时可能发生在供应商已接受之后，应先核实任务列表。

## 语音合成

仅限 `agent-plan`，调用 `audio_speech`：

```json
{
  "req_params": {
    "text": "你好，欢迎使用火山方舟。",
    "speaker": "zh_female_vv_uranus_bigtts",
    "audio_params": {"format": "mp3", "sample_rate": 24000}
  }
}
```

`speaker` 及音频参数以供应商语音模型文档为准。将 `result.audio_base64` 解码后保存成对应
`result.audio_format` 的音频；`result.usage` 保留供应商返回的用量。
该动作聚合 HTTP 音频片段后返回完整音频，不提供实时 WebSocket 流或专用 ASR。
音频理解使用支持该模态的 Chat／Responses 模型。

认证过期或 Key 更新后，在同一实例调用系统 `auth-start` 重新验证，确认 ready 后继续。
403 可能是模型／接口权限不足；429 为限流／额度问题；网络错误不会被当作需要重新登录。
Agent Plan 的文本及向量模型按官方要求用于 AI 工具；套餐开通的超额后付费仍可能产生账单。
详细部署、边界和供应商文档链接见 [README](README.md)。
