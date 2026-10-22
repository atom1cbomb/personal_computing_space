# Grafana 监控能力包与 GPU 实际利用率

认证客户端与重试行为已由 [统一认证设计](2026-10-21-unified-authentication-design.md) 更新；下文保留原设计依据。

日期：2026-10-21。

## 目标与现状

新增独立的 `grafana-http/v2` 服务包，在 slave-a 激活 `nj`、`bj`、`ict-bj`，
通过 Observer 的 GRIP action 查询 Grafana 仪表盘及其 Prometheus 数据源。
验收包含查询北京节点 `vm-9-0-9-8-ubuntu` 的各 GPU 编号和实际利用率。
设计遵循 [spec 规范](../spec-design-guidance.md)，复用 HTTP runtime、激活级凭据、
MCUE 和目标绑定，不引入新的执行层或数据库表。

三个公开入口的 `/api/health` 可访问；`/api/datasources`、`/api/search` 和
`/api/frontend/settings` 实测均返回 401。Grafana 使用 connect.cs.ac.cn 的
generic OAuth 登录，进一步实测跳转到 `open.cs.ac.cn/connect/login` 的扫码页面。
浏览器会话与 Volcano/Kubernetes 认证独立。默认使用本地受控浏览器完成原站扫码，
通过既有 Observer 一次性认证桥将该浏览器自己的会话交给选定激活，网关验证后
持久化。非交互部署可选择操作员提供的只读服务账号 token 或既有 Grafana 会话。

| 激活实例 | 公开 Grafana 入口 | 关联 Kubernetes 激活实例 | 关联 Volcano 资源 |
|---|---|---|---|
| nj | https://grafana-prod.nj.cs.ac.cn:10062 | kubernetes-http/v4/slave-a/nanjing | resources/volcano/nanjing |
| bj | https://grafana-prod.bj.cs.ac.cn:7443 | kubernetes-http/v4/slave-a/beijing | resources/volcano/beijing |
| ict-bj | https://grafana-prod.ictbj.cs.ac.cn:10062 | kubernetes-http/v4/slave-a/beijing-ict | resources/volcano/beijing-ict |

表内引用在实施时通过 Observer 发现，保存完整的权威 GRIP URI。运行时入口需要从
生产网络验证；公开 URL 与运行时 `api_origin` 分开配置，不自动修改端口。

## 资源和激活

新增 MCUE `computing/grafana/v1`，公开属性为 `dashboard_url`、`kubernetes_ref`，
可选 `volcano_ref`、`display_name`、`description`、`labels`。三个稳定资源名为
`resources/grafana/nj`、`resources/grafana/bj`、`resources/grafana/ict-bj`。
激活的 target 指向相应资源；DESCRIBE 返回 ready 激活及可调用 action。
关联字段只描述资源关系，不复用其他能力包的凭据或权限。

激活的 `instance_config` 包含 HTTPS `api_origin`、可选公开登录入口 `browser_origin`，以及可选默认 `datasource_uid` 和
`gpu_metric`。GPU 指标配置明确记录指标名、节点标签名、GPU 数字编号标签名、
源单位（percent 或 ratio），以及可选 UUID 标签名。配置值来自真实 Dashboard/
数据源发现；不根据域名、节点名、GPU 型号或指标命名惯例猜测。

扫码激活不需要 credential_profile。认证步骤为：

1. 激活先进入 pending_auth；对该实例调用 auth-start，复用现有 BrowserAuth 的
   五分钟会话、一次性 token、浏览器 binding、实例与 revision 约束。
2. 包声明本地 client，由它使用 Playwright 启动隔离的可见浏览器上下文，打开
   browser_origin 的 `/login/generic_oauth`。用户直接扫描原站二维码；Grafana
   自己处理 OAuth state、PKCE 和已登记的回调，不修改重定向 URI。
3. 本地 client 只从自己创建的上下文读取对应 Grafana origin 的 grafana_session，
   通过现有 Observer bridge callback 的瞬时 POST body 交给该会话；不放入 URL、
   业务 Run、内容存储、日志或聊天，不读取用户原有浏览器配置。
4. 网关在会话未过期且处于 bound 状态时接受一次回传，验证数据源访问成功后保存
   cookie 到该激活的私有持久卷。常规 health 将同一实例转为 ready。
   失效/过期/重放拒绝；本地 UI 展示状态，不把失败包装为认证成功。

本地登录 helper 支持三个独立会话，使用单个 localhost:3000 入口分别启动浏览器。
Playwright 仅是本地登录 client 的依赖，Grafana 服务镜像仍只需要 httpx。
包启用既有 persistent_state。由于 v1 已不可变发布，最终发布为 v2，保留 v1 历史。

