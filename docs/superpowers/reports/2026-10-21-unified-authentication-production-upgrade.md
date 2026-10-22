# 统一认证生产升级

Grafana 随后于 2026-10-22 升级到 v4，修复正常令牌轮换被误判为登录失效的问题；
当前部署见 [会话轮换修复验收](2026-10-22-grafana-session-rotation-production-upgrade.md)。

生产：`9.0.16.66`，Observer：`http://9.0.16.66:8080`。
对应 [设计](../specs/2026-10-21-unified-authentication-design.md)、
[回归验证](2026-10-21-unified-authentication-validation.md) 和
[认证使用说明](../../authentication.md)。

## 部署与备份

发布目录：`/opt/loom-production/releases/unified-authentication-20261021-203445`。
部署包含工作区尚未提交的统一认证修改，基准提交为
`3c6990674ffe40bba797e95749cae7ca9a2a1b0a`；以源码归档而非该提交单独标识本次产物。
597 个文件的源码归档 SHA-256：
`d06b0054e65cee20f45d02bfa853568b6790a8f80331d33451d6e13bef641f25`。
归档排除了 production 私有配置、密钥、审计记录和本地虚拟环境。

Observer、Slave、Driver 已重建并切换；Slave 的 HTTP runtime 同步更新。
Observer/Slave 镜像 ID：
`sha256:2f0310e4062601cda1e9973d59921b59e693ba8daa25605262d20d2871f340df`。
Driver 镜像 ID：
`sha256:e018e475b52629bfe4f13f778823c5f273abbc42678b4063f33e7fac08d11a5c`。
运行中的 HTTP runtime 源码 SHA-256：
`ccf5f06e2e63de6efce79cf75ef9b96bc73bf26f8b74563ac8a907eeed0687b8`。

切换前备份两个 PostgreSQL 数据库，并通过 pg_restore --list 验证备份可读取；
同时保存旧核心镜像标签、源代码、部署配置、原有凭据和实例私有认证状态。
备份位于发布目录的 `backup/`，仅操作员可读。
数据库未清空，迁移版本保持 `007_activation_credentials`；核心切换后核对历史包
和 Run outcome 未变、凭据文件哈希未变，13 个原有业务容器仍在运行。后续复核确认
15 个旧包、50 个旧 Run outcome、12 个原凭据文件均保留原始摘要。
三个 Kubernetes 的认证缓存通过容器内 tar 从 tmpfs 导出、按同一实例迁移，
没有经过 GRIP 内容存储或业务 Run。

切换后 `/healthz` 成功，`/api/v1/runtime` 的 execution、orchestration 均为 true，
Driver epoch 为 164。8 个已注册 VM/Volcano/Grafana 资源的 URI、MCUE 和原有属性保留。

## 发布产物

统一登录 SDK 为 0.7.0，入口 `loom-auth` / `python -m loom_orchestration.auth`。
技能归档 `dist/personal-computing-space.zip` 已同步到生产发布目录，SHA-256：
`e580a03642dc7b5a76d038064cd46d448b9c44280da8d7d2e09c7d79da77b511`。
本地已安装该归档内的 SDK wheel，Chromium 可见窗口启动检查通过。

新增不可变包：grafana-http/v3、kubernetes-http/v5、suanli-vm-ssh-http/v4、
outline-http/v3、suanlicloud-http/v5、vm-ssh-http/v5。
历史包保持原始内容。经公开 descriptor API 验证，算力云创建 VM 的资源效果引用
已发布的 suanli-vm-ssh-http/v4 及 Observer 返回的对应摘要。

Kubernetes 新镜像：
`localhost:5000/loom/kubernetes-gateway@sha256:cdbfc98f91c171d83ecaf5e9e0b02b8ffb7db0cbee9741966ac041f635ab33c5`。
保留生产 kubectl v1.35.1；镜像中的 server.py 哈希与部署源码一致。
算力云 SSH 新镜像：
`localhost:5000/loom/suanli-vm-ssh-gateway@sha256:fdf4431dbb1c5862bb52969787076382f3c80c701efd0d7d7c166e0b5a24a98d`。
Grafana、普通 SSH、Outline 和算力云 API 的网关代码未变，逐文件核验生产镜像与
源码一致后复用原不可变镜像，发布新的认证声明包。

## 实例验证

通过 Observer 发布、激活、auth-start、业务 action 和 stop 完成迁移；SSH/Docker
仅用于部署和对应实例私有状态转移。Outline、算力云 API、普通 SSH 的新激活已
使用各自原 profile，通过实际只读调用后停用旧激活。普通 SSH 命令退出码为 0，
回显 `loom-unified-auth-ok`，未修改 VM 应用或文件。

Kubernetes 初次 v5 验证暴露周期重检状态抖动：生产连续观察到有效凭据的北京实例
短暂变为 pending_auth。已修复并追加发布 v6，保留 v5 不可变历史；不会修改已发布
v5 的摘要。新行为和 50 项相关测试见验证记录的“生产重检补充修复”。

