# GRIP Driver 与原生任务验证

实现 [issue #2](https://github.com/yzs15/personal_computing_space/issues/2)，对应 [实施计划](../plans/2026-10-30-grip-native-tasks.md)。

工作分支 `feat/grip-native-tasks`，worktree `/root/loom_v2/.worktrees/grip-native-tasks`，基线 `479846f`。维持每个 workspace 一个活跃 Driver；多 Driver 与自动路由仍由 #3 跟踪。

## 实现范围

- `drivers/default` 是稳定的 `computing/driver` 资源，描述格式/Schema、在线租约和提交入口，提供 `submit-plan`、`get-submission`、`how-to-use`。
- `plans/{run_id}/{closure_version}` 投影已有 ExecutionRequest 的不可变完整计划。目录/默认 Run 读取采用轻量 SQL；旧版本在修复后仍可读取，缺少快照的 Run 不发布悬空计划引用。
- Run 通过 GRIP 提供读取、取消、修复重提、验收和关闭。复用原有队列、事务、版本/成功条件约束、Driver 租约和执行恢复。
- readiness 拒绝属于提交动作输出中的 `rejected` 分支，保留公开 blockers；GRIP v1 核心信封未扩展。SDK 的 `PlanRejectedError` 暴露 code/details。
- SDK 0.10.0、Web、示例、通用 skill 和当前指南已迁移。任务 REST 路由及其专用模型删除；SDK 和 Web 精确选择默认 Driver，普通登记资源可继续使用同一 MCUE。

## 自动化验证

| 检查 | 结果 |
| --- | --- |
| 初始相关基线：GRIP/目录/提交/SDK/Worker 发现 | 30 passed |
| 原生 API、readiness、目录、GRIP、SDK、示例、协议/binding、Web 针对性回归 | 88 passed，26.23s |
| 原生完整计划真实 process 执行、Agent 注册、Worker 发现、清理恢复 | 27 passed，52.87s |
| 独立 spec 复核：原生 API/投影/目录/readiness/协议 | 56 passed |
| 独立质量复核：原生 API/持久化投影/SDK | 27 passed |
| 最终 Web 与独立 SDK/skill 构建检查 | 3 passed，3 skipped |
| 补充原生提交的 Driver 接管测试 | 1 passed |
| `node --check loom_v2/web/static/app.js`、`git diff --check` | 通过 |

各测试组有重叠，不累计为独立测试总数。构建验证实际生成 skill/SDK wheel，隔离安装后确认 SDK 不导入服务端模块。

最终完整回归命令：

```sh
/root/loom_v2/.venv/bin/python -m pytest tests -m 'not integration and not e2e' -q --disable-warnings --tb=short
```

结果：**1071 passed，23 failed，22 skipped，14 deselected，389.27s**。随后补充的 Driver 接管专项测试单独通过，验证同一 Run、计划 URI 和 execution 在接管后保持关联，旧 epoch 被拒绝，重复提交不创建第二个队列项。

23 项失败逐项在未修改的 `/root/loom_v2`（`479846f`）重跑，结果同样为 **23 failed**，最终失败集合与基线完全一致，新增失败为 0。已定位为本机 Docker runtime 不可用导致的既有 `orchestrator_runtime_unavailable` / `readiness_blocked`：

| 基线文件 | 失败数 |
| --- | --- |
| `tests/api/test_dynamic_nodes.py` | 16 |
| `tests/contracts/test_workflows.py` | 1 |
| `tests/driver/test_dynamic_orchestration_runtime.py` | 4 |
| `tests/driver/test_repository_parity.py` | 2 |

本次未修改这些运行条件，也未将基线失败改为跳过。依赖真实 Docker/Compose 的部署端到端验收未执行。现有依赖还产生弃用警告。

核心验证覆盖：

- Driver 离线发现/排队、普通完整计划真实进程输出、编排动态节点公开调用路径。
- 同业务键同输入复用、异输入冲突、响应找回；通信 request_id 独立。
- 计划版本及成功条件保护、readiness blockers、失败提交回滚、人工 accept/abandon、取消、重复关闭。
- SQLite 重载、不同 Observer 实例的状态新鲜度、workspace 隔离、冻结快照不可变、轻量目录读取。
- Driver 身份跨重启稳定、接管恢复、旧 epoch 隔离；SDK/binding 消息鉴权、Schema 校验和旧 REST 路由移除。

## 浏览器验证

使用实际 Chromium/Playwright、临时 Uvicorn Observer 和 mock S3，无外部服务调用。分别验证：

1. `http://127.0.0.1`，显式 binding endpoint。
2. `http://loom-ui.invalid` 映射到本机，非 secure context，显式 endpoint。
3. 同一普通 HTTP 域名，Observer 未配置 public_base_url、offer 省略 endpoint。

三组均通过：页面提交后显示 committed 和 Run URI；重复相同业务键保持一个队列项；服务端取消后页面刷新显示 cancelled；无效计划展示具体 typed_hole_unbound；无页面脚本错误、无 localStorage 凭据存储。

审查发现并修复普通 HTTP 下 `crypto.randomUUID` 不可用，以及 endpoint 省略时无法调用的问题。Web 使用 `getRandomValues` 生成消息 ID，并复用同源 GRIP 引导入口。

## 交付与使用影响

代码保留在上述 worktree/分支；未合并 main、未部署。旧任务 REST 调用方需升级到匹配的 SDK 0.10.0 或使用新 GRIP 契约，并提供 workspace token。没有新增数据库表或迁移。
