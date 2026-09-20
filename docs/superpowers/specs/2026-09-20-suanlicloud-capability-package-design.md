# Suanli Cloud capability package

**日期：** 2026-09-20  
**状态：** 草稿；认证协议待补充，尚未完成可用部署

## 必须补齐的信息

所给 OpenAPI 只声明 `AKSKSignature` 使用 `Authorization`，没有签名算法、
canonical request 格式、时间戳头、编码方式或官方 SDK 地址。不能据此推导
签名协议。当前实现保留明确失败的签名入口，不发送猜测签名或明文 SK。

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

已验证命名操作路由、凭据缺失和未知签名协议下的 fail-closed 行为。生产试装
v1 的认证探测未通过，已停用；未创建、修改或删除任何算力云资源。补齐官方
签名协议后还需完成 OpenAPI schema 绑定、只读真实 API 联调和 Observer 到
Slave 的结果落盘验证；应发布新包版本，不覆盖 v1 的不可变身份。
