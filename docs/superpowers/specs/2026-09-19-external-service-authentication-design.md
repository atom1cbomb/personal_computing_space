# 外部服务能力包通用认证设计

**日期：** 2026-09-19  
**状态：** 实现设计

浏览器 authorization-code 登录的增量协议与入口见 [远程回调代理设计](2026-09-22-browser-auth-redirect-proxy-design.md)。下文两个路径描述原有设备码/静态凭据握手；需要浏览器往返的包按增量设计增加可选 `browser_path`，并选择命名实例。

## 目标

让需要用户认证的 `service + container:http/1` 能力包在安装后自动进入
“待认证”状态，向调用方返回设备登录信息；用户完成认证并通过能力包自身的
权限探测后，激活才进入 `ready`。Observer、Driver 和 Slave Core 不识别具体
外部服务（Kubernetes、云厂商等）的认证细节。

## 设计决策

1. 认证是普通 HTTP service runtime 的通用可选协议。package body 增加
   `authentication`，只声明两个包内 HTTP 路径：`start_path` 与 `status_path`。
2. 认证端点统一使用无参数 `POST`，返回 JSON `{"status": ...}`。成功返回
   `authenticated`；待用户操作返回 `auth_required`，并可带
   `verification_uri`、`user_code`、`qr_payload`、`expires_at`、
   `poll_after_seconds`。token、kubeconfig 和任何 secret 不进入 Loom。
3. `container-http-v1` 在容器健康检查通过后调用 `start_path`。若结果为
   `auth_required`，容器仍保持运行，但激活状态为 `pending_auth`，健康报告的
   `details.authentication` 原样携带公开登录信息。
4. Slave 的后台健康检查调用 `status_path`，认证成功后报告 `ready`；认证仍未
   完成则继续报告 `pending_auth`。只有 `ready` 激活允许 dispatch。
5. `pending_auth` 是生命周期状态，不等同于容器故障；deprovision 仍可停止它。
   激活 API 直接返回健康报告，客户端可立即展示登录信息；后续健康报告用于
   完成状态转换。认证过期时回到 `pending_auth`。
6. 新认证协议只扩展通用 package/runtime 合约；Kubernetes gateway 只负责
   产生设备码和权限探测，不增加 Observer/Driver 分支。
7. Observer 提供通用的 `POST /api/v1/capability-packages/{package_ref}/auth/start`
   重新认证入口。请求携带 `target_slaves` 和精确的 `approved_digest`；该操作
   只调用现有激活的 runtime，不改变 desired state，也不重建容器。

## 合约

```json
{
  "authentication": {
    "start_path": "/v1/auth/start",
    "status_path": "/v1/auth/status"
  }
}
```

`start_path`、`status_path` 必须是不同的 origin path，且不能与健康路径或能力
export 路径冲突。未声明 `authentication` 的服务保持现有行为：健康后直接
`ready`。

重新认证请求示例：

```http
POST /api/v1/capability-packages/capability-package://k8s-gateway/v1/auth/start
Content-Type: application/json

{"target_slaves":["slave-a"],"approved_digest":"<package_digest>"}
```

Observer 在生产部署中将请求转发给 Driver；Driver 仅按已注册 Slave 路由，Slave
校验 workspace、package digest 和现有 running activation 后调用 runtime plugin
的 `auth_start`。返回值仍是健康报告，包含脱敏后的认证 challenge。

## 状态与安全边界

`pending_auth` 允许从 `provisioning`、`ready`、`degraded` 和 `lost` 进入；只有
`authenticated` 探测可以进入 `ready`。登录信息是外部服务返回的公开 challenge，
应通过正常 health report/API 返回；运行时不得记录响应 body 或 secret。认证请求
失败按 `degraded`/`failed` 处理，不能伪造 `ready`。

## 验证

测试覆盖无认证包的兼容行为、设备码返回与脱敏、认证完成后的状态转换、认证失效
回到 `pending_auth`、Observer 重新认证不重建容器，以及未认证时 dispatch 被拒绝。
认证 gateway 在 `/auth/start` 与 `/auth/status` 间保持同一认证进程；显式重新认证
才会替换该进程。Kubernetes 包把现有 `/v1/auth/start` 和 `/v1/auth/status`
声明为该通用协议的实现。
