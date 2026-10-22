# Grafana 能力包本地验证

> 后续状态：Grafana 已升级至 v3，三个实例均已完成统一登录及真实数据读取验证。
> 本文保留初次接入记录；当前生产状态和 GPU 实测见
> [统一认证生产升级记录](2026-10-21-unified-authentication-production-upgrade.md)。

日期：2026-10-21。对应 [设计](../specs/2026-10-21-grafana-capability-design.md)。

## 结果

```sh
PATH="/root/loom_v2/.venv/bin:$PATH" .venv/bin/python -m pytest -q -m 'not integration and not e2e'
```

v2 扫码版本：738 passed、6 skipped、14 deselected，221.82 秒。
最初 v1 凭据文件版本：725 passed、6 skipped、14 deselected，206.81 秒。
跳过项为既有废弃入口测试；
本次未执行需外部部署环境的标记测试。

Grafana gateway 单测覆盖凭据隔离、认证失败、cookie 轮换和重启持久化、源凭据
删除/更新、数据源选择、仪表盘分页、Prometheus 即时/范围查询、warnings、GPU
数字编号与 UUID、采样时间、单位换算、缺失样本和无效指标。

服务集成测试逐个发布七个真实包，经过 GRIP → Driver/Slave → HTTP runtime →
真实 HTTP gateway，执行全部 44 个业务导出。仅 Docker 和外部 API 使用 fixture。
Grafana 的直接领域资源调用返回指定节点和 GPU 编号对应的采样利用率。

生成契约检查、`git diff --check`、skill quick_validate 和 skill ZIP 内容检查通过。
实际生产镜像另以 UID/GID 65534、禁用网络运行 mock-provider smoke，验证模块
依赖、cookie 私有状态写入及 GPU 结果解码。smoke 数字为 fixture，不是实际监控。

v2 增加完整的公开认证桥接测试：pending_auth 实例通过 auth-start 创建会话，
Observer bridge 的 open 返回公开 Grafana 登录地址，callback 验证并持久化 cookie，
health 将原实例转为 ready，随后同一实例执行 GPU action。验证 callback 重放拒绝、
原 revision 保持、不重建容器、cookie 不进入目录和 Run 结果。

本地 client 测试验证仅向一次性 bridge 发送自己浏览器的 cookie、三个会话独立、
错误不包含敏感数据、仅固定 localhost 路由可用。真实安装 Playwright 1.64.0 与
匹配的 Chromium 156.0.8078.4，确认 DISPLAY=:0 可打开可见窗口。真实 Grafana OAuth
导航到 open.cs.ac.cn 的微信扫码页，页面显示“北京 Grafana”和“使用微信扫一扫登录”。
本地入口页面经真实浏览器预检，状态正确显示且没有 JavaScript 错误。

v2 实际生产镜像另用 UID 65534、禁用网络执行认证回传 smoke，验证 cookie 只有在
数据源验证成功后落盘，重启 gateway 后仍可认证。服务镜像不包含浏览器；skill ZIP
包含本地登录 client、requirements.txt 和使用说明。

首次全量测试未将虚拟环境加入 PATH，出现 44 个既有子进程相关失败：找不到
pyright 和 runtime 子进程依赖。修正测试环境后完整重跑得到上述全通过结果，
未为此修改产品代码或放宽断言。
