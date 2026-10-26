# 能力包部署边界与激活前检查

日期：2026-10-26

## 问题与目标

AutoPaper 在 slave-a 上完成容器启动和认证后，激活状态为 ready，但五个
request-reply 动作没有可用 binding。此前部署过程中通过 SSH 补数据监听、
TLS 代理和 Observer 中转路由，混淆了节点接入与能力包部署的职责。

能力包部署应只使用镜像仓库和 Loom 公开接口。节点拉取镜像、创建容器、
持久化实例状态；外部部署者不需要目标节点的 SSH 凭据。数据入口、TLS、
信任密钥和节点路由属于节点接入配置，供该节点的所有能力包复用。

本设计遵循仓库 spec-design-guidance.md：复用现有 GRIP、binding 校验、
错误契约和依赖，不增加另一套远程执行通道，也不增加兼容分支。

## 部署流程

1. 在开发机或 CI 构建镜像，推送到节点可访问的 registry，取得不可变 image_ref。
2. 通过 ServicePackage.publish 注册包。发布与选择节点无关，允许尚无适用节点。
3. 对选定 Slave 调用公开 check-package，输入 package URI 和 instance_name。
4. 检查通过后调用 activate-package；服务端再次检查，不能依赖客户端自觉检查。
5. 节点通过现有控制通道拉取镜像并启动实例，按包声明完成认证。
6. 从 DESCRIBE 检查总体可用性及业务动作，调用业务接口验收。

检查失败应返回明确的缺失条件。部署流程不得自动 SSH、修改监听端口、
分发 TLS 私钥、修改 Observer 配置或重启平台组件。节点运维仍使用既有
基础设施部署工具，其 SSH 实现不作为能力包部署入口。

## 共用路由策略

将 ObserverServiceAccess 中的路由选择和 binding 投影提取为同一个轻量策略。
ObserverRepository 和公开发现使用同一实例，保证直接激活、configure、
Driver capability.desire、编排 ensure_activation 都受到同样约束。

策略保留现有规则：激活专属配置优先于节点配置；直接入口与 Observer 中转
支持并存；上游按交互模式选择，不能因只配置了流上游而遮蔽 JSON 直接入口。
内嵌拓扑只在测试装配中显式提供，生产 Observer 不具备该捷径。

检查以包的所有 action_exports 为准：

| 交互 | 必要条件 |
| --- | --- |
| task | 已有节点执行能力；不要求服务数据入口或服务签名密钥 |
| request-reply | 服务签名配置和至少一条合法 JSON binding |
| byte-stream | 服务签名配置和至少一条合法 stream binding |

沿用 validate_endpoint 的 TLS/协议规则和 audience 校验。缺密钥、无入口、
错误入口分别公开为 service_signing_unavailable、binding_unavailable、binding_invalid。
诊断只包含节点/激活/动作 URI、交互模式和稳定原因，不输出私钥路径或配置内容。

这是确定性的配置准入检查，不声称提前证明提供方登录、镜像拉取、远端端口
连通或证书实际握手成功。运行时健康和真实业务调用仍负责这些验证；检查
不通过执行论文检索等业务动作探测网络，避免副作用。

## 公开检查与发现

Slave 增加只读 check-package offer，其输入 schema 为：

```json
{"package":"grip://…/packages/autopaper-http/v1","instance_name":"autopaper"}
```

输出包含 ready、required_interactions 和 issues。每个 issue 包含 code、
interaction（节点整体错误时为 null）和受影响的动作 URI 列表。
节点离线及运行时不支持也必须使 ready=false。该动作不创建激活、不改变
revision、不拉取镜像、不启动认证。节点 DESCRIBE 另公开通用服务交互模式
及其配置缺失原因；有激活专属覆盖时，以 check-package 的结果为准。

activate/configure 在写入 desired state 前执行同一检查。服务 binding
条件失败沿用 GRIP unsupported_binding；调用方通过 check-package 获取
逐项原因。仓储/内部控制使用稳定领域错误 activation_preflight_failed，
包含同一 issues。失败不得消耗幂等键或 revision；条件修好后可重试原请求。
stop 不依赖数据入口，故障期间仍可停用和清理实例。

## 就绪语义

保留持久化 activation_state 作为节点报告的运行状态。公开激活描述增加
runtime_state、available、unavailable_reason；state 为汇总状态：运行状态
ready 但必需服务入口缺失时显示 degraded。逐动作 offers 保持精确：已可用
的 task/服务动作仍可调用，缺入口的动作不可调用。

activate/configure 的响应 state 使用相同汇总规则。配置撤销后重新 DESCRIBE
立即体现降级；恢复配置后无需修改包或重建实例即可恢复公开 ready。
认证门户遇到入口缺失应报告 activation_binding_unavailable，避免把已成功
的提供方登录误报为完成部署，或一直等待到登录过期。

## 节点接入校验

部署配置加载时检查本清单内配置服务路由的 Slave 是否声明 grip_data_port，
路由是否具有 endpoint/audience，以及签名配置是否完整。节点可有意只执行
task，此时不配置服务路由和数据端口，不伪装成具备直接服务调用能力。
TLS 代理及证书仍由节点接入流程管理；文档将该流程与日常包发布明确分开。
不为单个包创建代理，不在包 manifest 中加入宿主机端口或 TLS 配置。

## 验证

- 无服务入口的远程拓扑部署混合包：check-package 指出缺少交互模式，直接
  激活拒绝，激活记录、事件、revision、provision 调用均不增加。
- 同一拓扑仅 task 的包正常激活；缺服务签名不影响 task。
- 分别覆盖缺签名、只具备 JSON、只具备 stream、非法 binding、激活专属覆盖。
- 仓储直接调用与 configure 同样受检查；stop 在路由撤销后仍成功。
- 运行状态 ready 时撤销/恢复路由，公开状态 degraded/ready 正确变化，任务
  offers 保留；认证门户显示基础设施原因。
- 以真实本地 HTTP 服务隔离 Observer 和 Slave 数据入口，覆盖公开发布、
  检查、激活和调用，禁止内嵌数据中转捷径，不调用 SSH。
- 部署配置拒绝已声明路由却没有监听或签名配置的节点；既有 task-only 清单
  和合法服务清单通过。运行相关 API、编排、能力包、SDK 和部署回归测试。

## 实施顺序

先提取共用路由策略并接入仓储写入边界，再增加公开检查与汇总状态，随后
修正认证反馈和节点部署校验，最后补远程拓扑回归及发布文档。实现不需要
修改生产节点配置或通过 SSH 更新正在运行的服务。
