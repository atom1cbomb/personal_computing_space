# 激活凭据配置与三个 Kubernetes 实例生产部署

日期：2026-10-21。部署主机：`9.0.16.66`，Observer：`http://9.0.16.66:8080`。
发布目录：`/opt/loom-production/releases/activation-credentials-20261021-111042`。

## 核心服务

Observer、Driver、slave-a 和 HTTP runtime 已部署实例级 `credential_profile` 实现。
两个角色数据库均从 `006_mcue_attributes` 升级至 `007_activation_credentials`。
GRIP 的 `activate-package` 输入 schema 已包含独立凭据配置字段。

源码取自当前工作区，基准提交为 `117a13fc019ce99efcaec7c2f3848b2fd75ad721`，
包含尚未提交的 SSH 限制、MCUE 和实例凭据修改；此次部署没有提交或推送 Git。
归档共 518 个文件，SHA-256：
`5a4ff151a1cafa1badd9184b4ab31b4c650a4b9323bda656ed5ef9da0734f6a1`。
对应 SDK 0.6.0 skill 分发包已保存在发布目录。

升级前确认 15 个历史 Run 均已终结。核心服务停机期间生成并验证 Observer/Slave 的
PostgreSQL dump，备份原角色配置、源码、凭据及运行时实例文件，并保留旧核心镜像标签。
备份位于发布目录的 `backup/`，仅 root 可访问。初次备份校验使用了错误的版本表名，
在任何迁移前修正为角色专用版本表后继续；最终两个数据库迁移及校验均成功。

升级后通过 Observer 验证：15 个历史 Run 的公开读取结果完全一致，9 个既有 VM/激活的
身份、状态和 revision 保持不变。数据库包 JSON 和 Run outcome 校验值不变，
原有 6 个凭据文件内容及 4 个业务容器 ID 均保留。核心健康检查和 Driver execution /
orchestration 均正常。未清空数据库，也未重建已有 VM。

## Kubernetes 包与实例

最终使用包：`grip://loom.local/workspaces/workspace-default/packages/kubernetes-http/v4`。
Observer 返回的包摘要：`b23c2f2dd4979ddfa919399b55eecbafa26077adde0cbef634bb8eadba45181e`。
镜像：`localhost:5000/loom/kubernetes-gateway@sha256:46da7e5b72b16dd3aa062e7b36e2d4897b2847df384a31819cc61e088ae0fd86`。

