# SDFastGPT PDF 接口契约

本服务支持普通 `customPdfParse.url` 和 `DOCUMENT_PARSE_PROVIDER=sangfor` 的 PDF 调用。将 URL 配成可访问的 `http://服务地址:3888/v2/parse/file`；Sangfor 接管扩展名只配置 `pdf`，本服务不接收 Office 文档。

## 请求和结果

```bash
curl --fail-with-body --max-time 600 \
  -F 'file=@example.pdf' \
  -F 'keep_header_footer=false' \
  -F 'keep_appendix=true' \
  -F 'image_analysis=false' \
  -F 'chart_analysis=false' \
  'http://127.0.0.1:3888/v2/parse/file'
```

这四个字段是 multipart 表单字段。`disable_cache`、`encode_img`、`tier` 保持原有 query 参数形式。`POST /jobs` 接受同样的表单和 query 参数。

| 字段 | 默认值 | 输出规则 |
| --- | --- | --- |
| `keep_header_footer` | false | 保留或删除 SDK 标记的页眉、页脚及页码 |
| `keep_appendix` | true | 关闭时移除已识别标题以“附录 / 附錄 / Appendix / Appendices”开头的章节，支持数字编号；到下一同级或更高层级的非附录标题恢复正文 |
| `image_analysis` | false | 保留或清除图片及印章块的识别正文，保留原图资源和图注 |
| `chart_analysis` | false | 保留或清除图表块的结构化识别正文，保留原图资源和图注 |
| `encode_img` | true | 独立控制图片资源；关闭时保留其他开关允许的文字、表格和公式 |

这些规则作用于 SDK 的结构化结果，不靠替换 Markdown 猜测章节。附录过滤依赖 SDK 的标题识别与层级；未识别成标题的内容不会按正文关键词删除。图片、图表识别是否产生内容仍受解析档位和模型能力约束，开关不自动提升档位。

成功响应为 HTTP 200：

```json
{"success":true,"message":"","markdown":"# Example","pages":2,"duration":1.23}
```

`pages` 始终为原 PDF 页数，过滤附录不会减少计费页数。明确过滤后没有剩余内容时，允许空字符串 Markdown。图片以 data URI 内嵌，FastGPT 自行转存；调用端未提供图片转存配置时会删除 base64 图片，这是 FastGPT 的既有行为。

所有输出选项参与缓存和在途合并，默认值显式传入或省略具有相同语义。

## 错误响应

失败响应保留非 2xx HTTP 状态，并统一提供 `success=false`、`error`、`message`、`detail`、`statusText`。验证错误也使用这一格式。

| `statusText` | 场景 |
| --- | --- |
| `pdfEncrypted` | 无法使用空密码打开的加密 PDF |
| `pdfCorrupted` | 错误签名、损坏或无法读取的 PDF |
| `unsupportedParseFileType` | 非 PDF 文件名 |
| `invalidParseFile` | 空文档、参数错误、大小/页数/工作量超限、幂等输入冲突 |
| `pdfParseFailed` | 解析失败、等待超时、容量不足或基础设施故障 |

容量不足仍返回 503 和 `Retry-After`。worker 对下载、完成提交的临时 429/502/503/504 和传输异常最多尝试三次，再进入任务重试；FastGPT 自身是否退避取决于调用端，服务端不能替它处理重试头。

## 超时与幂等

服务默认 `REQUEST_TIMEOUT=540` 秒，为 SDFastGPT 默认 600 秒 HTTP 超时保留余量。同步等待使用扣除上传、校验和提交耗时后的剩余预算；底层校验/存储操作本身不具备强制终止线程的能力。调整调用端超时时，应同步设置服务端预算并给网关、网络和响应传输留余量。

已有 `.env` 不会自动改写。升级前将旧的 `REQUEST_TIMEOUT=6000` 改成合适的值；`WORKER_MAX_RUN_SECONDS` 独立保持 6000 秒，异步任务可以继续处理长文档。

队列模式下，两种提交接口均支持 `Idempotency-Key`。同步等待超时返回 504，包含任务 `id / status_url / result_url`、`Location` 和 `X-Task-ID`；已提交任务继续执行。SDFastGPT 现有同步调用不会自动查询这些地址，需要其调用者另外处理恢复。`inline` 模式不提供持久幂等键，传入时明确返回 503。

## 凭据与内部地址

设置本服务的 `PDF_API_KEY` 后，将相同值填入 SDFastGPT 的 `customPdfParse.key`。公开解析、提交、状态、结果及 `/stats` 接口会校验 Bearer；`/health` 与 `/metrics` 保持可读。留空表示公开接口不启用鉴权。

`WORKER_API_TOKEN` 仅用于内部 worker，不能替代公开接口密钥。鉴权在读取请求体、预留上传名额之前执行。

普通自定义 PDF 路径使用 SDFastGPT 的 SSRF 检查客户端：生产环境会拒绝 loopback，启用 `CHECK_INTERNAL_IP` 还会拒绝私网。Sangfor 路径支持管理员配置的内部服务地址。部署时需按所选调用路径配置可访问且被调用端允许的地址。

## 从 1.1 升级

1. 暂停新提交，排空在途任务，备份 SQLite 和对象存储，再停止旧 worker 与 API。
2. 构建并启动 1.2 API。首次打开数据库时将 schema 1 在事务中迁移到 schema 2，保留任务、结果、租约和幂等映射；失败时回滚迁移。
3. 旧任务保存原有图片/图表识别策略；清除旧内容复用标记。旧幂等映射保留，输出版本变化后重复使用旧键可能返回 409，新逻辑请求使用新键。
4. 启动同版本 worker。协议版本 2 必须传递解析选项；旧 worker 领取会返回 409，避免静默忽略配置。

进程入口现为 `python -m app.interfaces.worker` 和 `python -m app.cli.queue`；网关为 `app.interfaces.gateway:app`。Compose 已更新这些入口。迁移后回退旧镜像需要恢复升级前备份。
