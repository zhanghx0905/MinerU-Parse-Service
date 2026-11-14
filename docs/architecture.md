# 代码结构与阅读顺序

这个服务有两个主要概念：**文档解析**决定一份 PDF 如何生成 Markdown，**任务调度**决定何时执行、由谁执行以及哪些结果可以发布。目录分为业务规则、应用流程、基础设施和外部接口，具体组件由 `main.py` 装配。

```text
app/
├── main.py                        # 组装应用、生命周期和依赖
├── config.py                      # 环境变量与配置校验
├── domain/
│   ├── parsing.py                 # ParseOptions、ParseOutput、文档校验失败
│   └── jobs.py                    # 调度失败
├── application/
│   ├── parsing.py                 # 进程内解析、并发合并和结果缓存
│   ├── queue.py                   # 持久任务提交、等待、结果读取和清理流程
│   └── fingerprints.py            # 两种模式共用的内容身份计算
├── infrastructure/
│   ├── pdf.py                     # pypdf 格式、加密和页数检查
│   ├── storage.py                 # 本地文件与 S3
│   ├── metrics.py                 # SDK 阶段采集与持久指标导出
│   ├── mineru/
│   │   ├── engine.py              # SDK 生命周期、实例复用、临时文件
│   │   └── rendering.py           # 结构化结果过滤与 Markdown 渲染
│   └── sqlite/
│       ├── database.py            # 连接、短事务、建表和版本迁移
│       ├── jobs.py                # 全局准入与任务状态的原子更新
│       ├── observability.py       # 完成统计与队列/解析速度读模型
│       └── runtime.py             # SQLite 运行库检查
├── interfaces/
│   ├── worker.py                  # 单文档解析进程、HTTP 协议和独立续租
│   ├── worker_pool.py             # spawn 进程池、固定 VIP 角色与故障重启
│   ├── gateway.py                 # vLLM 请求代理与副本选择
│   └── http/
│       ├── parsing.py             # FastGPT 表单、同步解析和异步提交
│       ├── jobs.py                # 状态、结果、worker 协调接口
│       ├── admission.py           # 接收请求体前的上传/传输准入
│       ├── auth.py                # 公开接口和内部接口的独立凭据
│       ├── errors.py              # HTTP 状态与 FastGPT 错误响应
│       └── health.py              # 存活、全局队列快照和解析速度
└── cli/
    └── queue.py                   # SQLite 在线备份
```

## 从哪里开始读

1. [domain/parsing.py](../app/domain/parsing.py)：先理解四个输出开关及默认值。
2. [interfaces/http/parsing.py](../app/interfaces/http/parsing.py)：跟踪上传、校验、提交和返回结果。
3. [application/queue.py](../app/application/queue.py)：看完整的持久任务流程。
4. [infrastructure/sqlite/jobs.py](../app/infrastructure/sqlite/jobs.py)：重点阅读 `submit → claim → heartbeat → complete / fail`。
5. [interfaces/worker.py](../app/interfaces/worker.py)：理解 worker 执行和续租的并行关系。
6. [interfaces/worker_pool.py](../app/interfaces/worker_pool.py)：看解析进程如何启动、分配 VIP 角色和重启。
7. [infrastructure/mineru/rendering.py](../app/infrastructure/mineru/rendering.py)：看开关怎样影响最终内容。
8. 最后阅读 `main.py`，理解上述具体组件如何装配。

## 边界与依赖

- 领域对象只依赖 Python 标准库，不知道 FastAPI、SQLite 或 MinerU。
- 应用服务通过传入的 engine、store、storage 执行用例，由 `main.py` 创建具体实现。
- HTTP 层解释表单和响应；PDF 校验器抛领域错误，由 HTTP 层映射成稳定诊断码。
- `QueueStore` 把状态转换与 SQL 写入放在同一短事务。没有复制一套脱离事务的内存任务实体，避免形成两个状态来源。
- 模型执行和对象读写不占数据库事务；`lease_token` 决定一次执行能否发布结果。

## 修改时应保持的约束

增加影响输出的选项时，依次修改 `ParseOptions`、HTTP 表单、SDK/渲染规则和测试。指纹统一读取整个选项对象；持久任务保存该对象并传给 worker，不能只在 HTTP 层临时使用。

修改输出语义时更新 `OUTPUT_REVISION`；修改领取载荷的必需字段时更新 worker 协议版本；修改数据库结构时增加事务迁移并测试旧任务保留。三者解决不同的兼容性问题。

测试入口：[FastGPT 契约](../tests/test_fastgpt.py)、[结构化输出规则](../tests/test_rendering.py)、[数据库迁移](../tests/test_migrations.py)、[并发与租约](../tests/test_queue.py)。