按运行中集群的版本，以 `KUBECTL_VERSION=v1.35.1` 构建网关镜像，并验证镜像内版本。
原网关 kubectl 为 v1.31.4；客户端与服务端的版本限制参见
[Kubernetes 版本兼容策略](https://kubernetes.io/releases/version-skew-policy/#kubectl)。
kubelogin 版本及六个 action 契约不变。验收时发现 Volcano 资源名映射问题，
以新的不可变包 v4 修复，未覆盖最初发布的 v3，详情见下文。

三个实例均由公开 Observer GRIP 激活，使用同一包和不同只读凭据目录。用户确认
继续使用已有 operator 配置中的 namespace `dev-yuzishu`：

| 实例名 | credential_profile | kubeconfig context | 验证过的 API 入口 |
|---|---|---|---|
| beijing | k8s-beijing | k8s-bj-prod | https://k8s-prod.bj.cs.ac.cn |
| nanjing | k8s-nanjing | k8s-nj-prod | https://k8s-prod.nj.cs.ac.cn:10062 |
| beijing-ict | k8s-beijing-ict | k8s-ictbj-prod | https://k8s-prod.ictbj.cs.ac.cn |

北京旧 7443 端口发生 TLS SNI 错误，北京 ICT 旧 10062 入口返回 404；默认 HTTPS 端口
均可通过 TLS 验证并返回 Kubernetes v1.35.1。南京的 10062 入口正常。
已同步 operator 文档中这两个 Kubernetes API 地址；未根据 API 结果推断 Volcano UI 地址。

实例 URI 共用前缀：
`grip://loom.local/workspaces/workspace-default/activations/kubernetes-http/v4/slave-a/`，
分别以 `beijing`、`nanjing`、`beijing-ict` 结尾，激活 revision 均为 1。
三个实例均为 `ready`，六个业务 action 均 available；验证挂载目录互不相同且均为只读，
DESCRIBE 不返回 profile 或凭据。

## 登录修复与 Volcano 验收

用户完成三个 OIDC device-code 登录后，访问探针报 ExecCredential 版本不匹配。
已缓存登录凭据，但 kubeconfig 声明 `client.authentication.k8s.io/v1`，镜像中的
kubelogin 1.30.1 返回 `v1beta1`。将三个新 profile 的 exec API 版本修正为 `v1beta1`，
未修改 namespace 或已有旧 profile。此兼容问题见
[kubelogin 上游说明](https://github.com/int128/kubelogin/issues/1049)。
在私有目录备份修正前配置，并同步发布目录中的 staged profile；无需重新登录。
v3 已进入 failed 状态，单独 auth-start 不能使其跨过 Observer 的终态限制；通过公开
activate-package 使用新幂等键重新激活后，原 URI 保留、revision 从 1 增至 2 并恢复 ready。

初次真实查询确认三个集群均能列出 Pod，但 `resources_list(resource="vcjobs")` 失败。
operator 的 API discovery 诊断确认 Volcano 注册的资源为 `jobs.batch.volcano.sh`，缩写为
`vcjob` / `vj`，没有 `vcjobs`。网关现将公开的 `vcjobs` 选择器映射到完整资源名，
原生 Kubernetes `jobs` 的选择保持不变；`resources_list` 与 `resources_get` 均覆盖。
为此发布 v4，未原地修改 v3 的内容或镜像。

新网关源码、Dockerfile、包模板和契约保存在发布目录的 `kubernetes-v4/`；server.py SHA-256：
`09ee58b5aead2b1d9dd0216db24c90754c91758a6ed35a8af97487e79e4f8792`。
该目录是最初核心源码归档之后追加的修复产物。登录缓存仅在同一实例/profile 的旧、新
容器之间迁移，私有备份位于 `backup/kubernetes-login-cache/`；不进入 Observer content。

针对修复运行 Kubernetes gateway 与五个服务包 GRIP 集成测试：**14 passed**，
包括新增的四项原生/Volcano Job 查询回归，以及五个业务包全部 32 个 action 的测试替身验证。
`generate_service_contracts.py --check` 和 `git diff --check` 通过。

最终通过 Observer 调用 v4 的 resources_list，六个只读 Run 全部 `completed`，
gateway status 均为 `ok`，返回项 namespace 均为 `dev-yuzishu`：

| 实例 | Pod 数 | Volcano Job 数 | Pod 验证 Run | Volcano 验证 Run |
|---|---:|---:|---|---|
| beijing | 0 | 0 | run-5e2e376ad8fb | run-605bd27d0d6b |
| nanjing | 8 | 0 | run-571d743e4ea5 | run-a59f1ad60025 |
| beijing-ict | 0 | 0 | run-cd762ee013a2 | run-af29b626614e |

Run URI 共用前缀 `grip://loom.local/workspaces/workspace-default/runs/`。
未向生产集群提交、修改或删除测试工作负载。首次 v3 Volcano 查询留下的三个验证 Run
`run-b4d94503ee2e`、`run-58f9477c9c9a`、`run-71fad6831cf2` 为 `awaiting_decision`，
原因是网关错误结果未满足成功输出 schema；保留原始证据，未自动 accept/abandon。
它们不属于最终六项成功验证。

验收后经 Observer 停用本次创建的三个 v3 实例，均为 `stopped`、revision 3。
最终再次确认三个 v4 实例 ready、revision 1，Observer 健康且 Driver execution /
orchestration 正常。原有业务 VM 和其他能力包未在此次替换中停用。

## 回退

保留的旧镜像标签为 `before-activation-credentials-20261021-111042`。
需要回退时停止写入，恢复同一备份点的数据库、角色源码/配置及运行时配置，再切回旧镜像。
不能仅切换旧镜像而沿用新版实例级凭据数据。新增内容为不可变发布资源，ContentStore 未清空。
