# VM 浏览器认证与 SSH 生产验证

日期：2026-09-22。环境：Observer `9.0.16.66:8080`，Slave `slave-a`。

## 实测结果

- 创建 Run：`run-b78571450e2e`。
- 临时实例：`c91000ea-bbc5-4af9-a9de-2bf2d6bfa7d6`，名称 `loom-e2e-vm-a43c915452`。
- 配置：Debian 12、4 vCPU、8589934592 字节内存、50 GiB 磁盘，IP `9.0.16.163`。
- 访问包：`grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v1`，实例名 `vm-c91000`。
- 用户通过 localhost SSH 隧道完成浏览器登录，Observer 查询得到 `ready / authenticated`。
- SSH 经 Observer 内部 Slave relay → Slave → HTTP 能力包 → VM 执行，未直接 SSH 绕过能力包。
- Attempt：`attempt-vm-c91000-ssh-2`；命令 `id && cat /etc/os-release && uname -a`。
- 返回 root、Debian 12 和匹配的实例主机名；`exit_code=0`，`timed_out=false`，`truncated=false`，stderr 为空。
- 结果 digest：`bef66ca0966bd62624447e18941fed3de07b57befa069c29eb30d0e55bb76d0e`。
- Observer `GET /api/v1/content/{digest}` 返回 200，响应头及响应体 SHA-256 均与 digest 一致。

## 验证边界

本次确认实际创建、用户认证、远程 SSH 和内容落盘路径可用；SSH 步骤由测试端调用内部 relay，不是 Driver 生成编排后自动恢复执行的完整单 Run 验收。

SSH terminal report 为 `decision_required / attestation_required`，不是 `completed`：输出 Schema 校验通过，但 IoContract 声明了 success_semantics 而没有 success_validator_ref。命令执行成功不等于系统已自动完成业务验收。本次未绕过该状态，也未为了测试删除成功语义。

## 清理

通过 Suanli Cloud 能力包对上述唯一 InstanceId 发起 terminate。随后 instances/list 查询返回该实例 `State=terminated`；Observer 资源状态同步为 terminated。

通过 Observer deactivate 停用 `vm-c91000`，返回 `stopped`、activation_revision=2。VM 销毁不可恢复；内容寻址结果和能力包保留用于审计。
