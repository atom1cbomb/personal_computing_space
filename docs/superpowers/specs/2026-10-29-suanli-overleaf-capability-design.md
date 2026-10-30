# Suanli Overleaf 能力包设计

遵循 [spec 设计规范](../spec-design-guidance.md)。

## 使用方式

`suanli_overleaf` 面向 https://overleaf.cs.ac.cn 的账号提供项目管理和源码同步。
一个激活实例管理一个账号可访问的多个项目。用户已选择由激活者提供 Git
authentication token，并通过 Loom 统一登录入口完成网页登录。

外部 Agent 将项目下载到自己的机器，用现有文件编辑、搜索、差异检查和 LaTeX
工具完成工作，再调用能力包同步。能力包负责认证、传输、版本检查和远端写入。
本地路径由外部客户端解释，不能作为远端能力包可访问的文件路径。

五个业务动作、客户端辅助功能和统一认证修正已实现于
`capability_services/suanli_overleaf_http`。2026-10-30 已部署生产并完成真实账号的只增
联调；按用户要求没有测试删除或覆盖已有文件。具体字节流格式与使用方法见该包
`HOW_TO_USE.md`，实测范围见 [生产报告](../reports/2026-10-30-overleaf-production-validation.md)。

## 五个动作

| 动作 | 输入要点 | 输出及语义 | 上游认证 |
|---|---|---|---|
| `projects.list` | 名称过滤、分页 | 真实的自有及参与项目列表，项目 ID、名称、链接；只返回上游可核实的权限字段 | 网页会话 |
| `projects.download` | 项目 ID | 对应同一版本的源码归档、基准版本和文件清单 | Git token |
| `projects.status` | 项目 ID、基准版本 | 最新版本、是否变化；能够取得基准时返回变更摘要 | Git token |
| `projects.upload` | 项目 ID、基准版本、修改归档、明确的删除列表、提交说明、幂等键 | 校验版本后提交并推送，返回新版本和修改摘要 | Git token |
| `projects.import` | 项目名称、完整源码 ZIP、幂等键 | 创建新项目并返回项目 ID、链接和操作结果 | 网页会话 |

`upload` 更新指定项目，`import` 创建新项目。项目名称不是唯一身份，始终使用
上游返回的项目 ID。列表来自上游账号，不能用手工配置或本地缓存项目清单代替。

下载归档和基准版本从同一 Git 快照产生。客户端在项目源码外保存同步记录，包含
激活 URI、项目 ID、基准版本及原始文件清单。根据此清单生成新增、修改和明确删除；
未打包的文件不自动视为删除。同步记录、Git 凭据及本地编译中间产物不进入源码包，
论文图片和作为素材的 PDF 保留。

查询使用现有 request-reply 交互；归档传输使用现有 byte-stream 交互。客户端辅助
功能负责解压、打包和同步记录，并复用公开 SDK；不增加远端文件编辑动作。
字节流采用 `overleaf-archive.v1`：stream-open 只确认接纳；服务端以四字节长度前缀
JSON 报告结果，下载成功后紧跟源码信封 ZIP。上传和导入先精确接收声明长度的 ZIP
并核验 SHA-256，再执行写入和返回终态；不能把断连当作写入成功。

## 认证

Git 使用用户名 `git`，令牌来自激活时选定的只读 credential profile。
令牌不放进包 manifest、普通实例配置、业务请求或 Git remote URL。
网页登录复用 `loom-auth` 的 browser-cookie 流程，站点 Cookie 名为 `overleaf.sid`。
网页会话私有状态按激活隔离持久化，重新认证沿用同一个激活实例。

网页登录成功必须通过受保护的项目列表请求确认。账号没有项目时，合法的空列表
仍可证明网页认证成功；Git token 的可用性需要真实 Git 访问确认，不能仅根据文件
存在推断。网页会话和 Git 凭据分别报告故障，避免通过重新网页登录处理无效 Git token。

新项目创建成功后即记录项目 ID。后续 Git 检查或下载失败不能把创建结果抹掉并
重新导入；基准版本在成功下载时获得。

## 统一登录修正

能力包的 Cookie 名校验允许点号。对于登录前就会设置 Cookie 的站点，新增仅适用于
browser-cookie 的精确回调结果 `{"status":"pending"}`。