非交互激活可独立选择 `credential_profile`。配置目录提供 `grafana_token` 或
`grafana_session`，分别是只读服务账号 token 或已登录浏览器会话 cookie 的值。
凭据不进入 MCUE 属性、instance_config、业务参数、内容上传、Run 或错误信息。
会话轮换产生的新 cookie 保存在该激活的私有持久卷，源凭据更新后重新载入。
只有成功读取数据源目录后才能 ready。操作员安全写入凭据文件，不通过聊天传递。

## Action 契约

所有导出均为 `SafeRead`、`ExternalService`，权限为 `grafana.read`。

| action | 参数 | 行为 |
|---|---|---|
| datasources_list | 空对象 | 列举 uid、名称、类型、默认标志；不返回数据源密钥和连接配置 |
| dashboards_list | 可选 search | 分页列举仪表盘 uid、标题和导航信息，验证未截断 |
| dashboard_get | uid | 读取仪表盘、面板表达式和变量配置，供指标发现 |
| metrics_query | query，可选 datasource_uid、time | 通过 Grafana 的 Prometheus 数据源代理执行即时 PromQL 查询 |
| metrics_query_range | query、start、end、step，可选 datasource_uid | 查询时间序列，验证时间范围及采样点数量 |
| gpu_utilization | hostname，可选 gpu_index、time、datasource_uid、gpu_metric | 使用已核实的指标与标签查询，返回各 GPU 实际利用率 |

action 不能选择任意主机、路径或 HTTP 方法。数据源通过 uid 选择并验证为
Prometheus；未指定 uid 时使用实例配置，或明确唯一的默认 Prometheus 数据源。
有多个无法唯一选择的数据源时返回歧义错误，不随意挑选。
使用已部署 Grafana 支持的 `/api/datasources`、`/api/search`、
`/api/dashboards/uid/{uid}` 和 `/api/datasources/proxy/uid/{uid}/api/v1/query*`。
PromQL selector 的节点名由 JSON 字符串编码产生，不拼接未转义的标签值。

通用查询保留 Prometheus 的 resultType、result、warnings 和 infos，明确 status；
上游报错、鉴权失败、无效 JSON、返回过大和查询结果截断不会包装为成功的零值。
范围查询接受 Unix 秒或 RFC3339，step 是秒，单条序列不超过 11000 个采样点。

GPU action 返回查询时间、每条样本的实际采样时间、节点名、数字 GPU 编号、可用时
的 UUID、源标签和 `utilization_percent`。无样本返回 `status=no_data` 和空 samples，
不把缺失监控伪装成 0%。源单位为 ratio 时显式乘 100；非有限值和越界值返回错误。
数字 GPU 编号与 UUID 保持分别记录，不把容器内编号直接推断为宿主机编号。
GPU busy 百分比是监控指标的定义，不等价于理论 FLOPS 的利用率。

首次扫码后才能发现受保护的 Dashboard 和数据源。调用方可在 GPU action 明确传入
发现并核实的 datasource_uid/gpu_metric，或使用实例预配的默认值，因此不需要为
登录后发现的指标重建实例。映射属于查询参数，不包含凭据。

## 实现和验收

1. 新增 gateway、Dockerfile、六组 descriptor/schema/IoContract、包模板、MCUE、
   资源接入脚本，接入现有契约生成与发布工具。依赖复用 httpx 和 Python 标准库。
2. 单测覆盖激活级地址/凭据隔离、认证失效及 cookie 轮换、数据源选择、仪表盘分页、
   Prometheus 查询错误和 warnings、节点/GPU 过滤、单位换算、缺失样本和设备标识。
3. 加入真实 gateway 的 GRIP → Driver/Slave → HTTP runtime 集成测试，逐个执行
   全部导出，验证 MCUE 关联、直接资源调用、配置挂载和独立实例生命周期。
4. 更新服务包索引、GRIP 使用文档和 personal-computing-space skill，重建技能归档。
5. 发布不可变镜像和能力包，通过 Observer 发布 MCUE、注册三个资源并激活。
   各实例通过 auth-start 和本地扫码完成认证，读取数据源和仪表盘，并执行 GPU 指标查询；北京最终查询指定节点。
   所有实际监控结果由能力包 action 获得，记录 Run、时间和采样完整性。
6. 生产操作保留既有激活及资源，记录部署前后健康和身份；不把 pending_auth 报告为
   部署验收成功。登录 client 的接入测试覆盖浏览器 origin、会话隔离、一次性回传、
   凭据验证与私有持久化，并真实启动浏览器验证扫码页面可见。

## 协议依据

- [Grafana data source API](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/data_source/)
- [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)
