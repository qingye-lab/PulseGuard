# PulseGuard

[简体中文](./README.md) | [English](./README.en.md)

<p align="center">
  <img src="./assets/brand/pulseguard-brand-card.svg" alt="PulseGuard - 本地与跨网络 UI/API 探活控制台" width="100%" />
</p>

<p align="center">
  <strong>在一个控制台中持续检查页面、接口与基础网络目标，并保留可判断的失败证据。</strong>
</p>

PulseGuard（脉守）是一个面向个人、小团队和内网环境的 **UI/API 探活与轻量监测控制台**。

控制台面向桌面与笔记本浏览器，明确不做手机视口兼容；UI 探测任务的 H5 页面模式和移动目标视口不受影响。

它把定时检查、手动调试、运行历史、失败证据、健康状态、告警和多节点执行放在一个本地优先的单实例系统中。适合检查后台页面、登录前置流程、健康接口、批处理心跳、证书、TCP/DNS 和基础网络可达性。

PulseGuard 不试图替代完整 E2E 测试平台、公开状态页或 incident/on-call 系统。它解决的是一个更具体的问题：

> 当服务分散在本机、局域网和不同网络节点时，如何用较低成本持续确认它们是否仍然可用，并快速判断失败来自目标还是执行环境。

## 适合的场景

- 定时检查需要登录或准备页面上下文的后台 UI；
- 监测内部 API、健康接口和关键响应字段；
- 接收批处理、定时任务或设备主动上报的心跳；
- 监测 TLS 到期、HTTP 内容、重定向、静态资源、TCP 和 DNS；
- 从一个主节点统一调度本机、局域网和跨网络执行节点；
- 在失败时保留截图、Response Body 和可选 Trace；
- 区分业务目标失败与浏览器、节点、依赖或数据库等 Runner 异常。

## 核心能力

### UI 与 API 探活

- 支持定时、手动、批量与重跑；
- UI 支持结构化步骤、前置脚本和多浏览器类型；
- API 支持请求配置、字段预览、基础断言和高级脚本；
- 编辑器试运行不污染正式健康状态或告警统计。

### 可信健康状态

PulseGuard 不把单次抖动直接等同于故障。系统区分：

```text
健康 → 疑似故障 → 故障 → 疑似恢复 → 健康
```

同时保留未知、观测陈旧和停用等状态。浏览器崩溃、节点不可用、依赖缺失或数据库异常会归类为 Runner 异常，不会伪装成目标故障。

### 失败证据与趋势

- 失败摘要、截图和 Response Body；
- 可选 Playwright Trace，默认关闭；
- 24 小时、7 天、30 天和自定义时间段趋势；
- 成功响应时间的平均值、P95、P99 与失败次数；
- 相似失败滚动保留，避免证据无限增长。

### 多节点执行

支持三种执行来源：

```text
主节点
├── local：内置本机执行
├── direct worker：主节点直连局域网子节点
└── relay worker：子节点主动出站连接，不开放入站端口
```

多节点和多浏览器组合会生成统一分组的运行记录，并明确展示节点状态、浏览器类型、失败来源、耗时和证据。

### 告警与只读出口

- 任务、运行、趋势、异常周期、版本、项目审计和运行告警按项目隔离；
- 目标执行告警使用项目渠道，系统异常告警只使用全局系统渠道；
- 支持项目默认、标签和任务级运行告警策略；
- 支持冷却与恢复通知；
- 提供项目范围的 JSON、Prometheus 和受令牌保护的只读快照；
- 系统配置包与项目配置包分离，支持脱敏导出、导入预检与版本恢复。

## 快速启动

推荐使用 Docker Compose：

```bash
git clone https://github.com/liyanqing90/PulseGuard.git
cd PulseGuard
docker compose up --build -d
```

默认发布到 `0.0.0.0:8787`：

- 本机：`http://127.0.0.1:8787`
- 局域网：`http://<本机局域网 IP>:8787`

检查状态：

```bash
docker compose ps
docker compose logs --tail 80 pulseguard
curl http://127.0.0.1:8787/api/health
```

如只允许本机访问，在 `.env` 中设置：

```env
PULSEGUARD_PUBLISH_HOST=127.0.0.1
PULSEGUARD_HOST=0.0.0.0
PULSEGUARD_PORT=8787
PULSEGUARD_PUBLISH_PORT=8787
PULSEGUARD_ALERT_DETAIL_BASE_URL=http://127.0.0.1:8787
```

## Relay：让内网节点只出站连接

主节点启用 Relay：

```bash
export PULSEGUARD_RELAY_PUBLIC_HOST="<主节点公网 IP>"
docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d
```

默认只需开放公网 TCP `9443`。worker 不映射 `8788` 到宿主机，只通过 relay-client 主动连接主节点。

Relay 使用自签 TLS、server fingerprint pinning、一次性部署凭据、Token 哈希/加密存储和内部控制网络。完整部署与安全说明见 [运维与运行参考](./docs/operations-reference.md)。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| Backend | FastAPI、SQLite、Playwright Python、uv |
| Frontend | React、TypeScript、Vite、Ant Design |
| Runtime | Docker Compose 或本地开发进程 |
| Storage | SQLite、`data/`、`reports/` |
| Remote execution | Direct worker、WebSocket Relay |

## 设计边界

- 单实例、本地或 LAN 优先；
- 默认 SQLite，不要求外部数据库；
- 子节点只执行任务，不拥有调度、告警或主数据；
- 自定义 Python 脚本是可信本地能力，不是安全沙箱；
- 失败证据有保留策略，不把完整运行产物永久保存；
- 录制器不是主线，多步骤探活优先使用模板、结构化规则和前置脚本。

## 文档

| 文档 | 内容 |
| --- | --- |
| [运维与运行参考](./docs/operations-reference.md) | 本地开发、浏览器池、多节点、Relay、Token、接口和排障 |
| [中国网络环境部署](./docs/china-deployment.md) | 国内镜像与依赖环境部署 |
| [火山与本地节点部署](./docs/volcano-deployment.md) | 火山主节点、本地子节点和迁移流程 |
| [English README](./README.en.md) | English overview and setup |

## 验证

后端：

```bash
uv run python -m unittest discover -s backend/tests -p 'test_*.py' -v
```

前端：

```bash
cd frontend
npm run build
```

锁文件与部署检查：

```powershell
uv lock --check
.\scripts\deploy.ps1
```

## 项目结构

```text
backend/                    FastAPI 后端、存储、运行器、告警、测试
frontend/                   React 前端、页面、组件和样式
data/                       SQLite 数据目录
reports/                    截图、Trace、Response Body 和归档摘要
docs/                       部署、设计和运行文档
Dockerfile                  主节点镜像
docker-compose.yml          主节点部署
docker-compose.relay.yml    Relay 服务
Dockerfile.worker           子节点镜像
docker-compose.worker.yml   子节点部署
```

---

PulseGuard 由 [青野](https://github.com/liyanqing90) 创建和维护。
