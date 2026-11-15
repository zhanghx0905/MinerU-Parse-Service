# 部署、配置与运维

本页是部署命令和环境配置的入口。任务调度细节见[调度说明](scheduling.md)，接口参数见[HTTP 契约](fastgpt.md)。

## 部署拓扑

| 模式 | Compose 文件 | worker / vLLM 容器 | 默认解析进程 / VIP |
| --- | --- | --- | --- |
| 单卡 | `compose.yaml` | 1 / 1 | 2 / 0 |
| 双卡 | 再加 `compose.scale.yaml` | 2 / 2 | 4 / 0 |
| 四卡 | 再加 `compose.quad.yaml` | 4 / 4 | 32 / 2 |

所有模式另有 1 个 CPU API 和 1 个 VLM 网关。每个 worker 容器启动一个轻量监督进程，再用 `spawn` 启动解析子进程；每个子进程同时处理一份 PDF，独立持有本地模型和 SDK 锁。vLLM 服务按 GPU 运行，由这些解析进程共享。

SQLite 和本地对象目录只由唯一的 API 进程访问。启动第二个 API 打开同一数据库会被进程锁拒绝；Compose 固定 `--workers 1`。数据库必须放在协调主机本地持久磁盘，不支持多主机通过 NFS 打开同一 WAL 文件。解析 worker 可以跨主机扩容；当前没有控制平面自动故障转移或自动扩缩容。

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
| `API_PORT` | 3888 | 对外端口；API 固定一个进程 |
| `PDF_API_KEY` | 空 | 业务接口的可选 Bearer 密钥 |
| `WORKER_API_TOKEN` | 初始化脚本生成 | 内部 worker 接口凭据，队列模式必填 |
| `MAX_PENDING` | 10000 | 全局 queued + running 总量 |
| `QUEUE_MAX_UPLOADS` | 64 | 上传、校验、输入存储中的并发请求 |
| `QUEUE_MAX_SYNC_WAITERS` | 64 | 同步 HTTP 在途请求 |
| `QUEUE_MAX_TRANSFERS` | 64 | 结果下载及 worker 文件传输并发 |
| `MAX_UPLOAD_BYTES` / `MAX_PAGES` | 209715200 / 2000 | 单份 PDF 大小、页数上限，超限返回 413 |
| `REQUEST_TIMEOUT` | 540 | 同步请求总等待预算，不取消持久任务 |
| `QUEUE_UPLOAD_SECONDS` / `QUEUE_TRANSFER_SECONDS` | 120 / 120 | 接收请求体与文件传输期限 |
| `QUEUE_MAX_QUEUE_SECONDS` | 0 | 0 表示排队不过期；正数才启用队列超时 |
| `QUEUE_MAX_ATTEMPTS` | 3 | 最大执行次数，包含首次 |
| `QUEUE_LEASE_SECONDS` / `QUEUE_HEARTBEAT_SECONDS` | 60 / 10 | 租约与续期间隔；心跳小于租约的一半 |
| `WORKER_MAX_RUN_SECONDS` | 6000 | 单个执行尝试的硬期限，超时退出该解析进程 |
| `QUEUE_POLL_SECONDS` | 0.25 | 空闲领取与结果轮询间隔 |
| `QUEUE_RETENTION_SECONDS` | 86400 | 终态任务、成功结果及失败输入保留期 |
| `QUEUE_MAX_RESULT_BYTES` | 67108864 | 单份 Markdown 结果上限 |

上传名额在输入持久化后释放，与队列积压量分开计数。降低容量不删除已有任务，只暂停超过上限时的新建任务。请求名额在 API 内存中计数，连接结束或进程退出后释放；任务容量仍由 SQLite 原子检查。成功结果在保留期内可复用，不设独立的缓存字节预算或 TTL。

### 文件存储与远端 worker

