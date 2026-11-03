# 算礼云 Cloud 使用说明

这个包调用算礼云 control-plane API，管理区域、节点、镜像和 VM 生命周期。先在 Slave
上创建命名 activation，选择 operator credential profile（例如
`suanlicloud-prod`），完成认证并等待 activation ready；再使用 activation 的精确
action URI。AK/SK 不进入 package、资源属性或 action 输入。

只读 action 包括 `regions_list`、`zones_list`、`nodes_list`、`instances_list`、
`instances_status` 和 `images_list`。生命周期 action 包括 `instances_run`、
`instances_start`、`instances_stop`、`instances_reboot`、`instances_shutdown` 和
`instances_terminate`。每个 action 的字段和结果以当前 DESCRIBE 返回的 schema 为准，
业务结果是 `{action, result}`，按各 action 的 Schema 读取。

## 参数和结果的单位

| 字段 | 含义和单位 |
|---|---|
| `instances_run.Count` | 创建 VM 的台数，例如 `1` |
| `instances_run.Cpu` | 每台 VM 的 vCPU 数，例如 `2`；不使用 Kubernetes 的 millicore 写法 |
| `instances_run.Mem` | 每台 VM 的内存，单位 **字节 B**；4 GiB 传 `4294967296` |
| `instances_run.DiskSize` | 每台 VM 的根磁盘容量，单位 **字节 B**；20 GiB 传 `21474836480` |
| `instances_list.PageNumber` / `PageSize` | 从 `1` 开始的页码 / 每页实例条数；`TotalCount` 是实例总条数 |
| 返回 VM 对象的 `Cpu` / `Mem` / `DiskSize` | vCPU 数 / 内存字节数 / 磁盘字节数 |
| 返回 VM 对象的 `Disks[].SizeBytes` / `Disks[].BusIndex` / `Vmid` | 单块磁盘字节数 / 对应总线内从 0 开始的磁盘索引 / 不透明 VM 数字标识 |
| 返回 Node 对象的 `MaxCpu` / `Cpu` | CPU 总核数 / 已用 CPU 的 0–1 比例；`Cpu=0.5` 表示总 CPU 的 50% |
| 返回 Node 对象的 `Mem`、`MaxMem`、`Disk`、`MaxDisk` | 已用/总内存、已用/总磁盘，均为字节 B |
| 返回 Node 对象的 `Uptime` | 节点运行时长，单位秒 |
| `images_list` 返回对象的 `Bytes` / `Status` | 镜像文件字节数 / 无量纲的供应商状态码 |

容量参数按算礼云 API 约定原样传递，不做单位转换。`1 GB = 1000000000 B`，
`1 GiB = 1073741824 B`。例如创建 2 vCPU、4 GiB 内存、20 GiB 根磁盘的 VM 时，
相应字段为 `{"Count":1,"Cpu":2,"Mem":4294967296,"DiskSize":21474836480}`；
其余必填字段仍按 schema 提供。

创建 VM 的使用顺序是：调用 `instances_run`，按 descriptor 声明用
`instances_list` 等待对应 InstanceId 进入 running 且出现 IP；登记 VM resource，
将它的 `managed_by` 指向算礼云管理 resource；然后创建绑定 VM 的
`suanli-vm-ssh-http/v6` activation，完成认证后再执行 SSH。`instances_status` 不提供
guest IP，不能把 HTTP 创建成功当成 VM 已可登录。

停止或销毁 VM 时使用对应的算礼云 lifecycle action；停止 SSH activation 不会销毁 VM。
