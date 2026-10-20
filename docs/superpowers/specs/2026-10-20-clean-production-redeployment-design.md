# Driver 部署配置清理与生产重新初始化

日期：2026-10-20。遵循 [spec 设计原则](../spec-design-guidance.md)。

Driver 生产进程仅执行 Observer 队列，不创建模型 provider。删除部署 DriverConfig 的 Codex/Volcengine URL、模型、API key 和 catalog 配置，删除相关 secret 读取、归档与启动脚本生成逻辑；Driver 配置仅保留 workspace_path，旧模型字段应拒绝。同步生产私有 inventory、公共示例和测试。保留现有内部认证和执行工作区。

补齐 Observer 的 GRIP token 归档与错误日志脱敏，确保渲染的 secret 挂载在远端实际存在。新 token 写入 git 忽略的凭据文件，不输出其内容。

按用户授权重新初始化 9.0.16.66 上 loom-production：停止应用角色和属于该 workspace 的 Loom 能力容器，删除所属 PostgreSQL、MinIO、运行时持久卷及运行状态，替换部署目录并重新构建启动。重置范围由项目/运行时 labels 和实际挂载确认；服务凭据保留，外部云资源不在主机重部署范围内。

部署后验证迁移版本、角色注册、GRIP 认证及发现，并执行最小包发布/激活/运行/停止验收。验收后移除验收数据并恢复空的业务数据库与内容存储，最终保留启动产生的 Driver/Slave 注册和调度回执。
