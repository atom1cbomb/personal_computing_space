# Observer 资源目录优化生产部署

部署目标：`9.0.16.66`，公开 Observer：`http://9.0.16.66:8080`。
完成时间：2026-10-22 22:14（北京时间）；最终目录采样时间：22:17:30。
设计与本地验证见 [spec](../specs/2026-10-22-observer-resource-directory-performance-design.md) 和 [验证报告](2026-10-22-observer-resource-directory-performance.md)。

## 发布与保留

- 发布目录：`/opt/loom-production/releases/observer-directory-20261022-221143`。
- 镜像：`sha256:663cedbf36ce1dd32972d5e2f14a65b2e9090938e8fb6760f264af5023daa587`。
- 源码归档 SHA-256：`fe4968ac529d2189ac724e319874d89f293b51a9e4f4eb2e2c213e9b573cdfa2`。

核对生产镜像的 113 个核心文件后，确认仅 `loom_v2/observer/grip.py`、`loom_v2/observer/repository.py` 与本地不同。使用原生产镜像及现有依赖构建新镜像，更新这两个文件在 `/app` 与 site-packages 中的副本；启动后再次核对两个位置的文件摘要。SDK 未改动。

切换前保留旧镜像、源码和 Compose 配置，使用 pg_dump 备份 Observer 数据库，并以 pg_restore --list 校验备份。数据库与原有资源保留，无新增迁移。通过原 Compose 项目仅重建 Observer 服务，其他 19 个运行容器的 ID、镜像和启动时间全部保持一致，包括 Driver、Slave、13 个业务能力容器、两个 PostgreSQL、MinIO 和镜像仓库。

第一次切换后的验收脚本使用了 localhost，而 Observer binding 声明的 endpoint 是公开地址，SDK 因此返回 `untrusted_endpoint`。脚本自动恢复旧镜像；确认地址校验原因、改用公开 Observer 地址后重新切换成功。最终服务健康，execution/orchestration 均为 true。

## 生产性能与正确性

同一工作区、同一 SDK、同一份 531 项资源目录；测量均使用公开 Observer 接口。

| 操作 | SDK 耗时 | GRIP 请求数 |
|---|---:|---:|
| 部署前完整 resources() | 23.4846 s | 6 |
| 部署后采样 1 | 1.0394 s | 6 |
| 部署后采样 2 | 0.7889 s | 6 |
| 部署后采样 3 | 0.7872 s | 6 |
| 部署后采样 4 | 0.7849 s | 6 |

部署后最后三次中位数为 0.7872 秒，约为部署前的 1/30。资源 URI 集合、包/action/Schema/MCUE 等静态元数据、注册资源的公开属性均与部署前一致。对所有计算资源和期望运行的激活实例逐项 DESCRIBE 成功。

额外只读检查：MCUE 过滤返回两台 VM；读取 Kubernetes v6 的动态调用 Schema 成功；读取既有 Run `run-012bb139e982` 的状态与结果成功，状态为 completed。验收未创建新的业务 Run。

## 当前业务资源

以下路径均使用前缀 `grip://loom.local/workspaces/workspace-default/`。表中服务状态取对应激活实例；注册的 Grafana/Volcano 资源自身 state 为 registered。

| 资源 | URI 路径 | 状态 |
|---|---|---|
| Slave | `slaves/slave-a` | available=true |
| AutoPaper VM | `resources/vm/9.0.16.113` | running，SSH ready；display_name=autopaper-vm |
| 算力云 VM | `resources/vm/loom-v2-auth-3c474f5bb5` | running，SSH pending_auth |
| 北京 Kubernetes | `activations/kubernetes-http/v6/slave-a/beijing` | ready/authenticated |
| 南京 Kubernetes | `activations/kubernetes-http/v6/slave-a/nanjing` | ready/authenticated |
| 环保园 Kubernetes | `activations/kubernetes-http/v6/slave-a/beijing-ict` | ready/authenticated |
| 北京 Volcano | `resources/volcano/beijing` | 绑定 v2，ready |
| 南京 Volcano | `resources/volcano/nanjing` | 绑定 v2，ready |
| 环保园 Volcano | `resources/volcano/beijing-ict` | 绑定 v2，ready |
| 北京 Grafana | `resources/grafana/bj` | 绑定 v4，ready/authenticated |
| 南京 Grafana | `resources/grafana/nj` | 绑定 v4，ready/authenticated |
| 环保园 Grafana | `resources/grafana/ict-bj` | 绑定 v4，ready/authenticated |
| 算力云 API | `activations/suanlicloud-http/v5/slave-a/default` | ready/authenticated |
| Outline | `activations/outline-http/v3/slave-a/default` | ready/authenticated |

算力云 VM 的 SSH 激活 `activations/suanli-vm-ssh-http/v4/slave-a/loom-v2-auth-3c474f5bb5` 在部署前即为 pending_auth/auth_required，部署后状态相同，需要重新认证。VM 的 Observer 登记状态仍为 running。

目录总计 531 项：1 个工作区、12 个 MCUE 定义、1 个 Slave、23 个包版本、38 个激活实例、8 个注册业务资源、85 个 Run、198 个 action、165 个 Schema。38 个激活实例中，12 个 ready、1 个 pending_auth、25 个 stopped。

## 审计材料

发布目录保留 manifest、源代码归档、构建/切换日志、数据库与源码备份、回滚镜像标签、部署状态，以及 `audit/` 中的部署前后公开目录和计时、只读验证和本报告。本地材料位于忽略目录 `production/observer-directory-*`，凭据未写入报告或公开内容存储。
