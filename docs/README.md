# 文档索引

当前实现为 **1.3.1**，worker 协议版本为 **3**，SQLite schema 为 **3**。文档描述当前行为，历史测量单独归档。

| 需求 | 文档 | 内容归属 |
| --- | --- | --- |
| 快速启动与调用示例 | [项目 README](../README.md) | 服务边界、最短使用步骤 |
| 阅读和修改代码 | [架构与代码入口](architecture.md) | 目录、依赖、修改约束 |
| 部署和调参数 | [部署与运维](scaling.md) | Compose 配置、环境变量、升级、备份 |
| 对接 HTTP / FastGPT | [接口契约](fastgpt.md) | 上传参数、任务查询、响应、鉴权、错误 |
| 理解执行与恢复 | [任务调度](scheduling.md) | 准入、优先级、进程池、租约、重试、文件生命周期 |
| 排查积压与速度 | [可观测性](observability.md) | `/stats` 与 Prometheus 指标口径 |
| 启动监控 | [监控 README](../monitoring/README.md) | Prometheus / Grafana 的部署和维护 |
| 开发和验证 | [验证说明](validation.md) | 安装、检查命令、测试范围与限制 |
| 评估吞吐 | [性能观察](benchmark.md) | 当前观察、压测方法、历史基准入口 |

## 目录和生成文件

| 路径 | 用途 |
| --- | --- |
| `app/` | 服务源码 |
| `tests/` | 自动化回归测试；样本由测试生成 |
| `scripts/` | 环境初始化、SQLite 构建、依赖安装、冒烟与压测工具 |
| `monitoring/` | 可部署的监控配置与仪表盘源码 |
| `docs/archive/` | 标注版本的历史性能资料，不作为当前容量依据 |
| `artifacts/` | 不入库的生成文件；本地保留 SQLite 运行库和最新部署清单 |
| `.venv/` | 本地开发依赖，按需安装，不入库 |

离线镜像归档、镜像解包目录、临时 PDF、观测数据库、日志、逐次采样、旧源码压缩包和一次性部署脚本用完即清理。需要复现时使用 `scripts/` 中的工具重新生成；SQLite 运行库由 `scripts/build_sqlite.py` 重建。
