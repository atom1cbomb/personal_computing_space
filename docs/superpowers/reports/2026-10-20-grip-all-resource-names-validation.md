# 全部资源 GRIP 命名迁移验证

> 2026-10-20 术语更新：本文保留当时的设计或验收记录；现行能力包业务接口统一为 action，字段、命名和实例描述以 [action 设计](../specs/2026-10-20-grip-actions-and-activation-description-design.md) 为准。旧示例需按新契约重新生成后使用。

日期：2026-10-20。设计依据：[全部资源统一命名 spec](../specs/2026-10-20-grip-all-resource-names-design.md)。

## 结果

ResourceRef 的服务端和 SDK 模型只接受 GRIP URI。Schema、IoContract、descriptor、validator、程序、结果、包版本、Slave、激活实例、领域资源、执行器和运行时定义均已迁移。可读名称在前，内容摘要位于 `/sha256/<digest>` 后缀；descriptor 的语义摘要仍单独校验。

例如算力云实例查询输出 Schema 使用：

```text
grip://loom.local/workspaces/workspace-default/content/suanlicloud-http/schemas/instances-list-output/sha256/<digest>
```

操作源身份使用：

```text
grip://loom.local/workspaces/workspace-default/operations/suanlicloud/instances-list
```

ContentStore 持久化完整名称绑定并按摘要去重字节。GRIP 可以描述和读取内容，SDK read_json 使用该入口。发布器按文件用途命名，改写部署命名空间和依赖引用后重新计算摘要。资源目录直接使用原始 GRIP 身份；Slave 调度按完整名称匹配，不再通过 URI 后缀猜测目标。

原生成样例中固化历史包摘要的 orchestrate.py 已删除，使用模板与当前发布结果生成。旧会话集成测试已改用现行 Run 历史接口，与现有“会话接口已移除”的 API 测试保持一致。

## 自动化验证

| 检查 | 结果 |
| --- | --- |
| 全仓 `PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q --disable-warnings --tb=short -rs` | **574 passed，7 skipped**，222.06 秒 |
| 最后清理路径命名复验 `tests/driver/test_cleanup_continuation.py` | **14 passed** |
| 五包 GRIP 生命周期与全部业务导出集成测试 | **6 passed**，包含在全仓结果中 |
| 跨 authority/workspace 发布、持久化名称、伪造名称拒绝、旧名称拒绝、JSON 数组读取 | 通过，包含在全仓结果中 |
| SDK/server wheel、CLI、Worker、调度、资源清理与数据库重载 | 通过，包含在全仓结果中 |
| `scripts/generate_service_contracts.py --check` | 通过 |
| `git diff --check` | 通过 |
| 全仓旧资源 scheme 扫描 | 仅负向测试保留拒绝用例；生产、工件、脚本、示例、文档无旧 scheme |
| 生产/示例 JSON 与 Python AST 的 resource_id 字面量扫描 | 无非 GRIP 资源名称 |

五个服务包全部业务导出覆盖：

| 包 | 版本 | 导出数 |
| --- | --- | ---: |
| kubernetes-http | v2 | 6 |
| outline-http | v1 | 12 |
| suanlicloud-http | v1 | 12 |
| vm-ssh-http | v2 | 1 |
| suanli-vm-ssh-http | v2 | 1 |
| 合计 | | **32** |

7 项跳过中，6 项属于已经删除的 planner/message 流程；1 项部署统计端到端测试需要显式设置 `LOOM_LIVE_OBSERVER_URL`。服务包集成使用真实 gateway handler 和 Loom 执行链路，外部云 API、kubectl/SSH 与 Docker runner 使用测试替身。本次不声明真实云服务或已部署集群验收通过。

## 数据切换

不提供旧 URI 兼容解析。旧包、模板和持久化资源引用需要按新名称重新生成、上传和注册；内容字节可继续按摘要去重。HTTPS、OCI、数据库连接地址仍属于传输或存储配置，不作为 ResourceRef 身份。
