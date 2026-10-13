# YOLO11n HTTP capability package

这个目录同时包含 YOLO11n HTTP 服务源码和 Loom 能力包声明。

YOLO 镜像本身已经是常驻 HTTP 服务：

- 健康检查：`GET /healthz`
- 能力调用：`POST /invoke`
- 输入：`request_id`、`image_id`、`image_base64`
- 输出：检测框、类别计数、推理耗时

接入 Loom 时，不需要把 YOLO 复制进 Slave 的 Python 进程。应将这个 manifest 注册为能力包，然后分别 provision 到 Slave A 和 Slave B。每个 Slave 会由 `container:http/1` 启动一个本地 YOLO 容器并通过 `/invoke` 调用。

`package.example.json` 中的 schema 和契约引用已经对应本目录文件的 SHA-256。当前本机在 `localhost:5001` 运行实验 registry，并固定为镜像 digest；Loom 会拒绝普通 tag。Slave 与宿主环境共享 Docker daemon，因此镜像先推入该 registry 后，runtime plugin 可按精确 RepoDigest 复用本地镜像。当前可用版本为 `v4`，早期 `v1`–`v3` 是 registry 地址排障时保留的失败记录。

注册顺序：先 POST Schema 和契约文件到 `/api/v1/content`，再 POST `package.example.json` 到 `/api/v1/capability-packages`。注册只发布能力包；随后在 Loom 的激活流程中分别对 Slave A、Slave B provision。
