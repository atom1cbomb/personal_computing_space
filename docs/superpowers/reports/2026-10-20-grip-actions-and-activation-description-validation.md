# action 术语与激活资源 DESCRIBE 验证

日期：2026-10-20。设计：[action 和激活描述 spec](../specs/2026-10-20-grip-actions-and-activation-description-design.md)。

## 实现与用户可见行为

- `activate-package` 返回自动投影的激活资源 URI，无需额外注册；实例可通过目录发现。
- 实例 `metadata.actions` 包含全部业务动作的 URI、名称、描述、descriptor、IoContract、业务输入/输出 Schema 引用、权限、副作用及可用状态。算力云为 12 项。
- ready/running 时 offers 提供业务调用 binding；pending_auth、failed、stopped 时声明保留，业务动作不可调用。管理动作继续通过 offers 提供。
- 统一 `ActionExport`、`action_exports`、`action_descriptor_ref`、`action_ref`、`action.v1`、action descriptor 媒体类型和摘要域；SDK、Worker/Slave、runtime、服务网关、脚本、当前文档与示例同步。
- 异步动作回执统一 `{run: <Run URI>}`；网关业务响应为 `{action, result}`。
- 五包的 Schema/IoContract/descriptor 摘要重新生成；补上算力云 `instances-run` 的文字描述。
- migration 005 将 Slave attempt 的 `operation` 列重命名为 `action`，保留记录；历史 migration 与 patch 幂等 `operation_id` 继续表达原有含义。

## 验证记录

```text
PATH=/root/loom_v2/.venv/bin:$PATH .venv/bin/pytest -q --disable-warnings
576 passed, 7 skipped in 212.15s

.venv/bin/python scripts/generate_service_contracts.py --check
通过，生成内容无漂移

git diff --check
通过
```

7 项跳过沿用现有测试条件：6 项旧 planner/message 流程，1 项要求 `LOOM_LIVE_OBSERVER_URL` 的部署环境验收。

覆盖五个真实服务包、全部 32 个业务 action 的发布、激活、描述、调用、Run 读取和停止；通过真实 GRIP/Driver/Slave/container:http 代码及本机 HTTP gateway，Docker、SSH/kubectl 和外部 provider 使用测试替身。新增检查覆盖所有动作说明、Schema 引用可读取、pending_auth/failed/stopped 状态及拒绝调用。

数据库迁移使用一次性真实 PostgreSQL 容器，验证旧列已有记录升级后保留，以及 observer/slave 的新库、失败回滚和并发迁移。整套测试还覆盖独立 SDK wheel 和服务器 wheel 的构建。

## 部署边界

本次未向生产发布或激活服务。旧业务字段不提供别名；应更新 Observer、Driver、Slave、SDK 和 runtime plugin，重新构建服务镜像与契约并选择新包版本发布。数据库列迁移不转换旧包、Run/Closure JSON 内容。历史 spec/验收报告保留原始记录并标注现行设计入口。
