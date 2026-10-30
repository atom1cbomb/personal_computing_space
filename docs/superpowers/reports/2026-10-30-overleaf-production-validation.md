# Suanli Overleaf 生产部署与真实账号联调

2026-10-30 按用户要求部署 `suanli-overleaf-http/v1` 并完成真实账号联调。用户随后要求
只测试只增操作，因此本次仅读取、新建测试项目和新增文件，没有删除或覆盖任何已有
项目文件，也没有做能力实例的停止/重启业务测试。

## 部署结果

- 包：`grip://loom.local/workspaces/workspace-default/packages/suanli-overleaf-http/v1`
- 激活：`grip://loom.local/workspaces/workspace-default/activations/suanli-overleaf-http/v1/slave-a/overleaf`
- 最终状态：`ready` / `authenticated`，revision 1。
- 镜像：`localhost:5000/loom/suanli-overleaf-gateway@sha256:3f1f824d67063f22e1b1837f631909f27ac1da9c71e454536d7e0cbb755033ad`
- `check-package` 通过，request-reply 和 byte-stream 均就绪。
- 激活选择私有 `overleaf-prod` credential profile；Git token 通过只读凭据目录提供，
  网页会话由统一登录入口建立，没有将凭据放入包、普通配置、业务请求或报告。

Observer、slave-a 和 Driver 从各自现有镜像离线构建升级，保留既有依赖、CA 信任、
数据和配置，以支持点号 Cookie 名与 browser-cookie 的 pending 回调。
部署前没有正在执行的 Run；切换前保存 compose、环境文件、原镜像标签和两个数据库
的 `pg_dump -Fc` 备份，并用 `pg_restore --list` 检查备份格式。

核心镜像标签为 `loom-production-<role>:overleaf-20261030`。发布源码归档 SHA-256：
`d716c3c86a826d7ab60eeb493fd058bfc0fddc616e0ca4ceed2fd6688ed038b5`。
归档和逐文件清单记录了基于 `b6977b2` 的当前工作区源码；这不是已提交的 Git release。
平台切换后，原有 1425 个资源 URI 全部保留，原有 activation 的公开状态无变化，
业务容器 ID 和镜像保持不变。Observer 健康检查及 execution/orchestration 均正常。

## 真实测试

原账号列表返回 39 个项目。通过能力包创建一个专用测试项目，并在联调结束时保留：

[Loom Overleaf integration](https://overleaf.cs.ac.cn/project/6ae3f34ff7c17733af50417e)

| 检查 | 结果 |
|---|---|
| `projects.list` | 获取真实账号列表，并能查到新导入的项目 |
| `projects.import` | ZIP 导入成功；相同键和内容重试返回同一项目 ID |
| `projects.download` | Git `main` 分支快照与清单一致，文本和 PNG 原始字节一致 |
| `projects.status` | 未修改时 unchanged；新增提交后旧基准能检测到变化 |
| `projects.upload` | 只新增 `loom-added.tex` 和 `loom-sample.bin`，删除列表为空 |
| 上传重试 | 从另一个仍保存旧基准的本地副本重试相同键，返回同一提交 |
| 旧基准冲突 | 旧副本尝试新增另一个文件被拒绝，远端没有产生该文件 |
| 内容完整性 | 最终重新下载后，原始文件摘要不变，新增二进制字节一致 |

初始 Git 版本为 `ae231b58be93898681dbdb32a1e9a3e20b64b038`，新增后的版本为
`f1984cf0218766097c6f008f9b83f0e08f5904e1`。状态差异只含两个 `A`，没有 `M` 或 `D`。
联调结束时测试项目和新增文件均保留；用户原有项目未写入。

没有把本次旧基准测试描述为网页端与 Git 推送同时发生的竞态验证。删除、覆盖已有
文件、真实网页会话过期和能力实例重启恢复也不属于本次真实测试范围。

### 联调后的项目清理

2026-10-30 10:33（北京时间），用户明确要求删除
`Loom Overleaf integration 2026-10-30 42c5a00a`。当前能力包没有删除动作，
因此复用统一登录保存的浏览器会话，在 Overleaf 网页中核对项目 ID
`6ae3f34ff7c17733af50417e` 后执行 Trash 并确认，服务端返回 HTTP 200。
该操作将项目移入回收站，未执行永久删除，也不属于能力包删除动作的测试。

随后通过公开 `projects.list` 复核：活动项目从 40 个变为 39 个，仅该项目 ID
消失，其余项目的 ID、名称、链接与访问权限均未变化。私有操作回执保存在
`production/releases/2026-10-30-overleaf/cleanup-receipt.json`。

## 联调中修复的问题

首次镜像推送受 Docker Desktop 与 WSL 的 localhost 网络边界影响。改为传输本机已构建
的镜像归档，在既有生产 registry 主机推送，再以不可变摘要发布能力包。

平台发布检查发现原 compose 的 runtime 插件挂载使用相对路径，未切换到新源码。
已修正目标 Slave 的挂载并重建该核心容器，通过文件 SHA-256 检查确认新 runtime
生效；其他业务容器保持不变。随后统一登录成功。

首次字节流联调在发送任何归档数据前发现 SDK 将自定义 SSL context 传给了显式允许
的 `ws://` binding，websockets 因协议不匹配拒绝建立连接。已修复为仅向 `wss://`
传入 SSL context，保留原来的 binding 信任校验和禁止自动重放的行为。
SDK 0.9.2 包含该修复及 pending 登录支持，已构建 wheel 和技能归档。
WebSocket、WSS 证书校验与完整 Overleaf GRIP 回归共 6 项通过；修复后上述真实流程全部通过。
SDK 独立安装与技能发布包校验另有 12 项通过。清理前的最终只读复核确认五个动作均可用，
测试项目仍处于上述新增后的版本，状态查询返回 `changed=false`。

## 运维与回退

私有联调资料位于本机 `production/releases/2026-10-30-overleaf/`，包括发布结果、
预检、激活记录、只增测试脚本和终态证据、客户端发布包。该目录被 Git 忽略。
核心发布与回退资料位于生产主机 `/opt/loom-production/releases/overleaf-20261030/`。

核心回退可使用发布脚本的 `rollback <role>` 恢复原 compose 与镜像；原镜像标签为
`loom-production-<role>:before-overleaf-20261030`。本次没有数据库结构迁移，不需要为
应用回退恢复整库。回退旧核心将失去 Overleaf 所需认证支持，应同时停用该新能力实例。
服务包、认证卷及操作账本应保留，尤其不能在迁移时丢弃未决导入记录并自动重放。
