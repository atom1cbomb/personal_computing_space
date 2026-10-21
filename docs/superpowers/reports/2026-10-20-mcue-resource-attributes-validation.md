# MCUE 实现验证

日期：2026-10-20。对应 [设计方案](../specs/2026-10-20-mcue-resource-attributes-design.md)。
实现阶段完成代码、数据库迁移、SDK、能力包模板和 skill 更新，使用本地隔离环境验证。
随后经用户要求完成生产部署，见 [生产升级报告](2026-10-20-mcue-production-upgrade.md)。

## 实现范围

- 全部 GRIP 资源描述改用 mcue；目录和 SDK 使用 mcue 过滤，资源级 kind 没有兼容别名。消息外层 kind 保留。
- 发布、发现、读取不可变 MCUE 定义；内置 computing/vm/v1 和九种平台类型定义。
- 资源注册允许没有来源包；公开属性根据所引用类型的 Schema 校验。
- update-attributes 支持递归对象合并、数组替换、null 删除、修订冲突及同类型的版本迁移；不创建 Run。
- 数据库事务保护登记、状态和属性写入。独立 attributes_revision 列持久化，不把旧内存快照写回数据库。
- 编排上下文和 resource_effects 使用 mcue_ref；suanlicloud-http 模板升级为 v3。
- 历史描述符仍可校验摘要后读取，新发布和执行预检使用当前契约。
- SDK 升级为 0.5.0，skill 仓库内容、本机安装链接及生成的分发包一致。
- 原协议整理文档 `/root/computility_named_resource/grip协议整理v2.md` 同步资源字段及示例。

## 验证结果

| 检查 | 结果 |
|---|---|
| 非 integration/e2e 全仓回归 | 初轮 627 通过、6 跳过，1 个旧测试遗漏必填 mcue_ref；补齐后该用例及两个相关发布/打包用例复测，3 项全部通过 |
| MCUE API、SQLite 持久化及 GRIP 执行链路 | 最终相关集合 27 项通过 |
| PostgreSQL 迁移、回滚及并发升级 | 初轮 5 项通过；新增 MCUE 用例的测试 SQL 参数类型修正后，与 MCUE/编排补充用例一起复测，14 项全部通过 |
| 历史描述符公开读取、无来源包 VM 绑定执行 | 最后补充验证 2 项通过 |
| 既有编排创建、认证、使用、清理链路 | 与 MCUE 和元数据测试合计 46 项通过 |
| 能力包生成工件 | generate_service_contracts.py --check 通过 |
| 服务端与 SDK wheel | 全仓及定向测试验证可构建、可安装，服务端 wheel 包含内置定义和迁移文件 |
| Skill | quick_validate 通过；压缩包说明与源码逐文件一致，内含 SDK 0.5.0 wheel |
| 文档及代码基本检查 | JSON 示例解析、skill 引用、compileall、git diff --check 通过 |

以上测试集合有重叠，数量不相加。全仓跳过项是已停用的旧 planner 测试；没有遗留本次变更导致的失败。
测试使用本地隔离数据及临时 PostgreSQL 容器，没有清空或改写生产数据库。

覆盖的行为包括：

- 同一属性修订并发修改只有一个成功，另一个得到 attributes_revision_conflict。
- 状态更新与属性更新不会互相覆盖，重启后定义、属性和修订号仍存在。
- MCUE 显式声明的 host/user/port 可以公开；未声明的字段被对应 Schema 拒绝。
- 更新属性不会公开 instance_config、改变连接配置或激活 revision，也不破坏 exec 调用。
- 不同于 VM 的自定义类型可直接登记和更新，局部 `$defs` 引用可校验，远程 Schema 引用被拒绝。
- 属性无变化不递增修订；JSON boolean 与 number 正确区分。
- 旧资源回填清单不完整时整体失败，完整回填保留 URI、来源包和状态；重复迁移不覆盖后来编辑的属性。
- 不可变类型版本拒绝覆盖，失败的版本切换不产生部分更新。

## 升级产物

- [迁移步骤](../../mcue-upgrade.md)
- `scripts/migrate_resource_mcue.py`
- `migrations/alembic/versions/006_mcue_attributes.py`
- `dist/personal-computing-space.zip`，含 `loom_orchestration-0.5.0-py3-none-any.whl`

生产升级需使用显式资源 URI 清单执行回填，再发布、激活新版算力云包；资源 URI 和已有 SSH 激活绑定继续保留。
