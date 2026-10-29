# 资源依赖 metadata 验证

实现对应 [设计文档](../specs/2026-10-29-resource-dependency-metadata-design.md)。

## 验证结果

Docker 启动后，重跑此前受阻的动态节点、map/reduce 及编排 Pyright 预检测试：

```sh
python -m pytest tests/api/test_dynamic_nodes.py tests/contracts/test_workflows.py \
  tests/observer/test_orchestration_preflight.py -q --disable-warnings --tb=short
```

结果：**32 passed**，此前 `orchestrator_runtime_unavailable` 阻塞已经解除。
本轮没有修改 Pyright 相关代码。全仓库静态扫描的既有报错按用户要求暂不清理；
编排程序的 Pyright 预检测试通过不代表全仓库类型检查通过。

以下是 Docker 启动前的相关回归记录，覆盖 API、SDK contracts、数据库、部署配置、服务包产物与 Slave：

```sh
python -m pytest tests/api tests/contracts tests/db tests/deployment \
  tests/capability_services/test_service_contract_artifacts.py \
  tests/slave/test_runtime_plugins.py tests/slave/test_worker_api.py \
  --ignore=tests/api/test_dynamic_nodes.py -q --disable-warnings --tb=short
```

结果：490 passed、8 skipped、1 failed。失败的是
`test_map_reduce_executes_three_nodes_and_returns_persisted_reducer_result`，
返回 `readiness_blocked / orchestrator_runtime_unavailable`。

`tests/api/test_dynamic_nodes.py` 单独核对：16 项测试同样因缺少可用 Docker
编排 runtime 受阻，3 项通过。已将原始 HEAD 归档到独立目录，使用相同 Python
环境运行整个文件及上述 map/reduce 测试，复现相同的 16 + 1 项失败。
这些环境限制没有通过修改生产代码或放宽断言来绕过；Docker 启动后的复验结果见上文。

最终针对创建属性映射、真实服务包发布、配置 schema 上传的检查：

```sh
python -m pytest tests/api/test_resource_attribute_mappings.py \
  tests/contracts/test_grip_resource_names.py tests/api/test_content.py \
  -q --disable-warnings --tb=short
```

结果：39 passed。`git diff --check` 通过；服务包生成器的 `--check`
由相关回归中的产物测试验证通过。

## 已覆盖行为

- 自定义类型、URI 数组和嵌套字段均按 schema 投影关系；错误标记、缺少说明、
  非法 URI、缺少必填值被拒绝。DESCRIBE、list-resources 与 SDK inventory 一致。
- how-to-use 只包含关系字段说明，不复制实际依赖 URI 或私有实例配置。
- Slave 公开 metadata 从部署配置传入 Settings，注册前校验；非法重注册不替换
  有效租约。节点心跳过期后、Observer 数据库重启后，依赖信息仍被保留。
- 创建动作的必填属性映射、来源路径、基本类型及常量值受到检查；SDK 按声明
  生成登记属性，最终 provider 输出仍需通过注册校验。普通属性的本地 schema
  引用继续参与常量值校验。
- check-package 接收拟用配置且无部署副作用；activate/configure 重用校验，
  失败时不调用 runtime。省略配置的预检使用已有实例配置。
- ServicePackage 从描述符引用识别配置 schema，使用独立内容类型上传。
  业务 IO schema 保持原有受限校验。embedding-http/v2 示例可完整发布。

## 上线范围

本记录初次提交时仅修改仓库代码、类型、SDK、文档和示例。
随后已按用户要求提交并部署生产；组件、节点关系、备份与线上验收见
[生产发布记录](2026-10-29-resource-dependency-metadata-production.md)。
既有 computing/slave/v1 不被覆盖；未填写依赖的旧节点仍返回未知关系，
系统不会根据 IP、名称或历史日志自动补写。
