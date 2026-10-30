# VM 流式文件传输生产部署与验收

2026-10-30，按用户“部署生产试试”的要求，合并
[PR #6](https://github.com/yzs15/personal_computing_space/pull/6)，发布两个访问包，
完成三个已有 VM 接入的切换。Observer、Driver 和业务 VM 无需重启或数据库迁移。

## 发布内容

运行时代码来自合并提交 `35a9b82`。发布期间追加配置 schema 修正 `2bc9c53`：
生产内容库已有按 `application/schema+json` 存储的旧 SSH 配置 schema，
新版发布器使用 `application/vnd.loom.instance-config-schema+json`，同内容摘要
的媒体类型冲突使首次发布返回 `content_integrity_conflict`。
为新配置 schema 增加用途说明并重新生成描述符引用，产生独立的不可变内容；
旧 schema、旧包均保留。此修正不改变镜像内的运行时代码或配置字段。

| 包 | 不可变镜像 |
| --- | --- |
| `vm-ssh-http/v6` | `localhost:5000/loom/vm-ssh-gateway@sha256:1dc63047fbc1496d11362a2451403d299453ffcc0d4a5d67b24e9b9960b950e8` |
| `suanli-vm-ssh-http/v7` | `localhost:5000/loom/suanli-vm-ssh-gateway@sha256:5f699b8acdf89527f6499379b3448027543f8f496c536d8fe7a9c561b672f533` |

两镜像在本机构建，通过运维 SSH 传至现有 registry；包发布、路由预检、接入创建、
任务和文件操作均走公开接口。发布的诊断源码包含共享文件传输模块，逐文件回读比对
通过：普通 SSH 包 25 个文件，云 SSH 包 33 个文件。

独立 SDK wheel 与技能归档已构建，执行环境安装 `loom-orchestration 0.12.0`；
生产验收从 `/tmp` 运行，使用已安装 SDK，不依赖仓库源码导入。
云 SSH 沿用生产默认续期配置 `SSH_REFRESH_INTERVAL=0s`。

## 接入切换

三个新 activation 均通过 `check-package`，支持 `task` 与 `byte-stream`。
普通 SSH 根据原容器的运维挂载记录选择原 credential profile；两台云 VM 通过
统一认证门户自动复用浏览器会话完成新 activation 的首次登录，不复制旧认证状态。
文件操作与 `exec` 使用同一接入的 SSH 目标及认证。

所有 activation 都位于 `slave-a`，实例名保持如下：

| VM | 新包 | 实例名 | 切换后状态 |
| --- | --- | --- | --- |
| autopaper-vm | `vm-ssh-http/v6` | `vm-9-0-16-113` | ready / authenticated，revision 1 |
| loom-v2-auth-3c474f5bb5 | `suanli-vm-ssh-http/v7` | `loom-v2-auth-3c474f5bb5` | ready / authenticated，revision 1 |
| npc-test | `suanli-vm-ssh-http/v7` | `npc-test` | ready / authenticated，revision 1 |

三台全部验收后，通过公开 `stop` 停用旧普通 SSH v5 和两个旧云 SSH v6 接入。
旧包、镜像和持久状态保留，可按原配置重新激活回退。每条 VM 记录的 `activations`
现仅列出对应新接入，并提供两个 byte-stream 文件动作；资源身份、属性、属性修订号、
运行状态及管理关系逐项保持一致，无需重新登记 VM。

切换前没有执行中的 Run。11 条历史待决记录保持原状，其中一条引用普通 SSH v5，
其唯一 attempt 已于 10 月 26 日失败，等待 repair，无动态节点或活跃执行。
没有替用户作 accept/abandon 决定；后续 repair 仍须提交新计划。

自动登录完成后关闭本次辅助进程，并恢复统一门户，更新其中两条 VM 接入引用。
恢复后的自动登录开关关闭；后台 SSH 认证续期不依赖门户进程。

## 生产验证

三台 VM 分别上传并下载 **22,020,099 字节（21 MiB + 3 字节）** 的随机二进制文件。
远端文件名包含中文、空格和单引号。源文件及三份下载结果的 SHA-256 均为：

```text
06b8e3b784120e1d9ec632b01d7e6c6dfc43ac32b6e68b7221d2aae504898255
```

| VM | 本次大文件上传加下载耗时 | 结果 |
| --- | --- | --- |
| autopaper-vm | 20.687 秒 | 大小、SHA-256 一致 |
| loom-v2-auth-3c474f5bb5 | 20.364 秒 | 大小、SHA-256 一致 |
| npc-test | 19.826 秒 | 大小、SHA-256 一致 |

每台还通过了以下检查：

- 空文件上传、下载；默认拒绝覆盖已有文件。
- 故意发送错误 SHA-256，返回 `file_integrity_failed`，原文件保持不变。
- 显式允许覆盖时成功替换；下载文件权限为 `0600`。
- 上传未提交即断开，不发布目标文件，也没有遗留 `.loom-upload-*` 临时文件。
- 文件操作期间 Run 集合不变，认证保持 authenticated，activation revision 保持 1。
- 通过原 `exec` 动作创建、清理独立测试目录；6 个 Run 全部成功并关闭。

旧接入停用后，使用已安装的 `loom-vm-files download` 从三个新接入读取
`/etc/hostname`，再次验证返回大小与 SHA-256，确认 CLI 及新认证独立可用。
这三次下载同样不产生 Run。测试远端目录全部清理。

本次新增两个“已有旧媒体类型配置 schema 后发布新 VM 包”的回归用例。
配置与资源命名测试 **23 passed**，部署预检测试 **13 passed**；契约生成检查和
`git diff --check` 通过。功能实现阶段的完整相关回归为 **414 passed、2 skipped**，
详见 [实现验证报告](2026-10-30-vm-file-streams-validation.md)。

私有部署快照、脚本、幂等调用记录、认证状态和验收证据位于 gitignored
`production/releases/2026-10-30-vm-file-streams/`；最终判据为 `verification.json`、
`cutover.json` 与 `post-cutover-verification.json` 的 `status=passed`。

当前云创建包 `suanlicloud-http/v7` 仍固定引用 SSH v6，此次只升级上述三个已有 VM
接入和发布新访问包。新建 VM 的自动接入版本需随创建包另行更新。
使用方式见 [VM 文件上传与下载](../../vm-file-transfers.md)。
