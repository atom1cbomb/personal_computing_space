# 资源与访问包解耦：生产任务实测

2026-10-30，完成[生产部署](2026-10-30-resource-access-bindings-production.md)后，按用户要求执行真实任务。使用已安装的独立 SDK `loom-orchestration 0.11.0`，通过公开 GRIP 提交、读取结果和关闭任务。

**8 个 Run 全部执行成功并关闭**。结果均读取自生产内容存储并校验摘要，数值另用独立公式核对；没有用本地计算代替远程任务结果。

| 测试 | Run | 结果 |
| --- | --- | --- |
| AutoPaper VM SSH | `run-cd35de8551e2` | v5 实例执行 Python 校验脚本成功，退出码 0 |
| 认证测试 VM SSH | `run-e59debf5ace8` | `loom-v2-auth-3c474f5bb5` 执行成功，退出码 0 |
| npc-test SSH | `run-b086e0dac290` | 执行成功，退出码 0 |
| slave-a 远程计算 | `run-dc7a234a1b9a` | 1–1000 的计数、总和、平方和全部正确 |
| slave-autopaper 远程计算 | `run-beff8defa6e2` | 同一计算正确，执行证据确认目标节点 |
| slave-npc 远程计算 | `run-8f11844ac688` | 同一计算正确，执行证据确认目标节点 |
| 三节点分片及远程归并 | `run-0c40b852c35d` | 3 个 map 节点和 1 个 reduce 节点全部完成 |
| 编排内资源登记与后续计算 | `run-5a68dfcda83e` | 新登记协议成功，重复步骤只记录一条登记事件，随后远程计算完成 |

SSH 校验只在内存中计算 1–10000 的总和及固定文本的 SHA-256，返回值一致，未改动业务文件。测试前两个云 VM 的网页登录认证已过期，复用之前授权保存的浏览器会话自动恢复；AutoPaper VM 的 credential-profile 认证保持原状。

## 分布式执行证据

三个分片分别为 1–10000、10001–20000、20001–30000，实际落在 `slave-a`、`slave-autopaper`、`slave-npc`。最终归并节点位于 `slave-a`，直接接收三个上游结果引用，返回：

```json
{"count":30000,"sum":450015000,"sum_squares":9000450005000}
```

结果还记录了三个不同执行环境的标识。四次节点尝试均为 completed，未出现 terminal_error。此项从提交回执到结果确认及关闭约 75 秒；这是轻量功能验收，不是吞吐或压力测试。

## 登记与任务生命周期

真实 Python 编排对已有 `autopaper-vm` 连续两次调用同一 `ctx.register_resource` 步骤，提供已有 URI、MCUE 和相同属性，不传包引用。只产生一条 `program_resource_registered` 事件，事件参数不含 `package_ref`。之后完成一个远程计算节点，资源完整 DESCRIBE 与调用前一致，属性修订号仍为 4。

三个单节点任务分别使用相同业务键重复提交，均返回原 Run。完整编排计划重复提交和通过业务键找回也返回原 Run；改变计划内容后复用键按预期返回 `request_id_reused`，没有产生额外任务。已关闭的编排 Run 再次关闭成功，冻结计划读取结果不变。

## 收尾与证据

8 个测试 Run 均已关闭；4 个测试专用函数 activation 均已停止。保留以下四个不可变测试包作为复现材料：

- `production-smoke-stats-20261030/v1`
- `production-smoke-reduce-20261030/v1`
- `production-smoke-map-reduce-20261030/1`
- `production-smoke-registration-20261030/1`

原有 512 条 Run 的公开记录保持一致，包括 11 条历史待决任务。未创建、删除或修改业务资源；两个云 VM 的认证恢复是本轮预期的业务实例状态变化。

私有脚本、输入/输出契约、发布包引用、回执、完整结果、节点证据、认证前后资源快照及最终核对结果保存在 `production/releases/2026-10-30-task-smoke/`。最终汇总为 `verification.json`。凭据和完整私有输出未提交到仓库。
