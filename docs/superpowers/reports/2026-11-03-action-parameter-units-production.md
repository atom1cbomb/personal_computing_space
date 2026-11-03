# 能力包参数单位说明生产部署

2026-11-03，按用户要求修复并发布 11 个能力包的参数、结果和配置单位说明，迁移 19 个选定的现有实例。最终 18 个实例为 `ready`，全部通过实际业务调用；`suanli-vm-ssh-http` 的 `npc-test` 保留部署前的 `pending_auth` 状态。

## 修改与版本

Schema `description` 和每个包的 `HOW_TO_USE.md` 补充容量、时长、时间戳、CPU/GPU 数量、比例、百分比、GPU 小时、像素、帧数、采样率、码率、token 数、分页、索引、随机种子及无量纲系数的含义。原生透传对象在对象描述和使用说明中解释单位；不将未声明的上游字段改造成新的校验约束。

算礼云 `Mem`、`DiskSize` 按上游接口核实为字节（B）：4 GiB 内存填写 `4294967296`，20 GiB 磁盘填写 `21474836480`。明确区分十进制 GB 与二进制 GiB，以及秒与毫秒、0–1 比例与百分比。

| 能力包 | 最新已发布版本 → 本次版本 | 本次迁移实例数 |
| --- | --- | ---: |
| ark-http | v2 → v3 | 1 |
| autopaper-http | v4 → v5 | 1 |
| embedding-http | v2 → v3 | 1 |
| grafana-http | v6 → v7 | 3 |
| kubernetes-http | v9 → v10 | 4 |
| outline-http | v4 → v5 | 1 |
| suanli-overleaf-http | v1 → v2 | 1 |
| suanli-vm-ssh-http | v7 → v8 | 2 |
| suanlicloud-http | v8 → v9 | 1 |
| vm-ssh-http | v6 → v7 | 1 |
| volcano-http | v2 → v3 | 3 |

Embedding 原运行实例为 v1，本次直接迁移至 v3。其他历史版本和测试实例按原状保留。

## 部署方式

使用生产 Observer 提供并校验摘要的独立 `loom-orchestration 0.14.0` SDK，经公开内容、包发布及 GRIP 接口发布、检查、停止、激活和配置实例。11 个包均复用原生产镜像的不可变摘要及执行配置，输入／输出／实例配置的校验规则保持一致。

迁移保留实例名、业务配置、凭据配置档和资源绑定。对需要持久状态的 12 个实例，在停止旧实例后备份并复制状态卷，再启动新版本；旧卷和备份保留。两个 VM 的云管理引用、Embedding 模型和网络服务的 Kubernetes 引用，以及 Embedding 的依赖授权均已更新并读回确认。

为保留 Embedding 的 HTTP 入口，给 `slave-autopaper` 增加新版包的消费者端口映射，并用原镜像重建该 Slave 服务。新实例继续绑定 `127.0.0.1:18001`，旧版本端口配置条目保留供回退使用。Observer、Pool、Driver 无需更新镜像；无数据库迁移。

发布时处理了两处元数据兼容问题：

- 算礼云旧 action descriptor 与新 IoContract 组合导致资源效果解析歧义。为四个相关 descriptor 补充单位说明，并重新生成资源效果引用，使新版包引用自己的准确 action。
- Volcano 旧配置 Schema 的相同内容曾以另一种媒体类型发布。补充配置描述后生成新的内容地址，消除发布冲突。

