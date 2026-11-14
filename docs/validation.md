# 开发与验证

当前版本为 1.3.1，使用 MinerU 4.0.12、worker 协议 3 和 SQLite schema 3。

## 本地环境

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test,s3]'
.venv/bin/python scripts/build_sqlite.py

LD_PRELOAD="$PWD/artifacts/sqlite/lib/libsqlite3.so.0" .venv/bin/python -m pytest -q
.venv/bin/ruff check app tests scripts
.venv/bin/ruff format --check app tests scripts
```

SQLite 构建工具固定源码版本与校验和，输出到 `artifacts/sqlite/`；该运行库用于本地队列测试，API 镜像已内置。删除后需重新构建。源码样本由测试生成，不需要在仓库保留 PDF。

## 自动化覆盖

最近一次检查为 **120 项测试通过**，Ruff 与格式检查通过。保留 5 条 TestClient 超时参数弃用提示，不影响生产 HTTP 客户端。

| 范围 | 主要测试 |
| --- | --- |
| PDF 校验、同步响应、超时与图片选项 | [test_api.py](../tests/test_api.py)、[test_sdk.py](../tests/test_sdk.py) |
| FastGPT 表单、输出过滤与诊断码 | [test_fastgpt.py](../tests/test_fastgpt.py)、[test_rendering.py](../tests/test_rendering.py) |
| 并发领取、去重、幂等、租约与文件保留 | [test_queue.py](../tests/test_queue.py)、[test_storage.py](../tests/test_storage.py) |
| 业务优先级、VIP 预留、环境配置升降 | [test_scheduling.py](../tests/test_scheduling.py) |
| schema 1/2→3 保留任务、租约和结果，迁移失败回滚 | [test_migrations.py](../tests/test_migrations.py) |
| HTTP worker、失效租约退出、网关故障和分配 | [test_worker.py](../tests/test_worker.py) |
| 真实 spawn 独立 PID、VIP、子进程崩溃重启、优雅停止 | [test_worker_pool.py](../tests/test_worker_pool.py) |
| 持久计数、按页加权速度、重启与清理 | [test_observability.py](../tests/test_observability.py) |

队列容量还做过一次独立检查：接受 10000 条任务元数据，拒绝第 10001 条；模拟等待 8 小时并重新打开数据库后全部保留。此项不包含 PDF 上传或 GPU 执行，不计作吞吐基准。

## 部署配置与冒烟

初始化 `.env` 后检查不同拓扑：

```bash
docker compose config --quiet
docker compose -f compose.yaml -f compose.scale.yaml config --quiet
docker compose -f compose.yaml -f compose.scale.yaml -f compose.quad.yaml config --quiet
.venv/bin/python scripts/smoke_test.py http://127.0.0.1:3888
```

冒烟脚本生成图文 PDF，检查 HTTP、页数、正文、图片内嵌和缓存分离。示例不带业务密钥；启用鉴权时，调用客户端需增加对应请求头。

1.3.1 已在四卡部署中验证 32 个解析子进程启动、VIP 配置、真实 PDF 执行与外部健康接口。运行观察见[性能记录](benchmark.md)。超过 1000 页的合成 PDF 在入口返回 413，不进入解析队列。

并发渲染曾触发 `PDF render worker OSError: [Errno 28] No space left on device`，原因是固定容量的 `/tmp` 内存盘。移除后四卡观察未新增解析错误。失败输入现保留到终态保留期，服务端记录完整堆栈；业务方负责处理历史失败，不自动重新提交。

## 验证边界

自动化测试使用真实 SQLite 和 HTTP 协议；GPU 故障隔离主要用阻塞或失效执行替身验证，真实 SDK 测试包含不需要模型下载的 flash 路径。S3 使用模拟客户端验证，尚未完成真实对象存储故障演练。

现有 GPU 检查验证状态、页数、正文及图片，不是逐字 OCR、公式或表格的标注集精度评估。近期运行观察不能代替长期吞吐测试。主机掉电、磁盘损坏和跨节点控制平面切换尚未演练；历史 GPU 性能资料单独[归档](archive/benchmark-1.1.md)。
