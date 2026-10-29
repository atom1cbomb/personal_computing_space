# AutoPaper 使用说明

这个包用于查询 AutoPaper 中已有的论文、会议和年份目录。先在 Slave 上创建
命名 activation，等待它进入 `ready`/`authenticated`，再从 activation 的
DESCRIBE 结果选择 action。首次使用需要通过统一认证入口登录同一个 activation。

| action | 用途 | 交互 |
|---|---|---|
| `papers_search` | 按关键词或研究描述搜索已有论文 | task，返回 Run |
| `papers_get` | 按 `paper_id` 读取论文详情 | request-reply |
| `papers_list` | 按会议和年份列出论文 | request-reply |
| `venues_list` | 列出已有论文的会议及数量 | request-reply |
| `years_list` | 列出会议已有年份及数量 | request-reply |
| `venues_catalog` | 读取会议缩写、全名和 CCF 分类 | request-reply |

搜索至少提供非空 `keywords` 或 `description`；默认 `top_k=50`、
`min_similarity=0.5`，`top_k` 为 1–200。搜索固定只读已有数据，`auto_ingest` 不会
自动抓取论文。先调用 `venues_list`/`years_list` 确认可查询范围。`papers_get` 返回
已有摘要、作者、DOI 和来源链接，不触发 PDF 下载或摘要生成。

业务结果使用包声明的 output schema；task action 返回 Run URI，等待 Run 结束后读取
最终 `{action, result}`。认证失效时重新认证原 activation，不要把 Cookie 或 token
放入 action 参数。
