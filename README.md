# MinerU PDF API

基于 MinerU 4.0.12 的 PDF 转 Markdown 服务，当前版本 **1.4.0**。接收 PDF，异步解析并提供结果查询，也支持 FastGPT 同步接口；切块、向量化和 RAG 入库由调用方负责。

单进程 CPU API 使用本地 SQLite WAL 保存任务，PDF 和 Markdown 保存在本地持久卷。GPU worker 容器通过 HTTP 领取任务，每个解析子进程独立加载本地模型，共享外部 vLLM 推理服务。配置统一来自环境变量，Compose 从 `.env` 注入，数据库不保存容量配置。

## 快速启动

需要 Docker Compose 2.24.4+、NVIDIA Container Toolkit、兼容 CUDA 13.0 的驱动和本地持久磁盘。API 镜像内置 SQLite 3.51.3。

```bash
python3 scripts/init_env.py
# 修改 .env 中的 GPU_ID、API_PORT、PDF_API_KEY 等配置。
# WORKER_API_TOKEN 由脚本生成；已有配置会保留。
docker compose build

docker compose run --rm --no-deps --entrypoint mineru-kit mineru-worker \
  models download --tier standard --small-backend torch --vlm-engine vllm -s modelscope

docker compose up -d
curl http://127.0.0.1:3888/health
```

首次下载模型和预热需要时间。`model_ready` 表示近期有解析进程活动；使用实际 PDF 验证推理是否可用。启用 `PDF_API_KEY` 后，业务请求需要 `Authorization: Bearer <key>`。

四卡部署使用三份 Compose 文件，每卡默认 8 个解析进程，总共 30 个通用进程和 2 个 VIP 专用进程：

```bash
docker compose -f compose.yaml -f compose.scale.yaml -f compose.quad.yaml up -d
```

worker 容器数与解析进程数是两个维度。四卡配置共 10 个容器：1 个 API、1 个 VLM 网关、4 个 worker、4 个 vLLM；每个 worker 内有 8 个独立解析进程。设备与进程数配置、完整启动步骤见[部署与运维](docs/scaling.md)。

## 调用

推荐使用异步接口接收批量任务：

```bash
curl -F 'file=@example.pdf' \
  'http://127.0.0.1:3888/jobs?tier=standard&priority=0'
# HTTP 202 返回 id、status_url、result_url。
curl http://127.0.0.1:3888/jobs/REPLACE_WITH_JOB_ID
curl http://127.0.0.1:3888/jobs/REPLACE_WITH_JOB_ID/result
```

FastGPT 同步接口：

```bash
curl --max-time 600 -F 'file=@example.pdf' \
  'http://127.0.0.1:3888/v2/parse/file?tier=standard&encode_img=true'
```

`priority=1` 为高优任务，默认 `0`。同级按入队顺序处理；高优专用进程无高优任务时保持空闲。同步请求超时不会取消已经入队的任务。支持 `flash / basic / standard / advanced` 四个档位，完整参数、响应和错误规则见[HTTP 与 FastGPT 接口](docs/fastgpt.md)。

默认允许 10000 个在途任务、64 个并发上传，排队不过期。相同内容在结果保留期内复用，默认一天；`disable_cache=true` 始终提交新任务。单文件默认上限为 200 MiB、2000 页，超限返回 413。API 上传和 worker 渲染的临时文件使用容器磁盘及 Linux 页缓存，不挂载固定容量的 `/tmp` 内存盘。

## 文档与开发

文档按职责组织说明：

- [代码结构、开发和验证](docs/development.md)
- [部署、配置、升级和备份](docs/scaling.md)
- [调度、租约与故障处理](docs/scheduling.md)
- [指标口径](docs/observability.md)和[监控部署](monitoring/README.md)
- [性能观察与复现](docs/benchmark.md)

源码在 `app/`，回归测试在 `tests/`，可复用工具在 `scripts/`。生成文件统一放在忽略版本控制的 `artifacts/`，模型、业务文件、数据库、密钥和离线镜像不提交到仓库。
