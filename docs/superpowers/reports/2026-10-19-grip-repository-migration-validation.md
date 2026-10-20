# GRIP 全仓资源使用迁移验证

日期：2026-10-19。设计依据：[GRIP spec](../specs/2026-10-19-grip-resource-invocation-design.md)；
使用入口：[资源指南](../../grip-resource-usage.md)、[服务包指南](../../../capability_services/README.md)、
[可运行示例](../../../examples/grip/README.md)。本轮在工作区已有资源元数据和编排改动上继续实现，未覆盖或回滚这些改动。

## 完成内容

- 公开资源发现、登记、激活、停用和认证启动统一使用 GRIP。删除原公开 REST 资源路径；
  `promote` 只发布包，并拒绝旧激活参数。内容制作、候选包发布、完整计划提交与管理查询保留各自职责。
- 五个服务目录均包含完整 descriptor、Schema、IoContract 和实际上传流程。Kubernetes 的认证路径
  改为 activation 生命周期配置；两种 SSH 包带实例配置 schema 和检查退出码/超时的 validator。
- `ServicePackage.publish` 和 CLI 上传真实产物、解析同包 descriptor 依赖和跨包 GRIP 引用，
  返回不可变包资源 URI；拒绝可变镜像标签和未解析的访问包依赖。
- 命名 activation 使用显式 instance_name/revision。测试验证两个实例的独立配置文件、容器身份
  与停用行为。浏览器认证启动也携带调用者的 expected_revision，避免并发停用后跟随新 revision。
- README、服务包文档、认证助手说明、skill、SDK、smoke 脚本及 band-gap 验收脚本已更新。
  历史设计保留原记录并标明被 GRIP 取代。全仓扫描中旧公开资源路径仅剩历史文档和验证路径已删除的测试。
- 远程部署 TOML 支持 GRIP authority、公开 origin 和独立 token 文件；token 仅挂载到 Observer，
  部署预览隐藏值。SDK wheel 安装和独立导入检查包含 ServicePackage。

## 自动化结果

在仓库根目录使用 `PATH="$PWD/.venv/bin:$PATH"`，确保 subprocess 使用同一 Python 环境。

| 检查 | 结果 |
|---|---|
| `.venv/bin/pytest -q --disable-warnings --maxfail=5` | 533 passed，30 skipped，180.69 秒；运行于最终浏览器 revision 并发补丁之前 |
| 最终补丁后：`tests/api/test_grip.py tests/capability_services/test_grip_services.py` | 17 passed，86.37 秒 |
| 脚本提交契约、API 迁移、部署和生成物定向回归 | 89 passed |
| `scripts/generate_service_contracts.py --check` | 通过，覆盖全部五包 |
| compileall、三个 CLI 的 `--help`、`bash -n scripts/test-e2e.sh`、`git diff --check` | 通过 |

上述测试存在重叠，不将通过数相加。pytest 的跳过项由仓库既有标记和外部部署条件控制；
本环境 Docker daemon/Compose 不可用，依赖这些环境的验收未执行。

## 五包集成覆盖

`tests/capability_services/test_grip_services.py` 使用实际服务目录发布，贯穿：

```text
SDK 上传完整包 → GRIP 目录/描述 → Slave activate-package
→ activation 的认证与业务动作 → Driver 持久队列 → Slave
→ 真实 container:http runtime → 本机真实 gateway HTTP handler
→ IoContract / validator → GRIP Run read → 实例 stop
```

| 包 | 逐项执行的业务导出数 |
|---|---:|
| Kubernetes | 6 |
| Outline | 12 |
| Suanli Cloud | 12 |
| 普通 VM SSH | 1 |
| Suanli VM SSH | 1 |
| 合计 | 32 |

每个导出使用真实输入/输出契约，重复提交同一幂等请求只执行业务调用一次。
还验证外部错误与错误输出不会成为成功结果、SSH 非零退出码、无效输入、跨包发布依赖、
双实例隔离和停止。Suanli SSH 覆盖 pending_auth、GRIP auth-start、浏览器 bridge 上下文、
实际 runtime 健康检查恢复 ready，再执行 exec；外部浏览器登录完成后的密钥由测试准备。
通用 GRIP 测试另覆盖真实进程执行、HTTP Worker 转发、SQLite 重载、跨 workspace、
Schema、协议校验、认证、绑定信任、停止后 admission 和浏览器认证并发 revision 拒绝。

## 验证边界

Docker runner、外部云/Outline API 和 kubectl/SSH 可执行文件使用本地测试替身；
服务 gateway、HTTP 请求、runtime、Observer/Driver/Slave 执行与契约校验为实际实现。
本轮未部署生产环境，未执行真实云资源创建/销毁、真实 SSH/OIDC 浏览器登录、镜像构建推送、
真实 Docker 容器或 TLS 反代验收。已有历史生产测试记录不作为本轮 GRIP 接入的实测证据。
