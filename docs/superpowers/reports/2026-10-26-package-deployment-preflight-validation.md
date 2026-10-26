# 能力包部署前检查验证记录

对应 [spec](../specs/2026-10-26-package-deployment-preflight-design.md)。实现位于
`fix/package-deployment-preflight`，从 `66e40d3` 创建独立 worktree 开发。

## 结果

- 公开 `check-package` 以包导出、实例名、节点 lease/runtime 和服务路由给出
  required_interactions、ready 和逐项 issues。
- 激活、configure、Driver desired-state 写入和编排实例复用共用路由检查。
  数据入口缺失时，拒绝发生在实例状态写入与 provision 之前；同一幂等请求可在
  配置修复后继续，stop 不受影响。
- DESCRIBE 区分 runtime_state 和总体 state/available。撤销服务路由会显示
  degraded；恢复路由无需重建实例，已有可用动作仍可调用。
- 节点部署清单提前拒绝缺数据监听或签名配置的服务路由；认证门户显示节点入口
  故障原因。发布文档、示例和能力包技能均明确部署包不通过 SSH 修补基础设施。

## 验证方式与范围

`tests/api/test_package_deployment_preflight.py` 在本地启动独立 Observer、Slave
控制入口和 Slave 数据入口，使用真实 HTTP 通信。测试通过公开 SDK 上传契约、
发布包、检查、激活并调用业务动作；生产 Observer 组合不安装内嵌 Slave。
容器 runtime 使用替身，不声称这组测试验证了真实 registry、镜像拉取或公网 TLS。

覆盖无入口、缺签名、错误 key id、非法地址/audience、部分协议、实例专属配置、
节点离线/不支持、task-only、幂等重试、configure 拒绝、路由撤销/恢复和 stop。
其中无入口拒绝也在 Driver 不在线时验证，避免被后续 fencing 检查遮蔽。

## 执行结果

```text
python -m pytest -q --disable-warnings --tb=short \
  tests/api tests/capability_services tests/contracts tests/deployment tests/observer tests/driver
829 passed, 7 skipped

python -m pytest -q --disable-warnings --tb=short \
  tests/api/test_package_deployment_preflight.py tests/deployment/test_config.py \
  tests/driver/test_cleanup_continuation.py tests/contracts/test_auth_client.py
61 passed

python scripts/generate_service_contracts.py --check
通过

git diff --check
通过
```

广泛回归与开发并行，61 项针对性回归覆盖期间补入的节点状态检查、编排重试
和部署配置错误处理。7 项跳过来自未启用的 AutoPaper Docker 验收及已有的旧
planner 契约用例。警告主要为 Python 3.14 asyncio/测试依赖的弃用提示。

## 生产状态

本次没有 SSH、没有修改生产节点配置或重启生产组件。此前已部署的 AutoPaper
保持原状。新 check-package 和就绪投影需发布升级后的 Observer；认证错误提示
需更新 SDK/skill，节点配置校验在新版部署工具加载清单时生效。此记录是代码与
本地回归验收，不是生产升级完成记录。
