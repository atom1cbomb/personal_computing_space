# GRIP Driver 与原生任务生产部署

2026-10-30，按用户要求将合并提交 `e4008b9e8c572e94b66dbf7bf281ed8fff08faf9` 部署到现有生产环境。实现范围见 [合并验证记录](2026-10-30-grip-native-tasks-validation.md)。

## 发布内容

- Observer 和 Driver 使用同一源码快照，镜像标签均为 `grip-native-tasks-20261030`。
- Observer 提供逻辑 Driver、冻结计划和 Run 生命周期的 GRIP 接口，以及配套 Web 页面。
- 服务镜像和本机独立 SDK 升级到 `loom-orchestration 0.10.0`；保留自动复用登录功能和正在使用的登录助手。
- Slave、业务网关和数据库容器保持原实例；本次没有数据库迁移或依赖变更。

| 组件 | 部署镜像 ID |
| --- | --- |
| Observer | `sha256:cb63af61dc8343dc7acd75c4613927f3051f858e8662ab64e1811b8486bf53ee` |
| Driver | `sha256:f9f348097e78dbb4103b8004438e0b9d7fca18415f1f545d87dadc8093d54484` |

源码归档 SHA-256：`e7c46da8bbce0f652b5a59d1cacdf777533b130ce12f7b580fd41ae8e4e093ab`。

独立 SDK wheel SHA-256：`4f2bdc2987f6ef9e2a02b17cf81b7a42a40460318a3e8ad4c3680a7c464177ed`。交付物位于本地 `dist/loom_orchestration-0.10.0-py3-none-any.whl` 和 `dist/personal-computing-space.zip`；未发布到包注册表。

旧任务 REST 接口已移除。外部任务调用方需要使用 SDK 0.10.0 或新的 GRIP 契约；只更新客户端文档不能替代升级客户端。

## 部署与回归

切换前确认没有 `queued`、`committed`、`running` 或 `cleaning_up` 的任务。备份两项服务的 Compose、环境配置和旧镜像，并对 Observer 数据库生成 41,102,816 字节的 PostgreSQL custom-format 备份，使用 `pg_restore --list` 验证可读取。基于现有生产镜像离线安装新源码和 SDK，保留生产依赖及证书。

本轮重点回归：

```sh
.venv/bin/python -m pytest \
  tests/api/test_grip_native_tasks.py \
  tests/api/test_native_plan_projection.py \
  tests/contracts/test_orchestration_client.py \
  tests/driver/test_worker_discovery.py \
  -q --disable-warnings --tb=short
```

结果：**30 passed，192 warnings，20.48s**。本轮未重复完整回归；合并阶段的 161 项回归及既有 Docker 环境限制见前述记录。

## 生产验收

验收程序从 `/tmp` 使用实际安装的独立 SDK，经公开 GRIP 和内容/包发布接口执行，未使用内部任务 API。

| 检查 | 结果 |
| --- | --- |
| Driver 发现和契约 | `computing/driver`、`how-to-use`、提交/找回 Schema 可读取，接受提交且在线 |
| Driver 重启身份 | 实例改变，epoch `1751 → 1753`；资源仍为 `grip://loom.local/workspaces/workspace-default/drivers/default` |
| 原生完整计划 | `run-cd159e9d8604` 完成；动态节点实际在 `slave-a` 执行，结果内容为 `{"value":42}` |
| 幂等与响应找回 | 相同业务键和计划返回相同 Run/Plan；`get-submission` 可找回；改变内容复用键返回 `request_id_reused` |
| 冻结计划 | 读取 `plans/run-cd159e9d8604/committed-7ea9855a9012`，版本及摘要与回执一致；关闭 Run 后内容不变 |
| 普通函数任务 | 经 Slave `run-package` 提交，`run-43dedb76c509` 完成，结果内容为 `{"value":46}` |
| Run 关闭 | 两个测试 Run 均已关闭；原生 Run 重复关闭成功 |
| 历史读取 | 分别读取已有 completed、failed、awaiting_decision Run 及其计划，状态保持一致 |
| Web 与旧路由 | 线上 HTML/JS 与发布源码字节一致；旧任务查询路由返回 404 |
| 服务运行 | Observer/Driver 健康检查通过，execution/orchestration 可用；容器重启计数 0，日志无 ERROR/Traceback/HTTP 500 |

原有 **88 个包的摘要、94 个激活实例的状态/版本/认证摘要、508 个历史 Run 的状态全部保持不变**。11 个等待人工决定的 Run 未处理。除 Observer 和 Driver 外，同主机其余 **22 个容器的 ID 和镜像未改变**。

验收新增两个不可变测试包 `grip-native-double/v1`、`grip-native-plan/v1` 和两个已关闭 Run，作为执行证据保留。测试函数激活实例在完成后已停止。资源目录从 1447 项增加到 1990 项，主要新增 510 个冻结计划投影；这些投影不代表重新执行历史任务。未删除原有资源。

本次生产验收未主动制造在途 Driver 故障，也未在生产执行修复、人工验收或取消测试；这些路径的自动化覆盖见合并验证记录。

## 运维证据与回退

私有脚本、快照、回执和结果保存在本地 `production/releases/2026-10-30-grip-native-tasks/`。生产主机的发布目录为 `/opt/loom-production/releases/grip-native-tasks-20261030/`，包含镜像状态、配置及数据库备份。凭据和完整私有输出未提交到仓库。

需要回退时，先确认没有执行中的任务，再在生产主机运行：

```sh
python3 /opt/loom-production/releases/grip-native-tasks-20261030/deploy_core.py rollback driver observer
```

该操作恢复原 Compose 和旧镜像，保留现有数据；本次无数据库迁移，不需要为代码回退覆盖数据库。配套任务客户端应同时恢复 SDK 0.9.4。旧镜像已保留为 `loom-production-{observer,driver}:before-grip-native-tasks-20261030`。
