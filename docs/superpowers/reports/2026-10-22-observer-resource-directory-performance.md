# Observer 资源目录性能验证

实现依据：[资源目录按需读取 spec](../specs/2026-10-22-observer-resource-directory-performance-design.md)。本报告记录本地验证；后续部署与生产实测见 [生产部署报告](2026-10-22-observer-resource-directory-production-upgrade.md)。

## 改动

- 工作区、内置 action 和固定 Schema 在 Observer 初始化时构建；固定资源和 content 查询不扫描动态目录。
- DESCRIBE/INVOKE 按目标解析条目，list-resources 在投影前按 MCUE 筛选；只为 DESCRIBE 返回的条目计算 revision。
- 包与激活实例批量读取，包含 Run 中已发布的可见包；查询不读取包事件和完整执行历史。Run 列表仅读取 ID/状态，read 按需读取状态和结果。
- 复用按包 URI/摘要标识的不可变 action 声明与 Schema。动态发布状态、激活状态、属性、租约和 Run 状态每次读取，查询结果不写回执行路径的内存记录。
- SDK 文件和资源列表的六次 GRIP 请求流程均未修改。

## 本地对照

基线为提交 `10078fd491da452bdaaa1093ca4978865c2daf57` 的 GripService；两个实现使用同一数据库、同一 SDK 和同一合成数据。环境为本地 SQLite、ASGI 和模拟 S3，不包含生产网络延迟。

数据包括 23 个包（每包 6 个 action）、38 个激活实例、85 个 Run、8 个注册 VM，共 612 项目录资源。每个操作执行四次，表中耗时取后面三次的中位数。最终测量在回归测试结束后单独运行。

| 操作 | 基线耗时 | 优化后耗时 | SQL 次数 | GRIP 请求数 |
|---|---:|---:|---:|---:|
| SDK resources() 完整列表 | 5.802273 s | 0.065439 s | 174 → 6 | 6 → 6 |
| 固定输入 Schema read | 1.952745 s | 0.001134 s | 58 → 0 | 2 → 2 |
| SDK resources(mcue="capability/activation") | 5.918037 s | 0.051942 s | 174 → 4 | 6 → 6 |

首次完整列表分别耗时 5.649260 s 和 0.107626 s。全部 612 项资源的描述（包含 metadata、offers、revision）与基线逐项相等；列表顺序不作为一致性判据。

上述数字是同机合成数据的对照结果，不是生产升级后的延迟。此前生产 531 项目录的 23.18 秒测量不能直接与此表比较；生产环境的单独对照测量见生产部署报告。

复现命令：

```bash
PYTHONPATH=. .venv/bin/python tests/benchmarks/grip_directory.py --baseline-ref 10078fd
```

## 回归

```bash
.venv/bin/pytest -q --disable-warnings tests/api tests/observer tests/contracts tests/db tests/content
```

结果：344 passed，8 skipped。8 个依赖 PostgreSQL/Compose 的集成测试因环境不可用跳过。

新增 8 项回归覆盖固定资源零扫描、包数量增长时 SQL 次数不增长、过滤跳过无关数据源、列表不计算描述摘要、逐项描述与列表一致、注册资源使用内置 MCUE、数据库外部写入后的状态与属性更新、预热后撤销包即隐藏 action/Schema、Run 状态与结果实时读取、工作区隔离及共享 action 身份的当前版本集合。原有调用、停止、认证恢复、绑定歧义、输入校验与重启恢复测试全部通过。

`git diff --check` 通过，`loom_orchestration/` 无改动。
