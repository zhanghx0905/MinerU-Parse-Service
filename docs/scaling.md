# 部署、配置与运维

本页是部署命令和环境配置的入口。任务调度细节见[调度说明](scheduling.md)，接口参数见[HTTP 契约](fastgpt.md)。

## 部署拓扑

| 模式 | Compose 文件 | worker / vLLM 容器 | 默认解析进程 / VIP |
| --- | --- | --- | --- |
| 单卡 | `compose.yaml` | 1 / 1 | 2 / 0 |
| 双卡 | 再加 `compose.scale.yaml` | 2 / 2 | 4 / 0 |
| 四卡 | 再加 `compose.quad.yaml` | 4 / 4 | 32 / 2 |

所有模式另有 1 个 CPU API 和 1 个 VLM 网关。每个 worker 容器启动一个轻量监督进程，再用 `spawn` 启动解析子进程；每个子进程同时处理一份 PDF，独立持有本地模型和 SDK 锁。vLLM 服务按 GPU 运行，由这些解析进程共享。

SQLite 和对象存储只由 API 访问。数据库必须放在协调主机本地持久磁盘，不支持多主机通过 NFS 打开同一 WAL 文件。解析 worker 可以跨主机扩容；当前没有控制平面自动故障转移或自动扩缩容。

## 初始化与启动

```bash
python3 scripts/init_env.py
# 编辑 .env，设置 GPU、端口、业务凭据等。已有配置不会被脚本改写。
docker compose build
docker compose run --rm --no-deps --entrypoint mineru-kit mineru-worker \
  models download --tier standard --small-backend torch --vlm-engine vllm -s modelscope
docker compose up -d
```

四卡启动与检查：

```bash
docker compose -f compose.yaml -f compose.scale.yaml -f compose.quad.yaml build
docker compose -f compose.yaml -f compose.scale.yaml -f compose.quad.yaml up -d
docker compose -f compose.yaml -f compose.scale.yaml -f compose.quad.yaml ps
```

使用同一组 `-f` 参数执行启动、配置检查和维护。四卡模式每个 worker 服务保持一个容器；额外使用 `--scale` 会同时放大解析进程数和 VIP 数。

## 配置

配置只在进程启动时从环境变量读取，Compose 从 `.env` 注入。数据库保存任务和实际占用，不保存容量上限。修改 `.env` 后执行对应 Compose 配置的 `up -d` 重建受影响服务；`restart` 不会重新注入环境变量。

### 解析与 GPU

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `GPU_ID` / `GPU_ID_B` / `GPU_ID_C` / `GPU_ID_D` | 0 / 1 / 2 / 3 | GPU 设备编号 |
| `PARSE_WORKERS` | 2 | 单卡、双卡模式每个 worker 容器的解析进程数 |
| `WORKERS_PER_GPU` | 8 | 四卡模式覆盖每卡解析进程数 |
| `PRIORITY_WORKERS` | 0 | 单卡、双卡模式每容器的高优专用进程数，包含在 `PARSE_WORKERS` 中 |
| `PARSE_TIER` | standard | 默认解析档位 |
| `PRELOAD_MODELS` | true | 各解析子进程启动后先预热 |
| `MINERU_CPU_NUM_THREADS` | 2 | 每个解析进程的 CPU 算子线程上限 |
| `MINERU_MODEL_VLM_MAX_CONCURRENCY` | 8 | 每个解析进程的 VLM 客户端并发 |
| `GPU_MEMORY_UTILIZATION` | 0.35 | vLLM 显存比例，本地模型还需额外显存 |
| `MODEL_REVISION` | MinerU2.5-Pro-2605-1.2B | 参与指纹与 worker 校验的模型标识 |
| `VLLM_IMAGE` | vllm/vllm-openai:v0.29.0 | GPU 基础与推理镜像 |
| `MINERU_MODEL_SOURCE` / `SMALL_BACKEND` | modelscope / torch | 模型来源与本地模型后端 |

四卡配置的高优专用进程数固定为 1/1/0/0。默认每卡 8 个进程，共 30 个通用进程和 2 个 VIP；改变 `WORKERS_PER_GPU` 不改变 VIP 总数。通用进程先领高优再领普通，高优专用进程没有高优任务时空闲，不中断正在执行的普通任务。

增加进程会增加模型副本、内存、显存和请求并发。根据实际吞吐、排队和显存峰值调整；并发进程数不等于吞吐倍数。当前 API 和 worker 的 `/tmp` 使用容器磁盘及 Linux 页缓存，避免固定容量内存盘在并发渲染位图时耗尽。

