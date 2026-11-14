# 队列与解析速度观测

默认 Compose 的持久队列模式提供三个入口：`/health` 查看存活，`/stats` 查看全局快照，`/metrics` 供 Prometheus 采集。

```bash
curl http://127.0.0.1:3888/stats
curl http://127.0.0.1:3888/metrics
```

设置 `PDF_API_KEY` 后，`/stats` 需要同样的 Bearer 密钥；`/health`、`/metrics` 保持可读。

`/stats` 返回 `queue / parsing / workers` 三部分：

| 字段 | 含义 |
| --- | --- |
| `queue.queued` / `queue.running` | 等待与执行中的任务数 |
| `queue.pending` / `queue.max_pending` | 全部在途任务数与数量预算 |
| `queue.pending_pages` | 等待与执行中的原 PDF 页数之和，不作为准入预算 |
| `queue.oldest_wait_seconds` | 最老等待任务的本次排队时长 |
| `parsing.completed_jobs` / `parsing.completed_pages` | 采集以来成功执行并发布的文档数与页数 |
| `parsing.measured_jobs` / `parsing.measured_pages` | 带有效执行计时的成功样本量 |
| `parsing.parse_seconds` | 这些样本的累计解析执行秒数 |
| `parsing.average_seconds_per_page` | 按页加权的平均解析秒/页 |
| `parsing.average_pages_per_second` | 上一指标的倒数，平均页/秒 |
| `parsing.recent_window_seconds` | 近期完成吞吐的窗口，默认 300 秒 |
| `parsing.recent_completed_pages` | 窗口内成功完成的页数 |
| `parsing.recent_pages_per_minute` | 整个 worker 池的近期墙钟吞吐，页/分钟 |
| `workers` | 最近活跃的 worker 进程数 |

`workers` 统计向协调端领取或续租的解析子进程，不统计容器、监督进程或 SDK 辅助进程。四卡默认配置全部预热后应有 32 个；退出进程的记录在两个租约周期内仍可能暂时计入。

## 平均速度的口径

平均秒/页 = 成功样本执行秒数之和 ÷ 同批任务页数之和。例如两份文档分别为 2 页/6 秒、8 页/4 秒，结果是 10 秒/10 页 = 1 秒/页，不是对每份文档的秒/页简单平均。

`worker.parse` 从下载完成后开始计时，包含 SDK 解析、临时文件处理和 Markdown 渲染，排除队列、输入下载与结果上传。失败尝试不进入成功解析速度统计。没有有效计时时平均速度返回 `null`；缺少计时的成功任务仍计入完成页数，但不混入速度分母。

近期页/分钟 = 窗口内完成页数 ÷ 窗口分钟数，包含整个 worker 池的并发效果和空闲时间。例如 300 秒内完成 120 页就是 24 页/分钟。它不能与单份执行的平均页/秒直接比较。如果任务保留期短于 300 秒，窗口同步缩短，并在响应中给出。

累计值保存在 SQLite，重启和终态任务清理不清零；从本版本开始采集，不反推历史执行耗时。缓存命中、内容合并和同一租约的重复完成确认不会增加执行页数。近期窗口从仍保留的完成记录计算。

## Prometheus / Grafana

可直接运行的双容器部署、数据源 provisioning、Grafana 面板和告警规则位于 [monitoring/](../monitoring/README.md)。

| 指标 | 用途 |
| --- | --- |
| `mineru_tasks{state="queued"}` | 全局等待积压 |
| `mineru_tasks{state="running"}` | 执行中任务数 |
| `mineru_oldest_wait_seconds` | 最老任务的排队时间 |
| `mineru_pending_pages` | 全局在途页数 |
| `mineru_workers` | 最近活跃 worker 进程数 |
| `mineru_processed_pages_total{tier}` | 成功执行并发布的累计页数 |
| `mineru_measured_pages_total{tier}` | 有配对执行计时的累计页数 |
| `mineru_processing_seconds_total{tier}` | 配对样本的累计执行秒数 |
| `mineru_parse_seconds_per_page{tier}` | 按档位区分的累计平均秒/页 |
| `mineru_stage_seconds{tier,stage}` | 队列、worker、SDK 等阶段耗时直方图 |

最近五分钟按页加权的平均解析秒/页：

```promql
sum(rate(mineru_processing_seconds_total[5m]))
/
sum(rate(mineru_measured_pages_total[5m]))
```

最近五分钟整个 worker 池的完成吞吐，单位页/分钟：

```promql
60 * sum(rate(mineru_processed_pages_total[5m]))
```

没有完成样本时，速度查询可能没有结果或分母为零，仪表盘应显示暂无样本。比较解析档位时按 `tier` 分组，避免文档分布或档位变化被误认为性能变化。

多个 API 进程共享数据库，导出的指标都是全局值。采集时选择一个协调端入口，不要把多个入口的相同全局值直接求和。

积压与最老等待时间持续增长，而执行秒/页基本稳定，说明到达速度超过处理速度；若执行秒/页也变慢，可继续看 `worker.parse`、`sdk.total`、各 `pdf.*` 阶段，以及网关和 vLLM 指标。SDK 阶段可能嵌套，不能直接相加。
