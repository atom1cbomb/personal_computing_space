# 算礼云 Cloud 使用说明

这个包调用算礼云 control-plane API，管理区域、节点、镜像和 VM 生命周期。先在 Slave
上创建命名 activation，选择 operator credential profile（例如
`suanlicloud-prod`），完成认证并等待 activation ready；再使用 activation 的精确
action URI。AK/SK 不进入 package、资源属性或 action 输入。

只读 action 包括 `regions_list`、`zones_list`、`nodes_list`、`instances_list`、
`instances_status` 和 `images_list`。生命周期 action 包括 `instances_run`、
`instances_start`、`instances_stop`、`instances_reboot`、`instances_shutdown` 和
`instances_terminate`。每个 action 的字段和结果以当前 DESCRIBE 返回的 schema 为准，
业务调用通常返回 Run URI，最终结果是 `{action, result}`。

创建 VM 的使用顺序是：调用 `instances_run`，按 descriptor 声明用
`instances_list` 等待对应 InstanceId 进入 running 且出现 IP；登记 VM resource，
将它的 `managed_by` 指向算礼云管理 resource；然后创建绑定 VM 的
`suanli-vm-ssh-http/v5` activation，完成认证后再执行 SSH。`instances_status` 不提供
guest IP，不能把 HTTP 创建成功当成 VM 已可登录。

停止或销毁 VM 时使用对应的算礼云 lifecycle action；停止 SSH activation 不会销毁 VM。
