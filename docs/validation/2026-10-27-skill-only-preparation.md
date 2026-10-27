# 仅凭 skill 的外部 Codex 验证：准备与阻塞记录

日期：2026-10-27。目标测试宿主机：`root@9.0.3.9:22`。

> 历史准备记录：当日稍后 SSH 已恢复，完整预检及 T1、T2、T4 已实际执行。当前结论见 [真实验证报告](2026-10-27-skill-only-live.md)；以下保留恢复前的证据与阻塞状态。

**恢复前结论：通用流程与运行器已完成准备，离线容器边界及 skill 目录检查通过；指定测试机的真实任务验收为 `blocked_environment`。该阶段没有启动被测 Codex 的模型会话，没有创建业务 Run，也没有修改生产资源。**

## 交付

- [通用验证流程](skill-only-codex.md)：输入边界、操作员准备、七类用例、自然语言提示词、独立复核、认证介入及清理要求。
- [隔离运行器](../../scripts/run_skill_validation.py)：每例新容器、新工作区、新 Codex 会话；启用唯一项目 skill；日志由操作员保留；业务成功须另行复核。
- [测试镜像](../../deploy/skill-validation/Dockerfile)：Python 3.12、Codex CLI 0.164.0、UID 1000，无 SSH 客户端。

本轮源码基线为 `e447c3b`，在其上新增上述文件。发布版 skill 附带 SDK **0.8.0**：

```text
skill ZIP SHA256:
e270bf4f8499cf13a4d02cad218c55256d3af4f2e3536fb12fe5bf79af2b83c0

本地构建的测试镜像 ID:
sha256:fa9754fa14f1f6c9df7b69058c6c9eaf9f451ef82c01f37babfdda6d105f9d8c
```

ZIP 位于未纳入 Git 的 `dist/skill-validation/`。镜像和 ZIP 没有传到 `9.0.3.9`。本轮未调用模型，实际模型栏为空；不能把 CLI 默认值写成已经验证的模型。

## 已验证

1. 镜像实际构建成功，发布 skill 实际生成，wheel 内没有服务端 `loom_v2` 模块。
2. `tests/deployment/test_skill_validation.py` 的 8 项测试通过，覆盖发布包要求、路径穿越、额外 skill、符号链接、服务端源码混入和误操作宿主网络的拒绝。
3. 在真实 Docker 容器中以 `--network none` 独立检查文件与 skill 边界：非 root、空工作区、无 `loom_v2`、无 SSH 客户端/agent/用户密钥目录、无 sudo、无 Docker socket，全部通过。
4. 通过 Codex 实际 `skills/list` 获取并关闭其他 skill，再次查询确认唯一启用项为 `personal-computing-space`；实际 CLI 为 `codex-cli 0.164.0`。此过程没有模型调用或服务调用。
5. 文档相对链接、Bash 片段语法、计算 oracle 与 diff 空白检查通过。

本地私有证据位于 `production/skill-validation/`，其中
`20261027-offline-container.json` 保存离线目录与边界结果。临时测试容器均已删除，未注入生产 token 或 Codex 认证。

## 阻塞与未覆盖

| 检查 | 实际结果 | 影响 |
| --- | --- | --- |
| SSH 登录 `root@9.0.3.9:22` | 三次尝试在 banner exchange 阶段超时，尚未进入账号认证 | 无法检查或准备指定测试机 |
| Observer `http://9.0.16.66:8080/healthz` | 当前执行环境请求超时 | 未确认当前公开入口连通性，不据此认定服务自身故障 |
| 本地完整预检 | Docker 报告的容器 PID 在操作员 `/proc` 不可见，运行器拒绝操作网络命名空间 | 网络隔离完整路径仍需在实际 Docker 宿主机上验证 |
| T1～T7 | 未执行 | 不能声称资源发现、远端计算、服务调用、认证恢复或包部署通过 |

离线 `--network none` 检查仅验证容器内权限和 skill 隔离，不替代完整预检的网络规则设置，也不替代外部用户任务验收。完整预检拒绝后未改为 host network、未放宽权限继续运行。

## 恢复步骤

确认 `9.0.3.9` 的 VPN/路由、SSH 端口或跳板入口后，由操作员按通用流程传入最小文件并在该 Docker 宿主机运行 `--preflight-only`。通过后，私有注入模型与 Observer 认证，使用全新会话先执行 T1、T2，再按事先选定的资源类别执行 T4。逐项通过公开 API 独立复核，再扩展到多节点及生命周期用例。

## 14:10（北京时间）连接复查

用户提示入口可能已恢复后，再次连接 `root@9.0.3.9:22`，仍在 SSH banner exchange 阶段超时。
独立 socket 检查显示 TCP 连接约 0.004 秒建立，但等待 6 秒未收到 SSH banner。
当前执行环境尚未进入 SSH 身份认证阶段，无法据此判断 root 账号或密钥是否可用；真实任务验收继续保持 `blocked_environment`。