1. 网关用候选 Cookie 访问受保护接口；若确认仍未登录，返回 pending，不保存认证凭据。
2. HTTP runtime 只对声明为 browser-cookie 的包转发 pending；不允许携带额外数据。
3. Observer 保留同一浏览器绑定和原始挑战期限，不标记认证成功。
4. SDK 保持原窗口，每秒读取当前 Cookie 并重新验证；验证通过返回 accepted。
5. accepted 消费挑战，SDK 继续等待激活达到 ready/authenticated；后续重放被拒绝。

超时、窗口关闭、失效激活和终止错误仍结束本次尝试。网络故障、权限拒绝和响应格式
错误不能伪装为 pending。redirect 回调仍为一次性回调。采用 pending 的网关需要匹配
更新后的 Observer、HTTP runtime 和 SDK；不把已有网关的任意错误自动转换为 pending。

## 同步冲突

`status` 从远端获取最新 Git 状态后比较基准版本。`upload` 必须独立重新验证基准，
不能依赖此前一次 status 的结果。提交建立在已验证基准之上，并使用普通非强制推送；
查询与推送之间发生的远端更新也应导致冲突。

冲突返回最新版本和可获得的差异，外部 Agent 在本地合并后重新上传。
不自动覆盖网页端改动。推送结果未知时核实远端是否已包含本次提交，再决定结果，
不盲目创建重复提交。Git 桥对网页并发编辑的具体处理需要真实项目验证。

## 导入幂等与结果未知

上游 ZIP 导入实现没有声明幂等键。能力包不能承诺在任意网络故障下自动获得恰好一次
创建；本地账本用于防止重复发送，并保存已确认结果。

同一激活内，幂等键绑定项目名称与归档摘要。重试携带不同内容时返回冲突。
在发送可能创建项目的请求之前持久化发送记录；收到成功响应后立即持久化项目 ID。

| 状态 | 相同幂等键再次调用的行为 |
|---|---|
| 尚未发送 | 校验通过后允许发送一次 |
| 正在发送 | 返回进行中，不并发发送第二次 |
| 已确认成功 | 返回已有项目 ID 和链接 |
| 已确认未创建 | 返回明确失败原因；修正输入后使用新的操作 |
| 结果未知 | 返回 `import_outcome_unknown`，保留记录，不自动重新创建 |

请求超时、连接中断、收到可能伴随部分副作用的服务端错误，或发送记录存在但进程
重启后没有终态，均不能视为“项目未创建”。同名项目也不能作为成功的充分证据。
无法取得可靠结果时交由用户核查，不能仅更换幂等键继续自动导入。

## 验证

统一认证回归覆盖：点号 Cookie 名、匿名 Cookie 后成功、原窗口保持打开、pending
不延长期限、错误绑定、终止错误、过期、accepted 后重放，以及 redirect 拒绝 pending。

自动化验证使用上游 HTTP 替身、真实本地 Git 仓库及完整 GRIP HTTP/WS 链路，覆盖
账号项目列表、文本和二进制源码往返、Git 并发修改冲突、
明确删除、空账号导入、同键重试、成功后响应丢失、服务重启后的未知结果，以及
创建成功后 Git 验证失败仍保留项目 ID。

## 依据与待验证项

- [Git 集成文档](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration)：Git 负责源码同步；网页修改按需生成提交，Git 回写可能影响批注和修订。
- [Git token 文档](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration/git-integration-authentication-tokens)：用户名为 git，同一令牌可访问账号有权限的多个项目。
- [上游路由](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/router.mjs)及[ZIP 导入实现](https://github.com/overleaf/overleaf/blob/main/services/web/app/src/Features/Uploads/ProjectUploadController.mjs)：项目列表与新项目导入使用网页会话；创建返回项目 ID。
- 2026-10-29 未认证请求确认本站 `/user/projects` 返回 401 和 `OverleafLogin`，登录页设置 `overleaf.sid`。这不等于已验证本站所有上游接口或令牌权限。

2026-10-30 联调确认了本站的项目列表格式、CSRF 获取方式、ZIP 导入请求格式、Git URL
和 `main` 默认分支，五个动作的只增流程均通过。网页即时并发、删除和覆盖已有文件
仍不属于本次真实测试范围。