Compose 的 `QUEUE_DATABASE` 为 `/data/queue.sqlite3`，`OBJECT_DIR` 为 `/data/objects`，使用 `queue-data` 持久卷。直接运行 Python 时默认分别为 `data/queue.sqlite3`、`data/objects`。

本地文件采用同目录临时文件、同步落盘和原子替换。只有 API 访问对象目录；远端 worker 经认证 HTTP 下载输入和提交结果。

`MODEL_DIR` 未设置时使用 Compose 的 `models` 命名卷；也可指定模型所在的宿主机目录。远端 worker 需要 `WORKER_API_URL`、`WORKER_API_TOKEN`、模型目录、GPU 和 VLM 服务地址，并保持 MinerU 版本与 `MODEL_REVISION` 一致。直接运行的入口是 `python -m app.worker`。

## 更新与停止

代码更新先构建镜像，再重建 API 和 worker。配置更新仅重建受影响服务。长文档维护前先暂停上游新提交，等待 queued / running 归零；worker 默认停止宽限为 120 秒，API 为 30 秒，必要时通过 Compose `stop --timeout` 延长。

监督进程收到停止信号后通知所有解析子进程，让它们完成当前文档再退出。超过停止宽限会被终止，未完成任务依靠租约恢复。信号与领取有并发窗口，当前没有全局排空或取消接口。

### 旧版本升级

1. 暂停提交、排空任务，备份数据库与对象文件，停止旧 API 和全部 worker。
2. 构建并启动同版本 API / worker。schema 1/2/3 自动事务迁移至 4，保留任务、结果、租约、上传恢复记录、垃圾清单和累计统计；schema 1 保留原解析选项并取消旧结果的复用资格。
3. 迁移删除客户端幂等表、HTTP 等待/传输名额表、旧容量表，以及不再消费的任务指纹副本、开始时间、原始阶段计时和缓存预算字段。汇总阶段直方图与成功速度统计保留。迁移失败回滚，未知 schema 拒绝启动。
4. 删除旧 `.env` 中的 `SERVICE_MODE`、`API_PROCESSES`、`CACHE_BYTES`、`CACHE_TTL`、`OBJECT_STORAGE`、S3/AWS 配置和旧 `QUEUE_MAX_WORK`。当前只支持本地存储、单 API；worker 入口改为 `python -m app.worker`，协议仍为 3。
5. 预热后用实际 PDF 验证接口，再恢复流量。结果复用期统一为 `QUEUE_RETENTION_SECONDS`，默认一天；需要重算时使用 `disable_cache=true`。迁移后回退需停止新服务并恢复升级前备份。

模型或解析器升级时同步更新权重、API、worker 和 `MODEL_REVISION`，先排空旧任务。服务校验版本标识，不自动为旧任务挑选旧模型。

## 备份、恢复和清理

在线数据库备份：

```bash
docker compose exec mineru-api python -m app.queue.database \
  --database /data/queue.sqlite3 /data/backup.sqlite3
```

四卡部署使用相同三份 Compose 文件执行该命令。备份命令只包含 SQLite，不包含 PDF、Markdown、模型或凭据；不要单独复制正在变化的数据库主文件。

需要联合恢复点时，暂停提交并排空任务，停止全部 worker 和 API，再在维护容器中运行相同备份命令，并复制关联对象。停机备份示例：

```bash
docker compose run --rm --no-deps --entrypoint python mineru-api \
  -m app.queue.database --database /data/queue.sqlite3 /data/backup.sqlite3
```

对象备份期间保持 API 停止，避免后台清理改变文件集合。恢复时将数据库与对应对象一起恢复，使用匹配的模型和配置启动。`docker compose down` 保留命名卷，`down -v` 删除数据；主机或磁盘损坏的自动恢复尚未实现。

后台按保留期清理任务和文件，删除失败保留垃圾记录重试。任务与文件按保留期清理，未设置总存储字节配额；仍需监控磁盘容量。业务失败由调用方处理，当前没有原 ID 手动重排接口。故障码与文件清理边界见[调度说明](scheduling.md)。
