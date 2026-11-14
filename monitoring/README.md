# Prometheus 与 Grafana

本目录独立运行两个容器，采集 MinerU 协调端，不需要修改或重启解析服务。Prometheus 默认监听 `9091`，Grafana 默认监听 `3004`；数据库使用独立的 Docker 命名卷。数据保留受 30 天和 20 GB 两个上限约束，先达到的生效。

## 启动

```bash
cd monitoring
cp .env.example .env
chmod 600 .env
# 在 .env 中设置 GRAFANA_ADMIN_PASSWORD。
cp targets/mineru.yml.example targets/mineru.yml
# 修改 targets/mineru.yml，填写实际协调端 host:port。
docker compose config --quiet
docker compose pull
docker compose run --rm --no-deps --entrypoint promtool prometheus \
  check config /etc/prometheus/prometheus.yml
docker compose up -d
```

Grafana 登录名默认为 `admin`，密码来自本机 `.env`。管理密码只在首次初始化 Grafana 数据库时生效，后续修改已有用户密码应使用 Grafana 的用户管理功能。不要将 `.env`、实际目标地址或运行数据提交到仓库。

Grafana 自动配置 `Prometheus` 数据源、`MinerU` 文件夹和 `MinerU · PDF 解析` 面板，同时将该面板设为首页。使用包含数据源插件的标准镜像，并关闭插件自动更新，避免启动依赖外网；`13.2-slim` 不包含所需的 Prometheus 插件，不能直接替代。镜像差异见 [Grafana Docker 文档](https://grafana.com/docs/grafana/latest/setup-grafana/configure-docker/)。数据源 UID 与面板绑定，重建容器不需要手动导入。修改面板应编辑 `grafana/dashboards/mineru.json`；界面中的临时修改不会覆盖配置文件。原理见 [Grafana provisioning 文档](https://grafana.com/docs/grafana/latest/administration/provisioning/)。

## 采集与面板

Prometheus 每 15 秒采集自身、Grafana 和 `targets/*.yml` 中配置的 MinerU `/metrics`。目标文件会自动重新发现，修改目标地址不需要重启 Prometheus。每个数据库只填写一个协调端入口，避免同一组全局计数被重复采集。

面板提供队列积压、执行中任务、活跃 worker、最老等待时间、累计与近期按页加权的解析秒/页、完成页/分钟、速度样本量、任务阶段 P95、SDK 平均阶段耗时和任务事件。解析档位筛选作用于解析统计，队列指标始终为全局值。近期没有完成样本时速度显示无数据，首次启动后 `rate` 至少需要两个采样点。完整口径见[可观测性说明](../docs/observability.md)。

本配置只采集应用和监控服务自身指标，不包含 GPU 利用率、宿主机资源或容器资源；这些需要对应 exporter。Prometheus 自采集使用容器内端口 `localhost:9090`，Grafana 数据源使用服务地址 `http://prometheus:9090`，均不使用宿主机映射端口。

`alerts.yml` 配置 API 不可达、持续排队和无活跃 worker 三个告警。可以在 Prometheus `/alerts` 查看状态；当前没有 Alertmanager 或消息通知渠道。修改采集规则后先运行 `promtool check config`，再执行 `docker compose restart prometheus`。目标发现和规则配置见 [Prometheus 配置文档](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)。

## 镜像拉取与网络

如果主机无法访问 Docker Hub，在本机 `.env` 中将 `PROMETHEUS_IMAGE`、`GRAFANA_IMAGE` 改成可访问的完整镜像地址；先用 `docker pull` 验证，并优先固定实际镜像摘要。镜像覆盖只影响这个 Compose 项目，不需要修改 Docker 守护进程或重启其他业务。

```bash
docker pull <可访问仓库>/prom/prometheus:v3.16.0
docker pull <可访问仓库>/grafana/grafana:13.2.6
# 将成功拉取的镜像地址或 repo@sha256:... 写入 .env。
docker compose up -d
```

不要用不固定版本的 `latest` 绕过拉取失败。若目标主机完全无法联网，可在同架构的联网主机拉取固定版本，通过 `docker save` / `docker load` 导入，然后在 `.env` 中引用已导入镜像。

离线导入后可以用 `docker image inspect --format '{{.Id}}' <镜像名>` 获取镜像 ID，将对应的 `*_IMAGE` 设置为 `sha256:...`，再执行 `docker compose up -d --pull never`。镜像 ID 固定本地内容，不是可从仓库拉取的地址；重建主机时需要先重新导入归档。传输前后校验归档摘要，并保留来源版本和镜像 ID。

## 检查与备份

```bash
docker compose ps
curl -fsS http://127.0.0.1:9091/-/ready
curl -fsS http://127.0.0.1:9091/api/v1/targets
curl -fsS http://127.0.0.1:3004/api/health
docker compose logs --tail=100 prometheus grafana
```

Prometheus Targets 应显示三个采集目标为 UP。维护时先停止对应服务，再备份其命名卷及本目录配置；恢复时复用同一 Compose 项目名和数据卷。`docker compose down` 保留数据，`down -v` 会删除监控历史与 Grafana 用户数据。
