# Action 执行方式：生产部署与 E2E 验收

完成时间：2026-11-02 19:02（Asia/Shanghai）。

[设计](../specs/2026-11-02-action-execution-choice-design.md)已部署到 Observer、Pool、双 Driver 和三个 Slave。生产验收通过，七个验收 Run 已关闭，七个测试 activation 已停止，无本次遗留执行任务。

## 部署结果

| 服务 | 主机 | 发布镜像摘要（前 16 位） |
| --- | --- | --- |
| Observer、Pool | 9.0.16.66 | `318e399d58da5164` |
| Driver primary、secondary | 9.0.16.66 | `4c5c84f73c1aef21` |
| slave-a | 9.0.16.66 | `bbb3ade1d26e92ff` |
| slave-npc | 9.0.16.102 | `46e5a494920a7d61` |
| slave-autopaper | 9.0.16.113 | `421cea09a9ed9bf6` |

镜像使用 `action-execution-20261102` 标签；应用容器健康检查及 SDK 0.14.0 检查通过。三个 Slave 均启用 Docker init。Observer 仍在 8080，Pool 在 8093，两个 Driver 在 8090/8094。

Observer 和 Pool 的下载 manifest、SDK wheel、skill 压缩包逐项验证大小和 SHA-256，二者完全一致。生产 E2E 使用从生产下载后安装的独立 SDK，不导入仓库内的 SDK 源码。

| 下载 | SHA-256 |
| --- | --- |
| SDK 0.14.0 wheel | `55809e1657eb2211a4f04730080a390d963727488c430d99c3dcc8d9213eb26c` |
| personal-computing-space.zip | `8153aa05cf9c28e27d94cfa7ddcada6be9483a22b2054bf6221c66b7e37299c5` |

## 能力包与实例迁移

| 包 | 版本迁移 | 迁移实例 |
| --- | --- | --- |
| autopaper-http | v2 → v3 | slave-a / autopaper |
| kubernetes-http | v8 → v9 | slave-a / beijing-ict、nanjing、beijing；slave-autopaper / nanjing |
| ark-http | v1 → v2 | slave-a / ark-personal |
| suanlicloud-http | v7 → v8 | slave-a / default |

新版本保留生产服务镜像、业务 descriptor 和 IoContract，更新交互声明及使用文档。七个旧实例均停止，七个替代实例最终均 ready。五个持久认证卷在旧实例停止后备份并复制到新摘要对应的卷，保留配置与 credential profile。

Embedding 的 Kubernetes dependency 已指向 v9 activation 和对应 export；现有三个 VM 资源的算力云管理 activation 标签已更新到 v8。Embedding 最终 ready，实际请求成功。

slave-autopaper 在部署前已离线，最后心跳为当天 13:22。用户启动 VM 后完成该节点升级。其旧 task 包当时已退出新目录，因此使用已保存的旧控制器镜像执行一次受 revision 约束的正常停用流程，再升级该 Slave；未直接改写 activation 数据库记录。

## 生产 E2E

所有业务验证使用公开 SDK、GRIP 描述和发布 skill 中的单节点计划模板。SSH 仅用于部署、备份、持久化计数及故障注入。

| 验证 | 结果 |
| --- | --- |
| 同一普通函数直接执行与提交计划 | 两条路径返回相同业务值 42；task 保存一个 Attempt 和可读取的结果内容 |
| 直接调用不产生任务记录 | 直接调用前后均为 634 个 Run、589 个 slave-a Attempt、2842 个内容对象；包含慢调用及 deadline 失败验证 |
| 无隐式 30 秒上限 | Observer relay 32.18 秒、slave-a TLS 直连 33.05 秒、slave-autopaper TLS 直连 32.78 秒，均成功 |
| 显式 deadline 与进程取消 | 3 秒截止时间中止长调用；父子进程均已回收；随后调用成功 |
| 输入校验 | 不符合 Schema 的输入被拒绝 |
| 缺少直接路由 | slave-npc 报告 direct 不可用、task 可用；完整计划执行成功 |
| 三个 Slave 的单节点任务 | 均完成；结果各执行一次，并从内容存储读回验证 |
| 提交幂等与查询 | 相同 request_id 和原计划返回同一 Run；get-submission 找回同一提交 |
| 真实 Kubernetes 服务 | pods 查询分别直接执行和通过 Run 执行，结果一致 |
| 跨节点编排 | 两个 map 节点分别位于 slave-a、slave-npc，reducer 位于 slave-autopaper；三个 Attempt 完成，sum=84 |
| Driver 中断恢复 | 主 Driver 被强制停止，副 Driver 接管；execution_id、attempt_id 保持不变，业务执行计数为 1，结果为 78；随后显式启动恢复主 Driver |
| 真实 AutoPaper | venues-catalog 查询成功 |
| 真实 Embedding | 一条文本返回 2560 维向量，验证迁移后的 Kubernetes stream dependency |
| 历史与废弃契约 | 19 个历史 Run 通过公开 API 读取；5 个含旧 task 声明的包退出目录；新发布的 task 声明被拒绝 |
| Driver offer | 仍为 submit-plan、get-submission、how-to-use；没有 submit-action |
| UI 与清理 | Observer 首页跳转 Pool，Pool 首页 200；测试 Run 全部 closed，测试 activation 全部 stopped |

