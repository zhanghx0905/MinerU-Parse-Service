# 验证说明

## 1.2 适配检查

108 项测试通过，Ruff 检查和格式检查通过，单卡与双卡 Compose 配置校验通过。测试有 5 条 TestClient 超时参数弃用提示，不影响生产 HTTP 客户端。

新增 FastGPT multipart 契约、四个开关的独立输出组合、缓存隔离、重启后的选项保留、worker 协议版本拒绝、诊断码、独立公开凭据、传输背压重试以及 schema 1→2 迁移测试。可观测性测试覆盖积压、按页加权速度、重复/失效租约与缓存不重复计数、重启/清理后保留累计统计，以及近期吞吐窗口。结构化输出测试使用真实 SDK 类型与渲染器，GPU 执行使用替身；真实 SDK 的 flash 原生文本路径继续单独验证。

另外使用 SDFastGPT 已安装的 axios、form-data 和 Zod 依赖，经本地真实 HTTP 调用真实 MinerU flash：中文文件名、四个 multipart 开关、页数/正文响应、图片内嵌/省略、Bearer 鉴权和损坏文件诊断码检查通过。另外完成持久队列链路：同一 Node 调用端 → HTTP API → SQLite → 独立 HTTP worker → 真实 MinerU flash → `/stats` 和 `/metrics`，并验证缓存命中不会增加执行页数。这不是完整 SDFastGPT 应用或 GPU 档位的联调。

1.2 已在双 L40 上完成 13 个真实 GPU 任务、54 页验证，四个解析档位、多页 PDF、扫描件、图片开关与缓存路径通过，全部首次执行成功。四个 worker 均实际领取并完成任务，两路推理副本均处理了请求。租约使用 120 秒、心跳 20 秒；隔离验证将真实解析任务延长到约 125 秒，六次续租后首次执行成功。该人工延迟样本不计入生产速度统计。

部署后的 FastGPT 主服务和子路径服务均使用各自实际加载的配置与凭据，提交 PDF 并返回 200，页数、正文和内嵌图片检查通过；迁移模型目录、清理旧部署后重复检查仍通过。这些检查属于接入与执行验证，不是标注集质量评估或吞吐基准。

下述 GPU 性能与故障恢复测量属于 1.1 的历史记录，不能视为 1.2 已重新完成同等负载或故障演练。

## 监控部署检查

Prometheus 3.16.0 配置及三个告警规则通过 `promtool` 检查；Grafana 13.2.6 使用包含 Prometheus 数据源插件的标准镜像，关闭插件自动更新。两个镜像的配置和文件层摘要已核对，离线导入后固定到实际镜像 ID。

Prometheus 自身、Grafana 和 MinerU 三个目标均采集成功；Grafana 数据源健康检查通过，预置仪表盘包含 19 个面板，所有数据查询经 Grafana 数据源代理执行成功。近期无样本时的解析速度为空属于预期行为，不用零代替缺失的速度。

## 自动化检查（1.1 历史记录）

服务 1.1.0 使用 MinerU 4.0.12，43 项测试通过。队列测试使用 SQLite 3.51.3，真实 SDK 测试使用生成的图文 PDF 与 flash 原生文本路径，不需要预先下载模型。

覆盖内容包括：

- PDF 格式、加密、大小、页数和工作量限制；同步与异步接口的状态码。
- multipart 解析前的全局上传准入、分块请求体限制、同步等待和文件传输预算。
- 跨进程领取与全局容量、内容去重、独立幂等键、缓存总预算及结果保留。
- 失效租约、旧 worker 结果隔离、重复完成、有限重试、排队超时和垃圾清理。
- HTTP 等待超时后保留任务、在线备份和容量调整、拒绝未修复 WAL 问题的 SQLite。
- worker HTTP 协议、租约失效后退出，以及推理网关分配和故障后的名额回收。
- 本地存储与 S3 客户端契约、真实 SDK 阶段计时、图片和图表的回退渲染。

```bash
.venv/bin/python scripts/build_sqlite.py
LD_PRELOAD="$PWD/artifacts/sqlite/lib/libsqlite3.so.0" .venv/bin/python -m pytest -q
.venv/bin/ruff check app tests scripts
.venv/bin/ruff format --check app tests scripts
.venv/bin/python -m pip check
# 先运行 scripts/init_env.py，再校验部署配置。
docker compose config --quiet
docker compose -f compose.yaml -f compose.scale.yaml config --quiet
```

测试输出有两条 TestClient 超时参数弃用提示，不影响测试结果。S3 使用模拟客户端验证调用和读取关闭行为，尚未验证真实对象存储服务。

## 容器与真实 SDK

CPU API 镜像完成构建，确认未安装 Torch，SQLite 为 3.51.3。它与独立 worker 通过 HTTP 完成合成 PDF 的带图、缓存命中和无图解析；页数、关键文字和图片内嵌检查通过。GPU 环境同样完成同步兼容接口的这些检查；最终镜像的 flash、basic、standard、advanced 四档关闭缓存后，页数、关键文字与无图输出检查均通过。

真实 GPU 环境使用 L40、MinerU 4.0.12、Torch 2.13.0+cu130、vLLM 0.29.0 和 Pro-2605-1.2B 模型。API 不分配 GPU，worker 与独立 vLLM 共享每张 GPU。单卡与双卡分别运行独立 worker 池，通过同一个 SQLite 队列领取任务。定速到达测量见[性能汇总](benchmark.md)。

## 故障恢复

真实 GPU 测试强制终止正在执行的 worker。租约到期后，另一个 worker 用新租约重新执行；13 页、非空结果和关键文字检查通过。从终止到重新完成约 49.32 秒，包含新 worker 启动及租约等待，不等价于所有部署的恢复上限。

另一个测试在任务处于 queued 时重启 API；原任务 ID 和状态保留，随后以一次执行尝试完成 6 页解析。自动化测试还覆盖了旧租约不能提交、失败重试上限以及同步请求超时不取消持久任务。

这些验证覆盖进程故障和 API 重启；未执行主机掉电、磁盘损坏、真实 S3 故障或跨节点控制平面切换演练。

## 历史验证与精度边界

初始同步版本的 GPU 计时测试累计 144 次请求、1,002 页，失败数为 0，预热排除在统计之外。四种解析档位的无图检查均通过；无效文件和档位检查通过。

以上内容检查为 HTTP 状态、页数、非空 Markdown 和部分关键文字，并非逐字 OCR、公式或表格的标注集精度评估。分阶段计时存在嵌套与重叠，不能简单求和。

## 数据与复现

仓库只保留通用方法和匿名汇总，不包含部署地址、主机或用户信息、原始请求记录、环境快照、输入 PDF、逐次采样或中间 JSON。复现时使用自备样本和部署环境，原始结果写入忽略的 artifacts/ 目录。
