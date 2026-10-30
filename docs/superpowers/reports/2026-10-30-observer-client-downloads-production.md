# Observer 技能与 Python SDK 下载发布

2026-10-30，按用户要求增加 Observer 的技能包、独立 Python SDK 下载，以及技能内
的 SDK 下载、安装检查和缺失提示。实现提交为 `f6d674f`。

## 下载地址

- 技能包：<http://9.0.16.66:8080/downloads/personal-computing-space.zip>
- SDK：<http://9.0.16.66:8080/downloads/loom_orchestration-0.12.0-py3-none-any.whl>
- 版本与校验清单：<http://9.0.16.66:8080/downloads/manifest.json>

下载不需要 GRIP token。清单提供文件名、字节数、SHA-256 和 SDK 版本；客户端应
读取当前清单后使用其 SDK 文件名，避免将旧版本地址当作永久的最新版本地址。
资源操作仍使用原有认证。

## 发版行为

`scripts/build_agent_skill.py` 从同一源码构建技能 ZIP、独立 wheel 和清单。
ZIP 内嵌 wheel 与独立 wheel 的字节完全一致，构建会替换旧 wheel。
默认 Dockerfile 自动执行这一流程，并将产物打进 Observer 镜像和服务端 Python 包。
技能、SDK 或构建脚本变更后，下一次构建发布会更新下载文件。SDK 代码或依赖变更时
需维护其 `pyproject.toml` 版本号。

下载响应使用正确的附件文件名和 `Cache-Control: no-cache`；只开放指定的发布文件，
拒绝未知文件、路径穿越和符号链接。运行时不临时构建 SDK。源码运行可先构建到
`loom_v2/web/downloads`，或设置 `LOOM_CLIENT_DOWNLOAD_DIR`；未构建时返回 404。

技能会先检查所选 Python 环境中的 SDK 分发包及导入情况。未安装时明确提示用户
安装，给出匹配 Observer 的 wheel URL 和安装命令；下载示例仅依赖标准库，校验
字节数和 SHA-256 后给出安装命令。源码目录存在不等于 SDK 已安装。

## 验证与部署

- 下载 API、发布更新和 SDK 客户端测试：18 passed。
- 发版重建、Observer 注册、GRIP、服务绑定与技能验证回归：49 passed。
- Skill Creator 验证、Pyright 和 `git diff --check` 通过。
- 默认 Dockerfile 构建成功。只读容器验证下载 HTTP、已安装服务端包中的发布文件，
  并实际执行技能文档中的标准库下载示例。该静态下载测试关闭生命周期初始化，
  不连接存储；实际生产启动使用原有数据库和对象存储完成完整初始化。

生产从原 Observer 镜像派生，保留现有依赖和服务 CA，复制同一提交的应用源码及
经过镜像验证的下载产物，安装对应的独立 SDK。提前备份 Compose、环境文件和数据库，
保留旧镜像。没有数据库迁移，只重建 Observer 容器；其他容器 ID、镜像均未改变。

生产镜像：
`sha256:0bb0393f161aea2ec4d6c124ba518ab57ce1cae636f95e541df9e80fd5306427`。
旧镜像保留为 `loom-production-observer:before-client-downloads-20261030`。

生产验收确认：无需 token 即可下载三个文件；文件大小、SHA-256、ZIP 内 wheel
均匹配清单；下载的 SDK 在独立目录安装、导入成功且不引入服务端模块。资源集合、
资源属性和 activation 状态、revision 不变；Driver 接受任务，三个 Slave 在线。
通过原三个 VM activation 下载 `/etc/hostname` 均成功，没有新增 Run。

私有证据、回退配置和脚本位于 gitignored
`production/releases/2026-10-30-client-downloads/`；生产验收结果
`verification.json` 和部署记录 `deployed.json` 均为 `status=passed`。
