# 通过 GRIP 发现 Slave

Slave 是工作空间内的一类资源，使用统一的资源目录发现：

```python
slaves = await client.resources(mcue="computing/slave")
available = [s for s in slaves if s["metadata"]["available"]]
description = await client.describe(available[0]["uri"])
```

`client` 的配置见 [GRIP 资源使用指南](grip-resource-usage.md)。目录元数据包含
`slave_id`、`available`、`lease_state`、`last_seen_at`、`base_actions`、
`executor_descriptors`、`runtime_plugin_descriptors` 和 `term_support`。
工作空间隔离与 GRIP bearer 认证适用于发现和调用，公开元数据不包含内部地址或租约 token。

`available` 表示租约近期有效，不能保证空闲容量或包已完成认证。
从描述的 offers 选择 `run-package` 或 `activate-package`，输入已发布能力包的完整 GRIP URI。
`activate-package` 以顶层 `credential_profile` 选择该激活实例的 operator 凭据配置，
与 `instance_config` 并列；同包不同实例可以使用不同 profile。名称及文件内容不进入公开描述。
运行结果是 Run 资源；命名激活结果是 activation 资源。详情与示例参见上述指南。
