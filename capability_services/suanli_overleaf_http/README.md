# Suanli Overleaf

`suanli-overleaf-http/v1` 为 https://overleaf.cs.ac.cn 提供账号级项目管理。
五个动作是 `projects.list`、`projects.download`、`projects.status`、
`projects.upload` 和独立的 `projects.import`。使用方式见 [HOW_TO_USE.md](HOW_TO_USE.md)。

## 构建与发布

从仓库根目录构建（复用仓库的独立 SDK 和流服务实现）：

```sh
docker build -f capability_services/suanli_overleaf_http/Dockerfile \
  -t YOUR_REGISTRY/loom/suanli-overleaf:v1 .
docker push YOUR_REGISTRY/loom/suanli-overleaf:v1
python scripts/publish_service_package.py capability_services/suanli_overleaf_http \
  --image YOUR_REGISTRY/loom/suanli-overleaf@sha256:ACTUAL_DIGEST \
  --observer "$LOOM_OBSERVER_URL"
```

发布脚本使用 `LOOM_GRIP_TOKEN`。`ServicePackage.publish` 同时发布使用文档、
五个描述符及其 Schema/IoContract。需要源码诊断包时，可通过 SDK 的 `source_files`
显式包含 `server.py`、`web.py`、`git_store.py`、`common.py`、`client.py` 和 `askpass.sh`。

管理员在目标 Slave 上准备 credential profile，内含 `overleaf_git_token` 文件，
内容是激活者的 Overleaf Git authentication token。容器以 UID/GID 65534 运行，
凭据文件需允许该用户读取，凭据目录仅由管理员管理。激活通过既有的
`check-package` → `activate-package`，指定该 profile、独立的实例名和
`instance_config: {}`；不要把 token 或 Cookie 复制进配置、请求或发布内容。

容器读取 `/run/loom/credentials/overleaf_git_token`，通过 Git askpass 使用用户名
`git`。Git URL 固定为 `https://overleaf.cs.ac.cn/git/<project_id>`，禁止重定向和
交互式终端提示，令牌不写进 URL、参数或仓库配置。网页登录只使用 Cookie，
通过统一 `loom-auth` 门户完成，Cookie 名为 `overleaf.sid`。

持久卷 `/var/lib/loom/service` 保存网页会话、Git 缓存和 SQLite 操作账本，权限按实例
隔离。相同包版本的重启复用它；不同包 digest 的状态目录不同。升级迁移前要保留
旧账本并核查未决写入，不要在新实例中自动重放结果未知的导入。

## 上游适配和验收边界

- 列表用 `GET /user/projects`，返回当前账号非归档、非回收站的自有及共享项目。
  该接口一次返回全部项目，本包对实时结果按项目 ID 排序、过滤、分页；分页期间
  项目增删可能移动 offset，不把它描述为上游游标快照。
- 导入前读取 `/project` 的 `ol-csrfToken`，以 `X-CSRF-Token` 和 multipart 的
  `name`、`qqfile` 向 `/project/new/upload` 提交 ZIP。
- Git 从远端 HEAD 检测 `main` / `master`，fetch 后生成快照。上传使用对象和索引
  创建提交，避免源码中的 `.gitignore`、`.gitattributes` 改写或漏传文件；普通 push
  拒绝并发产生的非快进。Git token 的项目权限在实际 Git 操作时验证。
- `authenticated` 表示网页项目列表验证成功，包括合法的空账号；不代表 Git token
  已验证。Git 凭据错误通过业务结果单独报告。激活者须为网页和 Git 选择同一账号；
  上游没有提供本包可用的两种凭据账号匹配接口。

接口依据：[Overleaf Git 文档](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration)、
[项目列表实现](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Project/ProjectController.mjs)、
[ZIP 上传实现](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Uploads/ProjectUploadController.mjs)。
2026-10-30 已在该定制站点完成真实账号的只增联调：统一登录、账号列表、ZIP 导入、
Git 下载、新增文件上传、二进制往返、幂等重试与旧基准新增冲突，实际默认分支为
`main`。没有执行删除或覆盖已有文件的真实测试，也未验证网页编辑与 Git 推送的瞬时
并发。生产记录见 [部署与联调报告](../../docs/superpowers/reports/2026-10-30-overleaf-production-validation.md)。
本地客户端使用 SDK 0.9.2，支持带自定义 CA 的 WSS 与显式允许的 WS binding 共存。

```sh
python scripts/generate_service_contracts.py --check
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q \
  tests/capability_services/test_overleaf_gateway.py \
  tests/capability_services/test_overleaf_grip.py \
  tests/capability_services/test_service_contract_artifacts.py
LOOM_OVERLEAF_TEST_IMAGE=YOUR_BUILT_IMAGE .venv/bin/python -m pytest -q \
  tests/capability_services/test_overleaf_docker.py
```
