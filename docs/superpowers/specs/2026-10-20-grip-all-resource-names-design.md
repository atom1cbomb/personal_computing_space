# 所有资源统一使用可读 GRIP 名称

日期：2026-10-20。状态：已实现并验证。遵循 `../spec-design-guidance.md`。本设计替代上一版“仅公开入口使用 GRIP”的限制，不保留旧 URI 兼容层。

> 2026-10-20 更新：业务术语、Run 回执与完整实例描述以 [action 和激活描述设计](2026-10-20-grip-actions-and-activation-description-design.md) 为准。
## 目标与名称

ResourceRef、descriptor 自身标识、包版本、Slave、激活实例、执行器、运行时和内容引用均使用 `grip://authority/path`。底层 HTTPS 访问地址、OCI 镜像地址、数据库连接串属于 binding 或存储配置，保留各自协议。

资源统一位于 `grip://<authority>/workspaces/<workspace>/`。示例：

| 对象 | 相对路径 |
| --- | --- |
| action descriptor | `actions/suanlicloud/instances-list` |
| 不可变能力包版本 | `packages/suanlicloud-http/v2` |
| Slave | `slaves/slave-a` |
| 已激活能力包 | `activations/suanlicloud-http/v2/slave-a/default` |
| 业务资源 | `resources/cloud-vm/vm-123` |
| 内容资源 | `content/suanlicloud-http/schemas/instances-list-output/sha256/<digest>` |
| 包导出动作 | `actions/exports/suanlicloud-http/v2/<action>/sha256/<descriptor-digest>` |

内容名称必须包含用途或业务名称，哈希只出现在末尾的不可变版本部分。相同字节可以作为不同业务用途的内容资源，存储仍按哈希去重；完整 URI 才是资源身份。包坐标由已有的版本不可变约束保护，包摘要继续显式验证。激活 revision 控制实例并发变更，不编码为新 URI。禁止把整个旧 URI 百分号编码后当作新名称。

## 实现

SDK 提供共享的命名、内容摘要解析和 URI 校验。服务端和 SDK 的 ResourceRef 使用同一个校验规则，拒绝旧协议及裸标识。裸 Slave ID 只用于内部调度键，进入 ResourceRef 时必须显式构造完整 GRIP 名称；调度时按完整命名空间解析，不靠后缀猜测。

内容上传接受可读 name，未提供时以内容类型命名。ContentStore 保留按摘要存储的原始字节，同时持久化名称绑定，确保重启后仍能通过完整名称读取，不能凭更改可读名称冒充已发布资源。内容名称携带当前 authority/workspace。Observer 为内容提供 DESCRIBE/read，read 输出统一封装为 `{content, media_type}`，以支持 JSON 数组、标量和文本。SDK 的 read_json 使用该入口。

包发布器根据源目录和文件名命名 Schema、IoContract、validator、descriptor，按依赖顺序上传并替换引用。模板采用默认命名空间，发布到其他命名空间时重新构造引用和摘要。生成器使用相同命名规则生成可审核的静态契约。5 个 capability_services 包、样例、脚本、SDK 文档和测试同步迁移。

目录直接使用内部资源的 GRIP 身份，不再增加第二层包装。包、激活、动作的名称以业务名优先，消除纯哈希动作名和编码后的嵌套 URI。

## 验收

1. 全仓扫描：生产代码、能力包、测试、示例和当前文档没有旧协议资源引用；历史说明明确标记，负向测试可使用旧名称验证拒绝。
2. 5 个服务包的全部导出均具有可读 GRIP Schema/IoContract/descriptor 引用；生成器 `--check` 通过。
3. 内容命名、校验、重启读取、伪造名称、不同 workspace、descriptor 双摘要有测试覆盖。
4. SDK 发布、依赖包、GRIP 描述/读取/激活/执行、清理和调度测试通过；运行全套仓库测试。

验证结果：[574 passed、7 skipped 及全仓扫描记录](../reports/2026-10-20-grip-all-resource-names-validation.md)。