### API 与容量

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `API_PORT` / `API_PROCESSES` | 3888 / 1 | 对外端口与 API 进程数 |
| `PDF_API_KEY` | 空 | 业务接口的可选 Bearer 密钥 |
| `WORKER_API_TOKEN` | 初始化脚本生成 | 内部 worker 接口凭据，队列模式必填 |
| `MAX_PENDING` | 10000 | 全局 queued + running 总量 |
| `QUEUE_MAX_UPLOADS` | 64 | 上传、校验、输入存储中的并发请求 |
| `QUEUE_MAX_SYNC_WAITERS` | 64 | 同步 HTTP 在途请求 |
| `QUEUE_MAX_TRANSFERS` | 64 | 结果下载及 worker 文件传输并发 |
| `MAX_UPLOAD_BYTES` / `MAX_PAGES` | 52428800 / 1000 | 单份 PDF 大小、页数上限，超限返回 413 |
| `REQUEST_TIMEOUT` | 540 | 同步请求总等待预算，不取消持久任务 |
| `QUEUE_UPLOAD_SECONDS` / `QUEUE_TRANSFER_SECONDS` | 120 / 120 | 接收请求体与文件传输期限 |
| `QUEUE_MAX_QUEUE_SECONDS` | 0 | 0 表示排队不过期；正数才启用队列超时 |
| `QUEUE_MAX_ATTEMPTS` | 3 | 最大执行次数，包含首次 |
| `QUEUE_LEASE_SECONDS` / `QUEUE_HEARTBEAT_SECONDS` | 60 / 10 | 租约与续期间隔；心跳小于租约的一半 |
| `WORKER_MAX_RUN_SECONDS` | 6000 | 单个执行尝试的硬期限，超时退出该解析进程 |
| `QUEUE_POLL_SECONDS` | 0.25 | 空闲领取与结果轮询间隔 |
| `QUEUE_RETENTION_SECONDS` | 86400 | 终态任务、成功结果及失败输入保留期 |
| `QUEUE_MAX_RESULT_BYTES` | 67108864 | 单份 Markdown 结果上限 |
| `CACHE_BYTES` / `CACHE_TTL` | 134217728 / 3600 | 可复用结果的字节预算与有效期 |

上传名额在输入持久化后释放，与队列积压量分开计数。降低容量不删除已有任务，只暂停超过上限时的新建任务。多个 API 进程使用同一份配置并一起重启。

### 文件存储与远端 worker

Compose 的 `QUEUE_DATABASE` 为 `/data/queue.sqlite3`，`OBJECT_DIR` 为 `/data/objects`，使用 `queue-data` 持久卷。直接运行 Python 时默认分别为 `data/queue.sqlite3`、`data/objects`。

`OBJECT_STORAGE=local` 使用本地目录；设置为 `s3` 时配置 `S3_BUCKET`、可选 `S3_ENDPOINT_URL` 和标准 AWS 凭据。只有 API 访问对象存储，worker 始终经认证 HTTP 下载输入和提交结果。

`MODEL_DIR` 未设置时使用 Compose 的 `models` 命名卷；也可指定模型所在的宿主机目录。远端 worker 需要 `WORKER_API_URL`、`WORKER_API_TOKEN`、模型目录、GPU 和 VLM 服务地址，并保持 MinerU 版本与 `MODEL_REVISION` 一致。直接运行的入口是 `python -m app.interfaces.worker`。

## 更新与停止

代码更新先构建镜像，再重建 API 和 worker。配置更新仅重建受影响服务。长文档维护前先暂停上游新提交，等待 queued / running 归零；worker 默认停止宽限为 120 秒，API 为 30 秒，必要时通过 Compose `stop --timeout` 延长。

监督进程收到停止信号后通知所有解析子进程，让它们完成当前文档再退出。超过停止宽限会被终止，未完成任务依靠租约恢复。信号与领取有并发窗口，当前没有全局排空或取消接口。

### 旧版本升级

1. 暂停提交、排空任务，备份数据库与对象文件，停止旧 API 和全部 worker。
2. 构建并启动同版本 API / worker。schema 1/2 自动事务迁移至 3，任务、结果、租约和幂等映射保留；旧任务默认普通优先级。
3. 迁移删除旧 `limits` 表和成本、短文档调度字段。schema 1 保留原解析选项并清除旧内容复用标记，schema 2 保留已有选项与缓存资格。
4. 更新旧 `.env` 的容量和超时值，删除 `QUEUE_MAX_WORK`；旧值不会自动覆盖。worker 协议为 3，旧 worker 会被拒绝。
5. 预热后用实际 PDF 验证接口，再恢复流量。迁移后回退需恢复升级前备份。

模型或解析器升级时同步更新权重、API、worker 和 `MODEL_REVISION`，先排空旧任务。服务校验版本标识，不自动为旧任务挑选旧模型。

## 备份、恢复和清理

在线数据库备份：

```bash
docker compose exec mineru-api python -m app.cli.queue \
  --database /data/queue.sqlite3 backup /data/backup.sqlite3
```

四卡部署使用相同三份 Compose 文件执行该命令。备份命令只包含 SQLite，不包含 PDF、Markdown、模型或凭据；不要单独复制正在变化的数据库主文件。

需要联合恢复点时，暂停提交并排空任务，停止全部 worker 和 API，再在维护容器中运行相同备份命令，并复制关联对象。停机备份示例：

```bash
docker compose run --rm --no-deps --entrypoint python mineru-api \
  -m app.cli.queue --database /data/queue.sqlite3 backup /data/backup.sqlite3
```

对象备份期间保持 API 停止，避免后台清理改变文件集合。恢复时将数据库与对应对象一起恢复，使用匹配的模型和配置启动。`docker compose down` 保留命名卷，`down -v` 删除数据；主机或磁盘损坏的自动恢复尚未实现。

后台按保留期清理任务和文件，删除失败保留垃圾记录重试。缓存预算只限制复用资格，不限制全部保留结果或总磁盘用量；仍需监控容量。业务失败由调用方处理，当前没有原 ID 手动重排接口。故障码与文件清理边界见[调度说明](scheduling.md)。