v6 补充源码快照为 `source-v6.tar.gz`，598 个文件，SHA-256：
`76e93cd0d6c9c697419b407ec5b7e0cb3ab88bc8543deb2aed7206e897a3da72`。
当前技能 ZIP 同步更新 K8s v6 指引，SHA-256：
`b719b3edf271b007c3d5f0ce65def2dcab0ba32ae7a23d960956311476b7d157`。
初始技能 ZIP 另存为 `personal-computing-space-initial.zip`。

Kubernetes v6 镜像：
`localhost:5000/loom/kubernetes-gateway@sha256:72a78ae3f90e66901067ce763d29e4bfe064a1f6241720db60dcfc285b02b319`。
镜像 server.py 与源码一致，SHA-256：
`edb55ddff9cef724f3d182965ede814bafe5073483e83cd75974115d63a7003f`。
北京 v6 在 75.5 秒内完成 12 次公开 DESCRIBE 采样，始终 ready/authenticated，
覆盖两个周期检查窗口，没有再观察到无登录挑战的状态抖动。

## 最终验证

用户已通过同一本地 `http://localhost:3000/` 入口完成四个实例登录，客户端均显示
completed；公开 DESCRIBE 确认同一新激活 URI/revision 达到 ready/authenticated。
随后通过 Observer action 验证算力云 SSH 命令成功、三个 Grafana 的受保护数据源
读取成功。没有直接访问 Slave/Driver 业务接口，没有把浏览器凭据放入业务输入。

当前 13 个运行中的业务激活全部 ready，运行容器数为 13；被替换的旧激活均已
stopped。两个 VM 的 URI 和原有属性保留，autopaper-vm 显示名称与用途描述保留。
三个 Volcano 和三个 Grafana 的 kubernetes_ref 已分别更新为对应的 K8s v6 激活；
其余属性保持不变。Volcano 的实际 GPU 分配查询也已通过。

| 能力包 | 当前实例 | 验证 |
|---|---|---|
| kubernetes-http/v6 | beijing、nanjing、beijing-ict | ready；各实例读取 pods 成功 |
| grafana-http/v3 | bj、nj、ict-bj | ready；分别扫码认证，数据源读取成功 |
| suanli-vm-ssh-http/v4 | loom-v2-auth-3c474f5bb5 | ready；扫码重认证，SSH 命令成功 |
| vm-ssh-http/v5 | vm-9-0-16-113 | ready；原凭据、SSH 命令成功 |
| suanlicloud-http/v5 | default | ready；实例列表读取成功 |
| outline-http/v3 | default | ready；collection 列表读取成功 |
| volcano-http/v2 | beijing、nanjing、beijing-ict | 保持 ready；北京 GPU 分配查询成功 |

本地登录服务保持运行，可继续从原页面重新认证，不需要重建激活或更换 URI。
初次 v5 与更早的包、旧激活和 Run 均作为历史保留；当前 K8s 使用 v6。

最终认证验证时间：`2026-10-21T13:08:44.391734+00:00`；公开 runtime 返回 execution=true、orchestration=true，Driver epoch=167。

业务验证 Run：

- `outline-http/default`：`run-02ad9cd73db2`。
- `suanlicloud-http/default`：`run-591649d10f7e`。
- `vm-ssh-http/vm-9-0-16-113`：`run-5d5c9fded5be`。
- `kubernetes-http/beijing`：`run-488fe50b7f0c`。
- `kubernetes-http/nanjing`：`run-b63b20c8e917`。
- `kubernetes-http/beijing-ict`：`run-d63a3a10e16c`。
- `suanli-vm-ssh-http/loom-v2-auth-3c474f5bb5`：`run-f93587dd35c6`。
- `grafana-http/bj`：`run-e8ec5ba59739`。
- `grafana-http/ict-bj`：`run-d878d9077c8b`。
- `grafana-http/nj`：`run-9d2e03b19d8f`。

## 北京真实 GPU 监控验收

只通过 Observer 的 grafana-http/v3 action 完成 datasources-list、dashboards-list、
dashboard-get 和 gpu-utilization。实际仪表盘 `Oxed_c6Wz` 的面板 6 定义 GPU Utilization，
查询 `DCGM_FI_DEV_GPU_UTIL`，单位 percent，节点标签 Hostname、编号标签 gpu。
仪表盘数据源变量当前为 Prometheus，UID 为 `prometheus`；实际返回样本另验证 UUID 标签。
这些映射来自真实仪表盘和样本，不是预设的指标名称猜测。

最终 Run：`run-3b0139235536`，status=ok、complete=true、无 warnings。

| 采样时间（北京时间） | 节点 | GPU 编号 | 型号 | 利用率 | 工作负载标签 |
|---|---|---|---|---|---|
| 2026-10-21T21:11:11.482000+08:00 | vm-9-0-9-8-ubuntu | 0 | NVIDIA A800 80GB PCIe | 57% | dev-sunyixuan/worker1 |

GPU UUID：`GPU-3c5ca57d-1fd8-e74a-a325-b117d3202d9e`。工作负载对应 exported_namespace、exported_pod
和 exported_container=main；普通 namespace/pod 标签属于 GPU exporter 本身。
该值是所示时刻的 GPU busy 指标百分比，不是 FLOPS 达成率。

构建归档保留构建时快照；最终报告和私有验证记录另存于生产发布目录。