Embedding 切换初期的业务探测曾返回 `internal_error`。确认 Kubernetes 服务通道处于 `ready` 后，后续 GRIP 与 HTTP 探测均成功；未为此修改服务代码。运行态 `ready` 本身不作为业务验收通过的依据。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 新版包和使用说明 | 11 个包发布成功；公开 `how-to-use` 内容与仓库逐字一致 |
| 线上 Schema | 78 个 action 对应的 165 份输入／输出／配置 Schema 与本地 JSON 一致；112 处显式数值字段均有说明 |
| 已有包完整性 | 部署前采集的 17 个相关已发布包摘要全部保持一致 |
| 实例状态 | 19 个旧实例停止；19 个新版实例期望状态为运行，18 个就绪、1 个原有待认证 |
| 就绪实例业务调用 | 云区域、视频任务、文档集合、Kubernetes Pod、Grafana 数据源、Overleaf 项目、Volcano 队列、AutoPaper 会议目录及 SSH 命令读取均通过 |
| Embedding GRIP | 模型信息与一条文本的向量生成通过，返回 2560 维有限数值向量 |
| Embedding HTTP | 原端口上的 `/health`、`/info`、`/embed-one` 均通过 |
| 认证状态 | 12 份状态卷备份存在且大小与备份回执一致 |
| 独立测试实例 | 算礼云和 Volcano 的只读测试通过，两个测试实例已停止 |
| 平台状态 | 3 个 Slave 的公开租约均有效；逻辑 Driver 可用并接受提交 |
| 任务记录 | 本次使用直接调用，前后资源目录中没有新增 Run |

本地此前的相关回归为 **482 passed、16 skipped**；发布过程中针对契约、资源名称和 Volcano 的补充回归分别为 **55 passed**、**60 passed**。最终契约生成器 `--check` 和 `git diff --check` 通过。生产检查没有创建 VM、提交计算作业或触发论文抓取。

`npc-test` 在部署前就需要认证，本次未执行登录，也未将其计入业务调用通过的 18 个实例。

## 后续认证恢复与状态轮询修复

同日用户完成 `npc-test` 的手动认证后，公开状态已成为 `ready` / `authenticated`，实际 SSH 命令返回成功。登录助手曾使用 Chromium 145 打开由 Chromium 156 创建的档案并发生崩溃，现已指定匹配的浏览器，原登录档案保留。

针对登录完成后仍等待约 30 秒的问题，SDK 0.14.1 将认证轮询改为 `describe(..., refresh=True)`，每轮读取服务端最新状态。普通资源查询的缓存保留。回归使用真实 SDK 缓存和模拟 Observer，覆盖缓存有效期内的认证成功、认证失败和 activation revision 变化；这三类场景在修复前均失败、修复后均通过。认证、自动登录、真实 Chromium SSO 和 GRIP binding 共 65 项相关用例最终通过。

当前登录助手已安装本地构建的 SDK 0.14.1 及其声明的 Playwright 1.64.0，重启后直接识别已有认证。预置一份仍有 30 秒有效期的本地 `pending_auth` 缓存，再向生产 Observer 验证，约 0.18 秒即读到 `ready`；这是缓存绕过检查的耗时，不代表完整交互式登录耗时。随后 SSH 调用再次通过。

本次更新源码和当前登录助手，并构建新版 wheel / skill 交付物；Observer 的公开下载仍为此前的 SDK 0.14.0，未为客户端修复更换服务端镜像。新 wheel 以当前仓库源码构建，因此也包含仓库已有的远端错误消息透传、资源注册／删除后缓存失效改进；本次代码修改只涉及认证轮询。修复的构建产物、升级前回执及验收证据保存在私有发布目录的 `auth-cache-fix/` 下。

## 证据与回退

私有脚本、生产 SDK、发布回执、前后资源快照和验收结果位于本地 `production/releases/2026-11-03-action-parameter-units/`。核心结果文件为 `published.json`、`migration.json`、`verification.json`、`references-verified.json` 和 `hosts-after.json`。

生产主机备份目录为 `/opt/loom-production/releases/action-parameter-units-20261103/`，状态归档位于其 `backup/state/` 下。凭据、认证状态和完整私有响应不提交到仓库。

如需回退，使用 `migration.json` 的旧／新实例映射，通过公开 GRIP 停止对应新版实例，再使用保存的旧包 URI、配置和依赖恢复旧实例及原有资源引用。旧包和旧状态卷仍保留；先评估切换后是否产生需要保留的新状态。Embedding 恢复旧实例前必须先释放同一 HTTP 端口。此次部署没有需要回滚的数据库结构变更。
