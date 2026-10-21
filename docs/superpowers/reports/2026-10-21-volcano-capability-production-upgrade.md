# Volcano 能力包生产接入

日期：2026-10-21。生产主机：`9.0.16.66`，Observer：`http://9.0.16.66:8080`。
发布目录：`/opt/loom-production/releases/volcano-capability-20261021-132453`。
对应 [设计](../specs/2026-10-21-volcano-capability-design.md) 和
[本地验证](2026-10-21-volcano-capability-validation.md)。

## 交付

最终包：`grip://loom.local/workspaces/workspace-default/packages/volcano-http/v2`。
Observer 返回的 package digest：
`f6c954663ce04ac2f2215344c1c6149e6025850bd5cd4fb882194a4b83a7103a`。
镜像：`localhost:5000/loom/volcano-gateway@sha256:1125ed67eef738ff335e573f3e21dd8e80254e18730a85f163be71c8bf674924`。

新增六个只读 action：namespaces_list、queues_list、podgroups_list、pods_list、jobs_list、
gpu_usage。所有 action 均为 SafeRead，列表默认采用 Dashboard 的 All namespace 视图，
验证上游分页完整性；GPU 统计区分已分配量、Running 分配量和未调度申请量。

通过 Observer 的公开 GRIP 接口发布 MCUE `computing/volcano/v1`，注册三个具名资源，
以 target 将三个激活绑定到资源；激活位置均为 slave-a，最终均 ready、revision 1。

所有 URI 共用前缀 `grip://loom.local/workspaces/workspace-default/`：

| Volcano 资源 | 激活 | 关联 Kubernetes |
|---|---|---|
| resources/volcano/beijing | activations/volcano-http/v2/slave-a/beijing | activations/kubernetes-http/v4/slave-a/beijing |
| resources/volcano/nanjing | activations/volcano-http/v2/slave-a/nanjing | activations/kubernetes-http/v4/slave-a/nanjing |
| resources/volcano/beijing-ict | activations/volcano-http/v2/slave-a/beijing-ict | activations/kubernetes-http/v4/slave-a/beijing-ict |

DESCRIBE 资源通过 `metadata.attributes.dashboard_url` 公开浏览器地址，
`metadata.attributes.kubernetes_ref` 指向上述确切 Kubernetes URI；`metadata.activations`
返回唯一的 ready v2 实例，资源本身提供该实例的六个业务 action offer。
最初注册记录的 metadata.package 保留 v1 作为不可变注册来源；当前执行选择以 ready
激活及实际 offer 为准。原型的三个 v1 激活已通过 Observer 停用。

## 公开地址与生产连接配置

首次 v1 接入直接使用了公开地址，南京三项查询成功，北京和北京 ICT 的六项查询失败。
生产网络健康检查确认：北京公开端口在生产解析结果上发生 TLS SNI 错误，北京 ICT
公开端口返回 404。工作区域名解析到公网入口，生产解析到地区内网 ingress；两者的
HTTPS 端口不同。在生产使用 443 均可通过 TLS 验证并获得 Volcano Dashboard App。

为此将 v2 的私有实例配置改为 api_origin，与 MCUE 的公开 dashboard_url 分离。
地址由实例显式配置，网关不自动改写端口、不关闭 TLS 验证。

| 实例 | 公开 dashboard_url | 实例 api_origin |
|---|---|---|
| beijing | https://volcano-prod.bj.cs.ac.cn:7443 | https://volcano-prod.bj.cs.ac.cn |
| nanjing | https://volcano-prod.nj.cs.ac.cn:10062 | https://volcano-prod.nj.cs.ac.cn:10062 |
| beijing-ict | https://volcano-prod.ictbj.cs.ac.cn:10062 | https://volcano-prod.ictbj.cs.ac.cn |

三个容器的 instance.json 挂载均为只读，实际 API 入口与各自配置一致。
这些只读 JSON API 当前无需浏览器会话，未挂载额外凭据。HTTP 401/403、重定向、
无效 JSON 或不完整列表均返回错误，不将登录失败解释为零资源。

## Runtime 升级与保留验证

移除 container-http-v1 内对 SSH host/user/port 的专用配置要求，改为传递服务自有 JSON。
只替换生产 Slave 绑定挂载的 runtime.py 并重启 Slave 控制进程。
新插件 SHA-256：`3c9bc1ed2302871e9b59b742ed24961e8aa44eb5363a6fe270d96c4269f85cdd`。
备份文件为发布目录 `backup/container-http-runtime.py`。

确认原有七个业务容器的 ID 与运行状态保持；通过 Observer 验证原有十五个 VM/激活
的状态、revision、公开属性及绑定保持，包括 autopaper-vm 的描述。
Observer 健康，Driver execution/orchestration 正常。数据库 schema 未变化。

## 源码与分发

工作区基准提交：`117a13fc019ce99efcaec7c2f3848b2fd75ad721`。实际源码取自工作区快照。
最终 `source-v2/` 共 554 个文件，归档名 `volcano-capability-20261021-132453-source-v2.tar.gz`，
SHA-256：`15e040206ea613436962032b15c5943cad0e247c2228c691b0caa6e2efbd4d60`。
初始 v1 源码与镜像保留用于审计，未覆盖已发布包。

Skill 分发包包含更新的 Volcano 指引及 SDK 0.6.0 wheel，ZIP SHA-256：
`7a89acab523d328104d34321f2f59501d25e03e936ff0559d75081bc7975a7b1`。
部署报告和最终验证记录另存于发布目录，不属于源码归档校验范围。

## 真实 action 验证

三个实例分别通过 Observer 调用 queues_list、podgroups_list、gpu_usage，共九个 Run。
GPU action 直接以稳定的 Volcano 资源 URI 为 target。最终全部 completed，
结果 status=ok、complete=true；下面的数据为采集时快照。

| 实例 | 队列数 | PodGroup 数 | 可见 Pod 数 | 已分配 GPU | Running GPU | 未调度申请 | GPU 验证 Run |
|---|---:|---:|---:|---:|---:|---:|---|
| beijing | 2 | 1 | 288 | 1 | 1 | 0 | run-a00e3e21046d |
| nanjing | 2 | 2 | 481 | 2 | 2 | 0 | run-cbeb35207afb |
| beijing-ict | 2 | 1 | 312 | 1 | 1 | 0 | run-a074bd983044 |

| 实例 | queues_list Run | podgroups_list Run |
|---|---|---|
| beijing | run-2901abf1c9ed | run-7261b5de35ad |
| nanjing | run-7378994689f2 | run-6e78ac2077a7 |
| beijing-ict | run-1b2acf46d5d1 | run-e1f6a7deb41a |

Run URI 的统一前缀为 `grip://loom.local/workspaces/workspace-default/runs/`。

GPU 数字是 `metric=scheduled_pod_requests`、resource_name=nvidia.com/gpu 的资源单位，
排除已结束 Pod，已绑定节点与未调度请求分开；不表示物理卡容量或实时计算利用率。

## 回退

若回退到旧 runtime，先通过 Observer 停止 Volcano 激活，再恢复备份 runtime.py 并
重启 Slave 控制进程。数据库不需要降级；新增的不可变包和 MCUE 可保留作为历史记录。
原有业务容器不因恢复插件文件而重新创建。
