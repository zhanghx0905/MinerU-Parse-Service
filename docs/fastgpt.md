# HTTP 与 FastGPT 接口

服务只接收 PDF，输出 Markdown、原始页数和耗时。图片以 data URI 内嵌；不提供 Office 转换、向量化或 RAG 入库。

## 业务接口

| 方法与路径 | 行为 |
| --- | --- |
| `POST /jobs` | 上传后返回 202、任务 ID、状态和结果地址 |
| `GET /jobs/{id}` | 查询 queued / running / succeeded / failed 状态 |
| `GET /jobs/{id}/result` | 未完成返回 202；成功返回解析结果；失败返回 409 |
| `POST /v2/parse/file` | 通过同一队列等待结果，同步成功返回 200 |
| `GET /health` | 存活与近期 worker 活动，不代表实时推理探测 |
| `GET /stats` | 全局队列和解析速度快照 |
| `GET /metrics` | Prometheus 指标 |

两种 POST 均使用 multipart 的 `file` 字段上传 PDF，接受相同参数。

| Query 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `tier` | `PARSE_TIER`，默认 standard | flash / basic / standard / advanced |
| `priority` | 0 | 0 普通，1 高优；复用在途任务时只提升、不降低优先级 |
| `encode_img` | true | 是否在 Markdown 中内嵌图片资源 |
| `disable_cache` | false | 跳过结果复用和在途去重，始终创建新任务 |

| Multipart 表单字段 | 默认值 | 输出规则 |
| --- | --- | --- |
| `keep_header_footer` | false | 删除或保留 SDK 标记的页眉、页脚及页码 |
| `keep_appendix` | true | false 时移除已识别的附录章节，到下一同级或更高层级的非附录标题恢复正文 |
| `image_analysis` | false | 清除或保留图片、印章块的识别正文；原图和图注由其他选项控制 |
| `chart_analysis` | false | 清除或保留图表块的结构化正文；原图和图注由其他选项控制 |

过滤作用于 SDK 结构化结果，附录边界依赖标题识别与层级。未识别成标题的内容不会按正文关键词删除。开关不会自动提升解析档位；`encode_img=false` 仍保留其他开关允许的正文、表格和公式。

## 异步任务与内容复用

```bash
curl --fail-with-body -F 'file=@example.pdf' \
  'http://127.0.0.1:3888/jobs?tier=standard&priority=1'
# 使用响应中的 status_url 和 result_url 查询。
```

相同内容、输出选项、解析器及模型版本默认合并在途任务，并在结果保留期内复用成功任务，默认保留一天。失败任务不参与复用，修复原因后直接重新提交。使用 `disable_cache=true` 强制新建任务，重复提交会产生不同 ID。

服务不读取客户端 `Idempotency-Key`。提交响应丢失时，相同内容的默认重试可复用任务；禁用复用后的重试可能创建多个任务。当前没有取消或原任务 ID 重新排队接口。

## 同步结果与超时

```bash
curl --fail-with-body --max-time 600 -F 'file=@example.pdf' \
  -F 'keep_header_footer=false' -F 'keep_appendix=true' \
  -F 'image_analysis=false' -F 'chart_analysis=false' \
  'http://127.0.0.1:3888/v2/parse/file?tier=standard&encode_img=true'
```

成功响应：

```json
{"success":true,"message":"","markdown":"# Example","pages":2,"duration":1.23}
```

`pages` 为原 PDF 页数；过滤附录不会减少它。过滤后允许空 Markdown。`duration` 单位为秒：同步接口计算服务端接收、校验、提交和等待的总时间；异步结果计算任务入队至完成的时间。

默认 `REQUEST_TIMEOUT=540` 秒，同步等待扣除上传、校验和提交耗时。超时返回 504，并附 `id / status_url / result_url`、`Location` 和 `X-Task-ID`；已经入队的任务继续执行。调用方需要自行查询结果，网络断开不代表任务未提交；默认重试可依靠内容指纹复用；`disable_cache=true` 的提交需自行处理重复任务。

## 鉴权与 FastGPT

设置 `PDF_API_KEY` 后，解析、提交、状态、结果和 `/stats` 请求需携带 `Authorization: Bearer <key>`。`/health` 和 `/metrics` 保持可读；密钥留空时公开业务接口不启用鉴权。

FastGPT 的 `customPdfParse.url` 指向 `/v2/parse/file`，`customPdfParse.key` 使用相同的 `PDF_API_KEY`。`DOCUMENT_PARSE_PROVIDER=sangfor` 路径只将 `pdf` 扩展名交给本服务。调用方的私网地址、SSRF 和图片转存策略需要在调用方配置；图片未转存时的保留行为也由调用方决定。

`WORKER_API_TOKEN` 仅保护 `/internal/` 的 worker 协调接口，与业务凭据独立。内部协议要求解析器版本、模型标识和 worker 协议一致，不供业务方提交解析请求。

## 错误响应

失败保持非 2xx 状态，统一提供 `success=false`、`message / error / detail / statusText`，不向业务接口暴露堆栈。

| HTTP | 场景 | 调用方处理 |
| --- | --- | --- |
| 400 | 非 PDF、损坏、加密或空文档 | 修正输入 |
| 401 | 凭据缺失或错误 | 使用对应接口的凭据 |
| 404 | 任务不存在或已清理 | 检查 ID 和保留期 |
| 409 | 读取失败任务的结果 | 处理解析失败，修正原因后重新提交 |
| 413 | 文件大小、页数超限 | 调整输入或服务配置；默认最多 200 MiB、2000 页 |
| 422 | 参数格式错误 | 修正参数 |
| 503 | 上传、同步等待、传输或队列容量满 | 按 `Retry-After` 退避 |
| 504 | 同步等待超时 | 使用响应中的任务地址继续查询 |

稳定诊断码包括 `pdfEncrypted`、`pdfCorrupted`、`unsupportedParseFileType`、`invalidParseFile`、`pdfParseFailed`。`PDF exceeds 2000 pages` 是入口 413 校验，不占用解析 worker；入队后的 `parse_error`、租约失效和存储错误见[调度与故障处理](scheduling.md#5-异常情况如何处理)。
