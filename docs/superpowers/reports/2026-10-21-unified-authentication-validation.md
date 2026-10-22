# 统一认证验证记录

日期：2026-10-21。对应 [设计](../specs/2026-10-21-unified-authentication-design.md)
和 [使用说明](../../authentication.md)。

## 完成的变更

SDK 0.7 提供 `loom-auth` 与 `python -m loom_orchestration.auth`，用一个本地
入口处理声明的 redirect、browser-cookie、device-code 和 credential-profile。
删除包专用客户端和短期会话文件流程。用户可直接点击重新认证，客户端重新发现
当前 revision，并在实际 ready/authenticated 后显示完成。

Observer 统一 auth-start 返回结构，DESCRIBE 返回脱敏认证状态；显式认证支持
恢复已有 failed 实例，普通健康报告仍不能解除 failed。SSH 区分 Permission denied
和网络/主机密钥故障；Kubernetes 定期重新探测，支持凭据插件刷新，并停止展示
已经退出的设备授权进程所打印的旧链接。Grafana 保持会话验证与私有持久化。

新模板：grafana-http/v3、kubernetes-http/v5、suanli-vm-ssh-http/v4、
outline-http/v3、suanlicloud-http/v5、vm-ssh-http/v5。算力云资源效果引用同步升级
到 SSH v4，生成的描述符摘要已更新。Volcano 无认证声明，保持 v2。

## 验证证据

- 真实 Chromium 配合本地模拟授权站点，逐一验证三种浏览器方式。redirect 测试
  包含 HTTP 302 到固定 localhost 回调；修复 Playwright 路由不捕获重定向链后续
  URL 的情况，使用导航请求事件捕获实际回调。未访问真实账号或完成生产扫码。
- 真实浏览器执行入口页 JavaScript，验证登录按钮、过期后重新认证和无脚本错误。
- GRIP → Observer → Slave → HTTP runtime → Grafana 网关集成验证三轮认证，
  包括 cookie 失效、历史 failed 恢复、旧 token/回调拒绝、业务查询恢复；始终同一
  激活 URI/revision，Docker fixture 只创建一个容器。凭据不进入目录或 Run 结果。
- Kubernetes 使用真实子进程 fixture 验证定期重探测、过期设备码撤回和重新发起；
  SSH 验证入口过期、凭据拒绝、连接失败和主机密钥变化的不同结果。
- SDK 测试覆盖原入口重试、revision 变化、多实例选择、桥接 origin 和 POST
  凭据通道、CSRF/Host/Origin 检查、异常脱敏。包契约验证四种声明和历史包只读兼容。
- 契约生成检查、diff 空白检查、skill 验证通过。重建技能归档，确认内含 SDK 0.7
  wheel、loom-auth entrypoint 和当前客户端源码，没有旧包专用客户端。

完整仓库回归：

```sh
PATH="/root/loom_v2/.venv/bin:$PATH" .venv/bin/python -m pytest -q -m 'not integration and not e2e' --disable-warnings
```

结果：**761 passed、6 skipped、14 deselected，263.29 秒**。
命令按项目标记排除了 integration/e2e；此次四项真实 Chromium 测试均已执行。
修正历史包不再声明不可用的 auth-start offer 后，另补跑 GRIP API：11 passed。


## 部署状态

代码、文档、测试和 SDK/技能分发归档完成后，已继续执行生产升级，详见
[生产升级记录](2026-10-21-unified-authentication-production-upgrade.md)。历史包身份保留，
新版通过 Observer 重新发布。
之后的生产验证已由用户完成三个 Grafana 和算力云 SSH 登录，并通过真实业务
action 验证。模拟授权与 fixture 验证仍单独计数，生产证据见上述部署记录。

## 生产重检补充修复

Kubernetes v5 的周期权限检查在子进程完成前返回 auth_required，生产观察到
ready/pending_auth 短暂切换。v6 增加仅用于已有验证成功的周期检查的有界宽限：
设备授权挑战、失败和超时立即停止沿用成功，显式 auth-start 不沿用旧结果。
新增真实子进程测试覆盖成功、失败、超时、显式重启、零退出但权限拒绝；原有
过期设备码测试继续覆盖新挑战优先。相关网关、包契约、GRIP 服务测试：
**50 passed，60.06 秒**。契约生成与 diff 检查通过。
