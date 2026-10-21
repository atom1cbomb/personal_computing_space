# Volcano 独立能力包与 Kubernetes 资源关联

日期：2026-10-21。

## 目标

通过独立的 `volcano-http/v2` 能力包读取 Volcano Dashboard 的任务队列、PodGroup、
Pod、Job，并统计 GPU 分配。北京、南京、北京 ICT 使用同一包的三个激活实例，
各自选择面板地址。外部调用统一经过 Observer 的 GRIP action。

设计遵循 [spec 规范](../spec-design-guidance.md)，复用现有 HTTP runtime、
`instance_config`、MCUE、资源注册和目标绑定机制，不增加执行引擎或数据库表。

## 已确认的面板协议

检查三个面板的公开前端及只读协议，确认 API 与 Kubernetes API server 分开：

| 实例 | Dashboard origin | 对应的现有 Kubernetes 激活 |
|---|---|---|
| beijing | https://volcano-prod.bj.cs.ac.cn:7443 | activations/kubernetes-http/v4/slave-a/beijing |
| nanjing | https://volcano-prod.nj.cs.ac.cn:10062 | activations/kubernetes-http/v4/slave-a/nanjing |
| beijing-ict | https://volcano-prod.ictbj.cs.ac.cn:10062 | activations/kubernetes-http/v4/slave-a/beijing-ict |

前端读取 `/api/namespaces`、`/api/queues`、`/api/podgroups`、`/api/pods`、`/api/jobs`。
namespace 为 `All` 表示跨 namespace 读取；queue API 使用 page/limit 分页，其余页面
在浏览器本地分页。网关统一处理上游分页并验证列表完整性。
三个 `/api/podgroups` 只读入口目前均可无会话返回 `items` / `totalCount` JSON。
直接使用这些只读 API；若上游返回登录重定向或 401/403，返回明确错误，
不将 HTML 登录页当作列表或零资源。面板浏览器登录的端口丢失问题不改写为虚构的
OAuth/device-code 流程。

## 资源与关联

新增 MCUE `computing/volcano/v1`，定义公开属性：

- `display_name`、`description`、`labels`。
- `dashboard_url`：面板的 HTTPS origin，包含实际端口。
- `kubernetes_ref`：对应 Kubernetes 资源的完整 GRIP URI。

当前 Kubernetes 在目录中由激活资源表示，直接引用其 Observer 返回的 URI。
不为此复制一份 Kubernetes 资源或创建通用关系注册表。

注册 Volcano 资源，例如：

```json
{
  "resource_id": "grip://loom.local/workspaces/workspace-default/resources/volcano/beijing",
  "mcue_ref": "grip://loom.local/workspaces/workspace-default/mcue-types/computing/volcano/v1",
  "attributes": {
    "display_name": "北京 Volcano",
    "dashboard_url": "https://volcano-prod.bj.cs.ac.cn:7443",
    "kubernetes_ref": "grip://loom.local/workspaces/workspace-default/activations/kubernetes-http/v4/slave-a/beijing"
  }
}
```

将 Volcano 激活的 `target` 绑定到该资源。DESCRIBE Volcano 资源返回属性、关联的
Kubernetes URI、ready 激活及 action；DESCRIBE 激活保留现有 `metadata.target`。
从 Kubernetes 查找面板时，列举 `computing/volcano` 并匹配 `attributes.kubernetes_ref`。
关联是导航信息，不改变 Kubernetes 激活的权限或执行配置。

## 实例配置

每个激活使用 `instance_config: {"api_origin": "https://host:port"}`，由 action
descriptor 的 `instance_config_schema_ref` 验证，运行时挂载现有 `instance.json`。
面板地址只从激活配置读取，action 参数不接受任意 URL、HTTP 方法或路径。
配置必须为不含用户信息、query、fragment 的 HTTPS origin，保留端口并验证 TLS。
HTTP runtime 原有配置写入函数含 SSH 专用校验，本次改为传递 JSON 对象，复用 Observer
的包级配置 schema 验证，具体连接校验由各 gateway 负责。运行时保留的
`target_resource_ref` 仍由绑定生成，不允许 instance_config 覆盖；网关从同一文件读取
api_origin。部署时更新 runtime 插件并重启 Slave 控制进程，业务容器保持运行。
公开的 dashboard_url 与运行时地址在注册/激活时显式设置；更新公开属性不会自动
改变运行时连接配置。

真实接入确认工作区与生产存在 split DNS：公开三个域名在工作区解析到公网入口，
生产的北京、北京 ICT 解析到不同内网 ingress，使用 443 才能获得 Dashboard。
因此公开 `dashboard_url` 保留用户提供的端口，运行时 `api_origin` 单独配置：
北京 `https://volcano-prod.bj.cs.ac.cn`，南京仍为带 10062 的公开地址，
北京 ICT `https://volcano-prod.ictbj.cs.ac.cn`。网关不猜测、回退或改写端口。
v1 原型已不可变发布，最终模板改为 v2；不保留旧配置字段兼容层。

## Action 契约

| action | 参数 | 上游来源 |
|---|---|---|
| namespaces_list | 空对象 | /api/namespaces |
| queues_list | 可选 state、search | /api/queues |
| podgroups_list | 可选 namespace、status、search | /api/podgroups |
| pods_list | 可选 namespace、status、search | /api/pods |
| jobs_list | 可选 namespace、status、search、queue | /api/jobs |
| gpu_usage | 可选 namespace、resource_name | /api/pods |

namespace 默认 `all`，翻译为面板的 `All`；指定 namespace 时保持指定范围。
五个列表 action 返回 `status=ok`、`items`、`total_count`、`complete=true`。
上游截断、重复页或缺失分页进展返回错误，GPU 统计不得基于部分列表返回成功。
所有 action 均为 `SafeRead`、`ExternalService`，权限声明为 `volcano.read`。

GPU 默认资源名为 `nvidia.com/gpu`。输出标明 `scheduled_pod_requests` 口径、采集时间、
范围、已分配数量、运行中数量、未调度申请量、按 namespace 的汇总及 GPU Pod 摘要。
已分配量取已绑定节点且未进入 Succeeded/Failed 的 Pod 的有效 GPU request；
requests 未给出时使用 limits。有效 request 包含普通容器、init 容器、restartable init
容器以及 Pod overhead 的调度需求。等待中的未绑定 Pod 单列，不计入已分配数量。
资源数量必须可解析为非负整数；MIG 等扩展资源按 resource_name 分别查询，
结果表示对应资源单位，不推断物理 GPU 卡数。GPU 实时计算利用率需要监控指标，
不能由任务申请数推导。

## 实现与验收

1. 新增 gateway、Dockerfile、包模板、六组 descriptor/schema/IoContract，接入现有
   `generate_service_contracts.py`，新增 MCUE 文件。
2. 单测覆盖 origin/端口、namespace/All、分页完整性、上游鉴权及错误、GPU 统计中
   已结束/未调度/init/sidecar 的处理。
3. 通过现有 GRIP → Driver/Slave → HTTP runtime 测试执行新包的六个 action，验证
   激活配置挂载、Volcano 资源 DESCRIBE、Kubernetes 关联及直接资源调用。
4. 更新服务包目录文档和 personal-computing-space skill 的发现/调用说明。
5. 发布不可变镜像和包；通过 Observer 发布 MCUE、注册三个 Volcano 资源并激活。
   对三个资源执行队列和 PodGroup 查询，对北京执行 GPU 查询，保留 Run 证据。
   既有 Kubernetes 激活继续承担 Kubernetes/VolcanoJob 提交功能。
