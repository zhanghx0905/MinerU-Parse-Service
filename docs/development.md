# 代码结构与本地开发

服务 1.4.0 使用 MinerU 4.0.12、DocVortex 0.6.5、worker 协议 3 和 SQLite schema 4。HTTP 请求统一进入持久队列，API 进程只负责协调与本地文件，GPU 子进程串行执行文档解析。

## 代码入口

| 路径 | 职责 |
| --- | --- |
| [main.py](../app/main.py) | 装配 API，持有数据库的单进程锁，管理维护任务 |
| [config.py](../app/config.py) | 环境变量与配置校验 |
| [models.py](../app/models.py) | 解析值对象、调度错误和 worker 协议常量 |
| [http/parsing.py](../app/http/parsing.py) | PDF 校验、FastGPT 表单、同步等待和异步提交 |
| [http/jobs.py](../app/http/jobs.py) | 状态、结果、健康、指标和 worker 协调接口 |
| [http/middleware.py](../app/http/middleware.py) | 读请求体前鉴权，上传、同步等待与传输的进程内名额 |
| [http/errors.py](../app/http/errors.py) | HTTP 状态与稳定诊断响应 |
| [queue/service.py](../app/queue/service.py) | 指纹、输入存储、提交、等待与文件清理 |
| [queue/store.py](../app/queue/store.py) | 容量、优先级、租约、重试和完成统计的原子 SQL |
| [queue/database.py](../app/queue/database.py) | WAL、短事务、版本迁移、API 进程锁与在线备份 |
| [queue/metrics.py](../app/queue/metrics.py) | 持久计数、队列快照与 Prometheus 导出 |
| [parser/engine.py](../app/parser/engine.py) | 预热、临时文件和一次 SDK 调用；模型缓存由 SDK 管理 |
| [parser/rendering.py](../app/parser/rendering.py) | 消费本次文档结构，应用输出开关并渲染 Markdown |
| [parser/timing.py](../app/parser/timing.py) | SDK 诊断阶段采集 |
| [worker.py](../app/worker.py) | 单文档执行、独立续租、spawn 进程监督与重启 |
| [gateway.py](../app/gateway.py) | vLLM 副本选择、并发控制与代理 |
| [storage.py](../app/storage.py) | 本地文件键校验、原子写入与删除 |

`models.py` 只依赖标准库。HTTP 解释外部输入，QueueStore 在同一事务中读取和改变状态，文件和模型操作在事务外执行。中间件限额仅属于正在运行的 HTTP 请求；`uploads` 表单独保存输入文件恢复记录。

解析结果只生成一次 Markdown，渲染器取得该结果的所有权，允许就地过滤。需要从同一结果生成其他输出的调用方应在交出所有权前自行复制。原始页数在过滤前记录。

增加输出选项时，修改 `ParseOptions`、HTTP 表单、SDK/渲染规则及测试。指纹包含整个选项对象；持久任务保存选项并传给 worker。输出语义变化时修改 `OUTPUT_REVISION`；领取载荷的必需字段变化时修改 worker 协议版本；数据结构变化时增加事务迁移。目录调整不改变协议或输出版本。

## 本地环境

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python scripts/build_sqlite.py
LD_PRELOAD="$PWD/artifacts/sqlite/lib/libsqlite3.so.0" .venv/bin/python -m pytest -q
.venv/bin/ruff check app tests scripts
.venv/bin/ruff format --check app tests scripts
```

SQLite 构建工具固定源码与校验和，产物位于 `artifacts/sqlite/`，API 镜像内置同版本运行库。测试输入由 `tests/helpers.py` 和冒烟工具生成，测试文件不相互导入。安装 `.[gpu]` 用于匹配的 GPU 环境；完整运行依赖模型和 vLLM，部署步骤见[运维说明](scaling.md)。

## 验证范围

| 范围 | 测试 |
| --- | --- |
| PDF、响应、单 API 排他及请求名额释放 | [test_api.py](../tests/test_api.py) |
| FastGPT、同步超时、在途复用和诊断码 | [test_fastgpt.py](../tests/test_fastgpt.py) |
| 输出开关组合、正文、图片和原始页数 | [test_rendering.py](../tests/test_rendering.py)、[test_sdk.py](../tests/test_sdk.py) |
| 并发领取、租约、保留期复用与文件回收 | [test_queue.py](../tests/test_queue.py)、[test_storage.py](../tests/test_storage.py) |
| 优先级、VIP 与环境配置 | [test_scheduling.py](../tests/test_scheduling.py) |
| 独立的 schema 1/2/3 旧库升级到 4、失败回滚与未知版本拒绝 | [test_migrations.py](../tests/test_migrations.py) |
| HTTP worker、失效租约退出和网关失败释放 | [test_worker.py](../tests/test_worker.py) |
| 真实 spawn、独立 PID、崩溃替换和优雅停止 | [test_worker_pool.py](../tests/test_worker_pool.py) |
| 持久计数、加权速度与清理后保留统计 | [test_observability.py](../tests/test_observability.py) |

自动化使用真实 SQLite 和 HTTP 协议。SDK 的 flash 测试不需要下载模型；GPU 故障隔离测试使用阻塞和失效替身。真实 GPU 模型复用、目标负载和端到端冒烟需在匹配运行环境中验证。现有输出检查不是带标注集的 OCR、公式和表格精度评估。

部署配置检查：

```bash
docker compose config --quiet
docker compose -f compose.yaml -f compose.scale.yaml config --quiet
docker compose -f compose.yaml -f compose.scale.yaml -f compose.quad.yaml config --quiet
.venv/bin/python scripts/smoke_test.py http://127.0.0.1:3888
```

启用业务鉴权时，冒烟与压测使用环境变量 `PDF_API_KEY`，请求头由工具添加，密钥不写入报告。性能测量统一使用 [load_test.py](../scripts/load_test.py)，方法与历史观察见[性能说明](benchmark.md)。

`artifacts/` 保存忽略版本控制的生成产物，当前需要保留 SQLite 运行库与部署清单。模型、业务文件、密钥、镜像归档和一次性采样不提交到仓库。历史基准保留在 `docs/archive/`，不作为当前容量承诺。
