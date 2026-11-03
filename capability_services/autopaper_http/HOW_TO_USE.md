# AutoPaper 使用说明

这个包用于通过 AutoPaper 搜索论文，以及查询已入库的论文、会议和年份目录。先在 Slave 上创建
命名 activation，等待它进入 `ready`/`authenticated`，再从 activation 的
DESCRIBE 结果选择 action。首次使用需要通过统一认证入口登录同一个 activation。

| action | 用途 | 交互 |
|---|---|---|
| `papers_search` | 按关键词或研究描述搜索论文，可自动从网上抓取入库 | request-reply |
| `papers_get` | 按 `paper_id` 读取论文详情 | request-reply |
| `papers_list` | 按会议和年份列出论文 | request-reply |
| `venues_list` | 列出已有论文的会议及数量 | request-reply |
| `years_list` | 列出会议已有年份及数量 | request-reply |
| `venues_catalog` | 读取会议缩写、全名和 CCF 分类 | request-reply |

## 数值参数和结果

| 字段 | 单位和含义 |
|---|---|
| `start_year`、`end_year`、`year` | 公历年份，例如 `2026`；不是 Unix 时间戳；搜索中的 null 表示无对应年份边界 |
| `min_similarity`、返回的 `similarity` | 无量纲相似度分数，不是百分比；输入阈值为 0–1，例如 `0.5`，结果缺失时可为 null |
| `top_k` | 最多返回的论文篇数，范围 1–200，默认 50 |
| 返回的 `total` / `count` | 当前结果的条数 / 对应会议或年份的论文篇数；`venues_catalog.total` 是会议条数 |

搜索至少提供非空 `keywords` 或 `description`；默认 `top_k=50`、
`min_similarity=0.5`，`top_k` 为 1–200。省略 `auto_ingest` 时保留 AutoPaper 原生默认行为
（目前为 `true`），库内结果不足时可以从网上抓取并入库；显式传 `false` 才只查询已有论文。
搜索会保存搜索历史，也可能新增论文；结果不确定时不能盲目重试。
`venues_list`/`years_list` 只反映已入库数据，未列出某会议或年份不代表 `papers_search`
无法搜索它。`papers_get` 返回已有摘要、作者、DOI 和来源链接，不触发 PDF 下载或摘要生成。

业务结果使用包声明的 output schema，结构为 `{action, result}`。认证失效时重新认证原 activation，不要把 Cookie 或 token
放入 action 参数。