关键 Run：

| 场景 | Run ID |
| --- | --- |
| slave-a 单节点 | `run-68f93fb5f711` |
| slave-npc 单节点 | `run-1ef27062ab60` |
| slave-autopaper 单节点 | `run-2123f16285f2` |
| Kubernetes | `run-5c1cf4053d7c` |
| 两节点编排 | `run-fc0a227235cb` |
| 三节点编排 | `run-728b2f51bb72` |
| Driver 中断恢复 | `run-bf814e69e471` |

## 验收中修复的问题

1. Slave 原部署没有 init，进程组终止后子进程成为僵尸。生产三个 Slave、部署生成器和开发 Compose 均启用 `init: true`。重跑真实 deadline 测试，确认父子进程均消失；部署回归测试通过。
2. slave-a TLS 证书缺少 Authority Key Identifier，Python 3.13 默认严格验证拒绝连接。沿用原 CA、公钥、主机身份及有效期，重新签发含 AKI、SKI 和 Key Usage 的证书并热加载。严格证书校验和 SDK TLS 直连通过，没有关闭证书验证。slave-autopaper 原证书已满足要求。

slave-a 的直接 TLS 验收临时增加了仅适用于测试 activation 的 binding；验收后恢复原 Observer/Pool Compose，保留原有 relay 路由。测试标记文件已清理。

## 数据、备份与回滚

切换前确认没有正在执行或排队待执行的 Run，停止写入服务并保存切换点数据库备份。各 PostgreSQL 备份通过 `pg_restore --list` 校验；原镜像、Compose、环境文件及持久认证卷备份均保留。未在生产执行回滚演练。

最终核对结果：

- 原有 634 个 Run 的状态、结果、草稿、冻结内容、Attempt、动态节点和事件摘要不变。
- 原有 107 个包摘要不变，11 个 awaiting_decision Run 保留。
- 数据库版本仍为 `010_driver_pool`；PostgreSQL、MinIO、Registry 容器身份保持不变。
- 原环境文件逐字节不变；旧 task activation 的 desired_state=running 数量为 0。
- 最终正在执行的 Run 为 0。总计 641 个 Run，其中新增的 7 个均已关闭。

本机私有证据目录：`production/releases/2026-11-02-action-execution/`。
远端备份及部署证据：`/opt/loom-production/releases/action-execution-20261102/`。

其中 `acceptance.json`、各场景 `*-run.json`、`public-contracts-verified.json`、`business-health.json`、`dependencies-migrated.json`、`final-*.json` 记录验收结果；`deploy_remote.py` 提供镜像切换和应用回滚步骤。包与认证状态迁移需按 `packages-before.json`、`migration.json`、`state-copy-plan.json` 配套恢复，不能仅回退应用镜像。私有目录不提交凭据或配置快照到 Git。

## 本地验证

- 实现完成时全量非 integration/e2e 套件：1238 passed、33 skipped、34 deselected。
- 本次集成及 E2E 套件：21 passed、1 skipped；该 skip 是未配置地址的旧 live 测试，生产验证由上述独立 SDK 场景执行。
- init 修改后的部署相关测试：68 passed、1 deselected；排除的是硬编码 Windows Docker 路径的测试。实际生产 Compose 已由 Docker 执行验证。
- init 与进程取消针对性测试：15 passed。
- `git diff --check` 通过。
