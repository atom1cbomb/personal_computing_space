# 激活实例凭据配置验证

对应 [设计](../specs/2026-10-21-activation-credentials-design.md)。本次交付为本地代码、
数据库迁移、能力包模板、SDK 和 skill；以下为生产升级前的本地验收记录。
后续生产升级及 Kubernetes v4 修复结果见
[生产部署报告](2026-10-21-activation-credentials-production-upgrade.md)。

## 已验证行为

- 同一包、同一 Slave 的两个实例选择不同 profile，并生成各自的只读凭据挂载。
- GRIP 输入通过 Observer、Driver、远程命令、Slave 和插件链路传递；编排 sandbox 的
  `ensure_activation` 支持同一字段，改动同一 step 的选择会触发幂等冲突。
- 相同实例重试、停止后再激活、Observer 重启和 Slave 从数据库恢复均保留选择。
  重建模拟容器时沿用原挂载，revision 不增加。共享 digest 容器的包别名也不能改绑 profile。
- 非法名称、缺失目录、符号链接被拒绝；新实例未选择 profile 时不挂载。
  历史包 body 中的 profile 不再决定运行时挂载；新发布入口明确拒绝该字段。
- DESCRIBE 不公开实例的 profile、连接配置或凭据内容。
- PostgreSQL 迁移覆盖 Observer workspace 激活、Run 内激活和 Slave 副本，保留已显式
  设置的 profile/null，不修改包 JSON、摘要、URI、激活 revision 和 Run outcome。

## 验证记录

最终全仓库本地回归：**642 passed、6 skipped、14 deselected**，201.01 秒。

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q \
  -m 'not e2e and not integration' --tb=short --disable-warnings -ra
```

6 个跳过用例均属于已移除的旧 planner/provider/message 流程；14 个未选择用例属于
部署端 E2E 或 integration 标记，其中 PostgreSQL 的 8 项另行运行并通过。

专项生命周期与 Driver 回归：66 passed、4 skipped。覆盖新增凭据配置、GRIP、编排回放、
远程仓库和 Driver 执行。4 个跳过用例属于已移除的 remote planner/provider 流程，
不属于此次激活凭据链路。

真实 PostgreSQL 迁移测试 `tests/db/test_postgres_migrations.py`：8 passed。
五个真实服务包经 GRIP → Driver/Slave → HTTP runtime → 本机 gateway 执行全部 32 个业务
action；Docker、kubectl/SSH 和外部 provider 使用测试替身。这里不代表真实集群、浏览器
登录或生产 VM 验收。

`generate_service_contracts.py --check`、skill `quick_validate.py` 和 `git diff --check` 通过。
SDK wheel 和 skill 归档已构建，归档中仅有 SDK 0.6.0 wheel；服务器 wheel 测试检查包含
Alembic 007 和 MCUE 资源。产物：`dist/personal-computing-space.zip`。

## 交付与部署

模板：Kubernetes v3、Outline v2、算力云 API v4、普通 SSH v4；算力云 SSH 保持 v3。
四个新模板均不含包级 `credential_profile`。说明、示例和当前安装的 skill 已同步。

生产需按 [协调升级说明](../../activation-credentials-upgrade.md) 升级完整链路并迁移数据库，
再分发新 SDK/skill。新包模板本次未发布到生产 Observer，也未修改任何生产激活或凭据。
