# Suanli Cloud capability package

**日期：** 2026-09-20  
**状态：** 实现完成；v2 已在生产激活，只读联调通过

## 必须补齐的信息

OpenAPI 只声明 `AKSKSignature` 使用 `Authorization`，但 `/root/cloud-backend`
提供了权威协议：`SUANLI-HMAC-SHA256`、`X-Suanli-Date`、四步 HMAC key
derivation，以及标准 canonical request。能力包严格按该实现签名，不发送明文
SK。OpenAPI 中的 `/v1/{service}?Action=...` 是为了让每个 Action 具有唯一的
文档 path key；真实请求是 `POST /v1/{service}`，`Action` 放在 JSON body。

## 目标

通过一个普通 `service + container:http/1` 能力包访问 Suanli Cloud 控制面，
使 Slave 具备受约束的云资源查询和虚拟机生命周期能力。Observer、Driver 和
Slave core 不增加 Suanli 专用逻辑。

## 设计

能力包容器读取宿主机 operator-owned credential profile 中的
`AccessKeyId`、`SecretKey`，使用 `production/suanlicloud_openapi.json` 声明的
API 对象模型。容器只暴露固定命名操作，不接受任意 URL、Action、HTTP method
或 shell 命令。签名和 provider 错误归一化只存在于容器内，请求使用已有
`httpx` 依赖并禁用自动重定向与副作用操作重试。

首版导出地域、可用区、节点、实例、实例状态、镜像查询，以及创建、启动、
停止、重启、关机和终止实例操作。读操作标记 `SafeRead`；会产生外部副作用的
操作分别声明 create/power/delete 权限和 `NotReplaySafe`。

能力包声明通用 `authentication.start_path` / `status_path`。安装时先用
`DescribeRegions` 验证凭据；失败时激活保持 `pending_auth`/`failed`，不会伪造
ready。AK/SK 不需要设备登录，不返回登录链接；签名材料、请求正文中的凭据和
响应中的密钥永不写入 Loom 内容层、事件或日志。

## 凭据部署

生产 Slave 上建立（不纳入 Git）：

```text
/var/lib/loom/runtime-credentials/suanlicloud-prod/
  AccessKeyId  # owner 65534:65534, mode 0600
  SecretKey    # owner 65534:65534, mode 0600
```

该目录由 `container-http-v1` 以只读方式挂载到容器
`/run/loom/credentials`。目录 owner 为 65534:65534、mode 0700。能力包
manifest 只有 profile 名称，不包含凭据。通用 runtime 只验证 operator 目录
和路径边界；文件名及内容由能力包自己验证。

## 范围

当前不实现 Observer/Driver 的 provider 分支，也不把 OpenAPI 全量自动暴露为
通用代理。新增 Suanli API 操作必须新增显式 capability export 和新包版本。

## 验证与剩余工作

已验证命名操作路由、凭据缺失、签名 reference vector 和 provider 错误归一化。
生产容器现为 revision `6dd51a0`，已修复 Go 服务端验证器的 Host
canonicalization。使用标准签名对生产执行了全部六个只读操作并成功：
`DescribeRegions`（3 个区域）、`DescribeZones`（3 个 zone）、`DescribeImages`
（27 个镜像）、`DescribeInstances`、`DescribeInstanceStatus` 和
`DescribeNodes`（12 个节点）。随后用镜像 digest
`sha256:7c39d0c7d84dbb4fceea76d514c3c9c3b6bfb03979d53c7fc43327af6552e888`
构建并发布 `suanlicloud-http/v2`，在生产 `slave-a` 上激活为 `ready/running`；
从实际运行容器再次验证上述六个只读操作全部返回 200。没有创建、修改或删除
任何算力云资源；写操作仍未联调。
