# 包发布返回 Observer 权威资源名称

日期：2026-10-20。状态：已实现并验证。遵循 [spec 设计原则](../spec-design-guidance.md)。

## 问题与决定

包已有由 Observer 确定的 `version_ref`，但发布接口只返回包字段，ServicePackage 和编排示例需要根据包名、版本和客户端命名空间拼接 URI。发布响应应直接提供可用于 GRIP 的权威名称。

公开的包上传、promote、abandon、列表、详情和 GRIP read 响应，在既有包信息旁增加 `resource: <完整 GRIP URI>`。值取自实际返回包的 `version_ref`；promote 必须返回提升后包的身份。GRIP DESCRIBE 与目录项继续使用协议的 `resource.uri` / `uri`，值与发布返回一致。

`resource` 是响应字段，不加入不可变包 manifest、摘要或持久化模型。保留已有包信息便于取得 digest、契约和动作；本次不引入新注册表或数据库迁移。

`LoomClient.register_package` 原样返回响应；`ServicePackage.publish` 直接返回其中的 `resource`。发布脚本、编排示例和测试调用方构造 ResourceRef 时使用该字段，删除按包名和版本拼接名称的客户端逻辑，不提供缺失字段时的拼接回退。模板依赖坐标用于匹配源模板，仍按模板的坐标规则解析；它不是服务端发布资源名。

## 验证

- 默认和非默认 authority/workspace 下发布、重复发布、查询、DESCRIBE、read 的包名称一致。
- promote 返回提升后包的名称，发布返回可直接用于激活。
- ServicePackage 尊重服务端返回的 URI，即使它与客户端按包坐标计算的 URI 不同。
- 相关 API、SDK、五服务包生命周期、编排与打包测试通过；生成契约内容保持不变。

## 验证结果

相关 API、资源名称、SDK、编排示例、清理流程、Worker、五个服务包生命周期和 SDK/server wheel 共 90 个用例验证通过。首轮 89 个通过；提升版本测试新增 abandon 检查后，将“candidate 未被 promote 改变”的断言移至 abandon 之前，单独复测通过。生产代码未因该测试调整而改变。

`scripts/generate_service_contracts.py --check` 和 `git diff --check` 均通过。本次未部署服务，也没有重新发布现有能力包。
