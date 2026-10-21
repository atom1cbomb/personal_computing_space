# Volcano 能力包验证

对应 [设计](../specs/2026-10-21-volcano-capability-design.md)。

新增 `volcano-http/v2` 的六个只读 action，以及 `computing/volcano/v1` 公开属性定义。
通过独立的面板实例配置接入不同地区；公开描述通过 `kubernetes_ref` 关联现有 K8s。

## 本地验证

- Volcano gateway：**33 passed**。覆盖 HTTPS origin 与端口、namespace=All/指定范围、
  分页完整性、上游鉴权与非 JSON 错误、GPU 分配与待调度需求、已结束 Pod 排除、
  init/sidecar/overhead、整数资源数量和参数边界。
- 专项组合：**73 passed**。包含 gateway、六个真实包经 GRIP → Driver/Slave → HTTP
  runtime → gateway 的全部 38 个业务 action、MCUE、通用配置挂载、服务器 wheel。
  Docker 和外部服务在此组合中使用测试替身，不代表生产面板已经验收。
- 全仓库回归：**681 passed、6 skipped、14 deselected**，220.96 秒。
  六项跳过均为已删除的旧 planner/provider/message 流程，十四项为 E2E/integration
  标记用例。本次没有数据库迁移，不重复执行既有数据库迁移验收。

```sh
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q \
  -m 'not e2e and not integration' --tb=short --disable-warnings -ra
```

`generate_service_contracts.py --check`、`git diff --check`、skill quick_validate 通过。
重新构建 skill ZIP，包含 `references/volcano.md` 和 SDK 0.6.0 wheel；归档中的 Volcano
说明与工作区源文件字节一致。

## 集成中修正的现有问题

HTTP runtime 原先要求所有 `instance_config` 都包含 SSH host/user/port，导致 Volcano
激活失败。现改为传递服务自有 JSON，保留运行时生成的 target_resource_ref，并拒绝
配置覆盖该保留字段。Observer 继续使用包 descriptor 的配置 schema 验证，连接语义
由各 gateway 验证。原有 SSH 的配置验证仍在 SSH gateway 中，六个包的回归全部通过。

真实接入又确认生产存在 split DNS；最终 v2 将公开 dashboard_url 与实例 api_origin
分开，避免将外网端口直接用于生产内网 ingress。该契约修正后再次运行上述 73 项专项
验证，覆盖公开地址与运行时入口不同的资源关联场景；全仓库记录为入口分离前的回归。

Volcano 的参数和实例 schema 使用现有 io.v1 子集；origin/namespace 的详细语义由
网关验证，未为新包扩展 Observer/Slave 的 schema 协议。

生产发布和真实面板 action 的验证结果见
[生产部署报告](2026-10-21-volcano-capability-production-upgrade.md)。
