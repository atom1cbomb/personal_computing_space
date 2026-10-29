# Outline 使用说明

这个包提供受限的 Outline 文档 API。激活时选择 operator 配置的 credential profile，
等待认证完成和 activation ready，再从 activation DESCRIBE 中选择 action。业务输入
不能覆盖 Outline endpoint，也不能携带 API key。

支持的 action 分为集合查询、文档读取和文档变更：

| action | 用途 |
|---|---|
| `collections_list` / `collections_info` | 查询集合及其信息 |
| `collections_documents` | 列出集合中的文档 |
| `documents_list` / `documents_search` / `documents_search_titles` | 查询文档 |
| `documents_info` | 按 `id` 读取完整 Markdown 正文和元数据 |
| `documents_documents` | 按父文档 `id` 读取子导航树，不返回正文 |
| `documents_create` / `documents_update` | 创建或更新文档 |
| `documents_archive` / `documents_restore` | 归档或恢复文档 |

使用 action 的精确输入输出 schema；`documents_info` 的正文在 `result.data.text`。
该包不提供任意 Outline method、附件管理、永久删除或 API key 管理。认证失败时重新
验证同一个 activation。
