# PulseGuard 运维与运行参考

本文档集中记录 PulseGuard 的本地开发、浏览器生命周期、多运行节点、Relay、子节点更新、Token 管理、网络要求、排障、数据安全和接口参考。

产品定位、核心能力和快速启动请先阅读根目录 [README](../README.md)。

## 本地开发

后端依赖使用 `uv`：

```powershell
uv sync
uv run python -m playwright install chromium
uv run uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787 --reload
```

前端依赖使用 npm：

```powershell
cd frontend
npm ci
npm run dev
```

前端默认开发地址为 `http://127.0.0.1:5173`。如需指定端口和后端代理：

```powershell
cd frontend
$env:VITE_DEV_API_TARGET="http://127.0.0.1:8787"
npm run dev -- --host 127.0.0.1 --port 5175
```

## UI 浏览器生命周期

UI 任务支持 `chromium`、`firefox`、`webkit`。系统设置中可以分别配置：

- `enabled_browser_types`：允许任务选择和执行的浏览器类型；
- `prewarmed_browser_types`：服务启动或设置变更时自动预热的浏览器类型；
- `browser_pool_sizes`：每类浏览器预创建的空 `BrowserContext` 数量，默认 5；
- `browser_recycle_after_runs`：浏览器完成指定数量的 UI Context 后自动回收，默认 20；
- `similar_failure_retention_count`：连续相似失败保留的明细窗口，默认 10；
- `trace_artifacts_enabled`：是否记录失败 Trace，默认关闭。

每个预热浏览器类型保留一个 Playwright browser 进程，并维护独立的空 Context 池。UI 任务独占租用一个 Context/Page，结束后关闭 Context 并由资源池补齐。Web 与 H5 通过 Context 参数区分，可以在同一 browser 进程下并发执行不同 viewport。

页面扫描会把最外层 `cursor:pointer` 命中区域识别为交互候选，并限制低稳定性重复组件族的数量。重复的卡片、名称、城市/里程、价格等结构化区域会优先生成“重复区域 + 字段”的定位方式；候选下拉框只展示可选 selector，系统在内部保留并校验真实匹配数。重复区域生成数量规则时默认使用“至少 1 个”，避免动态列表条数变化造成误报。默认只分析页面自然加载后的 DOM，不会主动滚动并触发懒加载或无限列表；确实需要扫描滚动后内容时，应在任务的前置脚本中显式执行滚动。元素数量断言会在任务超时预算内等待数量条件稳定成立，而不是只在页面初次加载后立即计数一次。

任务执行时先解析执行节点，再解析节点支持的浏览器类型。多节点与多浏览器组合会按“节点 × 浏览器类型”生成运行记录。缺失或未启用的组合记录为 Runner 异常，不会伪装为目标故障。

## 多运行节点

PulseGuard 默认保留并启用内置 `local` 节点。远程子节点只负责：

- 暴露受 Token 保护的健康检查与执行接口；
- 执行主节点下发的任务；
- 回传结果与证据；
- 上报版本、构建 SHA、镜像与浏览器安装状态。

子节点不会启动调度器、不会写主节点数据库，也不会独立发送业务告警。

### 接入模式

#### 手动直连

主节点必须能够直接访问子节点：

```text
http://<worker>:8788
```

在主节点页面中填写地址与 worker token，并执行“测试连接”。

#### Relay 自动接入

适用于子节点无法开放入站端口的内网或跨网络环境。worker 只主动出站连接主节点 Relay，主节点通过 Docker 内部网络访问 worker API。

主节点启用 Relay：

```sh
export PULSEGUARD_RELAY_PUBLIC_HOST="<主节点公网 IP>"
docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d
```

默认公网端口为 `9443`。如只能开放 `443`：

```sh
export PULSEGUARD_RELAY_PUBLIC_PORT=443
docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d
```

## Relay 安全模型

主节点在 `data/relay/` 生成自签 TLS 证书，并向 worker 展示 server fingerprint。worker 验证 fingerprint 后才发送 relay token。

已有 Relay 节点时，证书或私钥丢失不会触发静默重建。应从备份恢复 `data/relay/`，否则已部署 worker 的 fingerprint pinning 将失效。

凭据存储规则：

- 数据库只保存 relay token hash；
- worker token 使用 Fernet 加密后保存；
- 默认加密密钥文件为 `runner-token.key`；
- 备份或迁移主节点时必须同时保管该密钥；
- 完整部署命令含敏感凭据，只应在受控环境中传递。

Relay 控制面默认使用 Docker 内部地址：

```text
http://pulseguard-relay-internal:18000
```

控制 Token 位于 `data/relay/control-token`，内部端口不应映射到公网。数据库中的 Token 或节点状态先立即失效，控制面 revoke 只用于加速旧 Session 断开。

Docker Relay overlay 会创建 internal-only 网络。默认内部主机名为 `pulseguard-relay-internal`。如需改动，应成对设置：

```text
PULSEGUARD_RELAY_INTERNAL_HOST
PULSEGUARD_RELAY_INTERNAL_LISTEN_HOST
```

每个 worker session 的并发内部 TCP stream 上限默认跟随系统最大并发任务数。空闲 stream 默认 900 秒关闭；已删除节点的内部端口会进入 quarantine，避免旧连接误复用。

同一 runner ID 只保留一个活跃 Session。新 Session 认证成功后替换旧 Session。

公网入口默认包含：

- WebSocket 连接总量限制；
- Hello 超时；
- 单帧大小限制；
- 禁用 WebSocket 压缩；
- 无效认证退避；
- Session 心跳超时。

可通过以下环境变量调整：

```text
PULSEGUARD_RELAY_MAX_PUBLIC_CONNECTIONS
PULSEGUARD_RELAY_HELLO_TIMEOUT_SECONDS
PULSEGUARD_RELAY_MAX_FRAME_BYTES
PULSEGUARD_RELAY_AUTH_BACKOFF_BASE_SECONDS
PULSEGUARD_RELAY_AUTH_BACKOFF_MAX_SECONDS
PULSEGUARD_RELAY_STREAM_IDLE_TIMEOUT_SECONDS
PULSEGUARD_RELAY_PORT_QUARANTINE_SECONDS
```

## Relay 节点状态

- `待部署`、`已过期`、`连接中`：不参与轮询，也不触发 Runner 异常告警；
- Relay 连接建立后先进入 `连接中`；
- 主节点成功访问 `/api/worker/health` 后进入 `可用`；
- 曾经可用后断线才进入 `异常` 或 `认证失败`；
- “重新生成命令”保留 runner ID 和内部端口，但轮换 relay token。

## 手动子节点

源码方式启动：

```powershell
uv run python -m backend.app.worker --host 0.0.0.0 --port 8788
```

可追加节点名称和区域：

```powershell
uv run python -m backend.app.worker --host 0.0.0.0 --port 8788 --name office-worker-1 --region office-lan
```

启动后会输出地址提示和认证 Token。主节点应填写实际可访问地址，例如：

```text
http://<子节点 IP>:8788
```

如未显式传入 `--token` 或 `PULSEGUARD_WORKER_TOKEN`，worker 会将 Token 保存到 `PULSEGUARD_WORKER_TOKEN_FILE`，默认使用数据目录下的 `worker-token`。

### Docker 部署

Windows PowerShell：

```powershell
git clone <your-pulseguard-repository-url>
cd PulseGuard
$env:PULSEGUARD_WORKER_NAME = $env:COMPUTERNAME
$env:PULSEGUARD_WORKER_REGION = "default"
$env:COMPOSE_PROJECT_NAME = "pulseguard-worker"
docker compose -f docker-compose.worker.yml -f docker-compose.worker.build.yml up --build -d
docker update --restart unless-stopped pulseguard-worker
docker logs --tail 80 pulseguard-worker
```

Linux：

```sh
git clone <your-pulseguard-repository-url>
cd PulseGuard
export PULSEGUARD_WORKER_NAME="$(hostname)"
export PULSEGUARD_WORKER_REGION="default"
export COMPOSE_PROJECT_NAME="pulseguard-worker"
export COMPOSE_PROFILES="updater"
export PULSEGUARD_WORKER_UPDATER_URL="http://pulseguard-worker-updater:8790"
docker compose -f docker-compose.worker.yml -f docker-compose.worker.build.yml up --build -d
docker update --restart unless-stopped pulseguard-worker pulseguard-worker-updater
docker logs --tail 80 pulseguard-worker
```

## 子节点受控更新

标准子节点部署默认启用 updater。updater 会挂载 Docker socket，但仅允许更新 Compose 中声明的 PulseGuard 服务，不接受任意命令。

```powershell
$env:COMPOSE_PROJECT_NAME = "pulseguard-worker"
$env:COMPOSE_PROFILES = "updater"
$env:PULSEGUARD_WORKER_UPDATER_URL = "http://pulseguard-worker-updater:8790"
$env:PULSEGUARD_WORKER_UPDATE_IMAGE = "pulseguard-worker:local"
docker compose -f docker-compose.worker.yml -f docker-compose.worker.build.yml up --build -d
docker update --restart unless-stopped pulseguard-worker pulseguard-worker-updater
```

更新流程：

1. 拉取目标镜像；
2. 本地已有目标镜像时允许直接使用；
3. 重建 worker，Relay 模式同时重建 relay-client；
4. 继承 runner、relay token 和 fingerprint 等必要变量；
5. 检查 `/api/worker/health`；
6. 失败时尝试回滚旧镜像。

如子节点无法访问源码仓库，可在可访问环境中构建并推送到内部镜像仓库：

```sh
export PULSEGUARD_WORKER_IMAGE="registry.example.com/pulseguard-worker:local"
export PULSEGUARD_WORKER_UPDATE_IMAGE="registry.example.com/pulseguard-worker:local"
export PULSEGUARD_WORKER_ALLOWED_IMAGE_PREFIXES="registry.example.com/pulseguard-worker"
docker compose -f docker-compose.worker.yml up -d
docker update --restart unless-stopped pulseguard-worker
docker logs --tail 80 pulseguard-worker
```

`PULSEGUARD_WORKER_ALLOWED_IMAGE_PREFIXES` 按镜像仓库边界校验，不按原始字符串前缀匹配。普通条目只允许该仓库的不同 tag/digest；如确需允许整个命名空间，使用 `registry.example.com/team/*`。类似 `pulseguard-worker.attacker.example/evil` 的前缀伪装不会被接受。

可覆盖基础镜像：

```powershell
$env:PULSEGUARD_UV_IMAGE = "registry.example.com/astral-sh/uv:0.9.30"
$env:PULSEGUARD_PLAYWRIGHT_IMAGE = "registry.example.com/playwright/python:v1.49.1-noble"
```

## Token 查看与轮换

查看当前 Token：

```sh
docker compose -f docker-compose.worker.yml exec pulseguard-worker python -m app.worker --show-token
```

轮换并重启：

```sh
docker compose -f docker-compose.worker.yml exec pulseguard-worker python -m app.worker --rotate-token
docker compose -f docker-compose.worker.yml restart pulseguard-worker
```

worker 未运行时：

```sh
docker compose -f docker-compose.worker.yml run --rm pulseguard-worker python -m app.worker --rotate-token
```

源码方式：

```powershell
uv run python -m backend.app.worker --rotate-token --token-file data/worker-token
```

手动模式轮换后，必须在主节点执行“更新认证”并重新测试连接。Relay 模式需要轮换 relay token 时，应重新生成部署命令并在 worker 宿主机执行。

## 多节点结果与调度

- 一次多节点执行使用统一 `run_group_id`；
- 节点与浏览器类型多选时按矩阵生成运行记录；
- 任一可用节点出现目标失败或超时，本轮任务失败；
- 全部可用节点成功，本轮任务成功；
- 节点不可用记录为 `status=skipped`、`failure_kind=runner`、`affects_health=false`；
- Runner 异常不计入目标失败；
- 任务级 round-robin 使用独立游标；
- 调度器保留 30 秒 misfire 宽限。

## 网络要求

- 手动模式：主节点必须访问子节点 `/api/worker/health` 和 `/api/worker/run`。优先使用 HTTPS；新增或修改为 HTTP/裸 IP 时必须显式确认明文传输风险，旧版已保存的 HTTP 节点会迁移为已确认；
- Relay 模式：公网只开放主节点 Relay TCP 端口，默认 `9443`；
- 子节点 API 使用 `Authorization: Bearer <token>`；手工 HTTP 模式不会加密 token、任务参数或环境变量，只适用于已确认的可信网络；
- 手动节点不主动访问主节点；
- Relay client 主动出站连接主节点；
- 截图、Trace 和 Response Body 使用 JSON base64 回传；
- 超出单文件大小上限时记录异常，不伪造成功；
- 手动停用节点不触发不可用告警。

## 常见排障

- **测试连接失败**：检查地址是否从主节点可达、端口是否开放、worker 是否监听正确网卡；
- **Relay 停留在连接中**：检查 Relay 服务、relay-client 日志、fingerprint 和公网端口；
- **Relay 认证失败**：重新生成部署命令并执行，旧 relay token 已失效；
- **节点不可用**：核对主节点保存的地址和 Token；
- **运行记录显示 Runner 异常**：优先检查节点状态、浏览器依赖和 worker 日志，不按业务目标失败处理。

### Relay 短暂断连

远程任务调用失败后，主节点会执行一次最多 5 秒的认证健康复核。同一节点的并发失败共用一次复核，已选中该节点的后续派发等待复核结束，再检查最新可调度状态。复核成功时不把单次调用失败升级成整节点离线；复核失败时标记不可用。原任务保留异常结果，不自动重放，避免子节点已执行但响应丢失时重复执行。脚本库验证不修改节点状态。

Relay 的每节点 TCP 流上限为 `max(max_concurrency, PULSEGUARD_RELAY_MAX_CONCURRENT_STREAMS) + 4`，额外空间用于健康检查、更新和证据请求；这不是请求分类隔离，辅助请求同样受总上限约束。它不提高实际任务并发数。relay-client 的 worker 建连最多等待 5 秒，等待主节点 ready 使用 `PULSEGUARD_RELAY_HELLO_TIMEOUT_SECONDS`。断连后按约 1、2、4 秒逐步退避，最高 30 秒并带抖动；连接持续至少 60 秒后重置退避，正常关闭也不会立即循环重连。

主节点服务日志记录 `worker_request_failed`（runner_id、run_id）和 `worker_health_recheck_*`。Relay 服务日志记录 `relay_connected`、`relay_disconnected`（runner_id、session_id）、`relay_stream_rejected`、`relay_peer_stream_failed`（stream_id、原因）。原因包括 `worker_connect_failed`、`eof_before_data`、`idle_timeout`、`byte_limit`、WebSocket 关闭码等。按时间与 runner_id 对齐主节点、Relay 和客户端日志；run_id 与 stream_id 尚无直接映射。日志不记录任务内容、Token 或任意对端错误文本。重连仍清空界面的当前断连原因，历史事件从服务日志查询。

systemd 环境查询 `journalctl -u pulseguard.service -u pulseguard-relay.service --since "30 minutes ago"`；Docker 环境分别查询主服务、Relay 服务和 `pulseguard-relay-client` 的容器日志。确认部署环境为 journal/容器日志配置持久保留。主节点更新即可获得健康复核和连接容量优化；详细流关闭原因、客户端建连超时和退避需要同时更新 relay-client。新增诊断字段可被现有客户端/服务端忽略，不改变 worker API 或数据表。

## 验证命令

快速验证（后端测试、前端行为测试、类型检查）：

```powershell
uv run --frozen python scripts/verify.py quick
uv run --frozen python scripts/verify.py quick --backend-pattern test_runner.py
```

完整验证（另含生产构建、包体预算、真实浏览器业务冒烟）：

```powershell
uv run --frozen python -m playwright install chromium
uv run --frozen python scripts/verify.py full
```

后端单独验证使用 `uv run --frozen python scripts/test_backend.py`，支持 `--pattern test_runner.py`。入口会在导入应用前隔离数据、数据库、报告和备份目录，并初始化临时数据库；不会继承运行实例的路径、令牌或节点角色。不要直接让测试读取 `data/pulseguard.db`。

完整验证要求浏览器测试实际执行，缺少 Chromium 会失败；快速验证仅在浏览器可执行文件缺失时跳过相关测试，其他启动错误仍失败。日志记录每个步骤的耗时、结果及失败详情，目录会打印到终端。`ui` 模式只运行前端构建与浏览器冒烟。浏览器冒烟启动临时实例，通过页面完成创建、调试、运行历史、项目切换，以及趋势和候选元素的键盘操作，并保存截图与 Trace；不依赖生产实例或外部探测目标。

前端生产构建通过 `frontend/scripts/monaco-features.mjs` 裁剪 Monaco 的可选功能注册，保留 Python/JSON 编辑、补全、查找替换、格式化和校验。编辑器增量加载预算同时统计 JS、CSS、异步语言包、Worker 和字体，结果写入 `frontend/dist/.vite/bundle-budget.json`；升级 Monaco 后需重新验证实际编辑行为，不能通过拆分文件绕过预算。

构建同时生成 JS、CSS 和字体的 `.gz` 文件；部署必须保留这些同目录文件。`/assets` 根据 `Accept-Encoding` 返回预压缩版本，并对带内容哈希的资源设置一年缓存；`index.html` 继续要求重新验证。不接受 gzip 的请求、Range 请求以及缺少压缩文件的旧构建继续返回原文件。排查加载慢时应核对实际响应的 `Content-Encoding`、`Content-Length`、`Cache-Control` 和下载耗时，而不是仅查看构建日志中的 gzip 估算值。

CI 在 PR 和既有 push 分支上运行完整验证；PR 不发布镜像。后端或前端的定向验证用于开发反馈，提交前运行完整验证。

锁文件检查：

```powershell
uv lock --check
```

部署会修改运行实例，单独使用 `.\scripts\deploy.ps1`，不属于验证命令。

中国网络环境部署见 [china-deployment.md](./china-deployment.md)；火山主节点和本地子节点接入见 [volcano-deployment.md](./volcano-deployment.md)。

## 高级脚本入口

API 或通用任务使用固定入口：

```python
async def check(ctx):
    response = await ctx.request()
    ctx.assert_status(response, 200)
```

UI 任务可先用 `setup_script` 准备页面前提：

```python
async def setup(ctx):
    page = await ctx.new_page()
    await page.goto(ctx.entry_url)
    return page
```

存在结构化断言时不强制编写脚本。复杂登录、多窗口或业务分支仍可使用脚本模式。

API 同时填写脚本和健康规则时，脚本先执行，规则检查最后一次 HTTP 响应；不会忽略脚本或额外发送一次请求。动态变量在发起请求时解析。配置方法、预览行为与请求耗时说明见 [API 健康判定](api-health-checks.md)。

## 项目隔离与一次性迁移

- 项目接口之外，任务、运行、趋势、状态页、指标、项目告警和项目配置包请求必须携带 `X-PulseGuard-Project-Id`；缺失返回 400，不存在返回 404。
- 归档项目历史可读，但创建、编辑、执行、重跑、导入和版本恢复返回 409；调度器只装载活跃项目任务。
- 只读令牌必须绑定至少一个项目，`/api/read-only/snapshot` 同时校验令牌范围与项目头。
- `pulseguard.config` v4 只包含全局设置、Runner 和成员；`pulseguard.project` v1 只包含当前项目任务与项目告警。

旧数据库不会由应用启动过程隐式修改。升级前先执行只读预检：

```powershell
uv run python scripts/migrate-projects.py
```

发布窗口内完成应用一致性备份后，再显式执行：

```powershell
uv run python scripts/migrate-projects.py --apply
```

脚本会在 `data/backups/` 创建迁移前 SQLite 在线备份、校验完整性并输出迁移前后各项目表计数和 SHA-256。迁移失败会回滚整个事务；回滚版本时必须同时恢复旧应用和该迁移前数据库备份。新旧版本不得连接同一数据库混跑。

## 数据与安全边界

- 默认 SQLite，数据库位于 `data/`；
- 在线备份位于 `data/backups/`；
- 恢复备份会暂停新接单和调度，最多等待 30 秒排空已接纳的任务；超时返回 409 且不恢复。恢复、安全备份与初始化共用独占维护边界，失败时从安全备份补偿；客户端取消不会提前解除保护；
- 纯查询使用只读 SQLite 快照，写入串行；历史保留清理按终态运行 ID 分批提交，运行中记录不删除，中断后重试不会重复归档；
- 恢复仅在主节点执行；若恢复补偿或运行时配置重载失败，返回 503 并保持任务暂停，需要按错误提示人工恢复/检查配置后重启，不会自动恢复接单；API/UI 扫描预览不是持久化运行任务，不属于该接单范围；
- “运维审计 → 数据库状态”只读展示数据库文件、WAL 与可回收空闲页；压缩仍需先备份并在停止写入的维护窗口执行；
- 趋势数据写入 `trend_rollups` 聚合表；
- 旧运行历史趋势默认不回填；
- 截图、Trace、Response Body 和归档摘要位于 `reports/`；
- 常见凭据、Header、Cookie 和只读令牌会在公开输出中脱敏；
- 用户自定义 Python 脚本是可信本地工具能力，不是安全沙箱；
- 录制器不是当前主线，多步骤探活优先使用模板、前置脚本和结构化规则。

控制台不做手机视口兼容；H5 探测能力不受影响。前端编辑器运行时和 Worker 使用本地按需资源，无需公共 CDN。加载、并发与验收范围见 [性能优化说明](performance-optimization.md)。

部署时核对 `uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"`，优先采用 [SQLite 官方 WAL-reset 修复版本](https://sqlite.org/wal.html#walresetbug)（3.51.3+，或官方列出的 3.50.7 / 3.44.6 回移版本）。不要在应用运行时启动外部写入/checkpoint 工具；也不要让多个应用进程共用同一数据库。

如确需补齐旧趋势数据，可临时设置：

```text
PULSEGUARD_TREND_BACKFILL_ON_STARTUP=true
```

启动一次后应恢复默认关闭。

## 常用接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/health` | 应用健康检查 |
| `GET /api/projects` / `POST /api/projects` | 项目列表与创建 |
| `GET /api/database-health` | 只读数据库容量与空闲页健康指标 |
| `GET /api/metrics.json` | JSON 指标 |
| `GET /api/metrics` | Prometheus 指标 |
| `GET /api/read-only/snapshot` | 受令牌保护的只读快照 |
| `GET /api/project-alert-settings` / `PUT /api/project-alert-settings` | 当前项目运行告警 |
| `GET /api/config/export` | 导出 `pulseguard.config` v4 系统包 |
| `GET /api/project-config/export` | 导出 `pulseguard.project` v1 当前项目包 |
| `GET /api/runners` / `POST /api/runners` | 节点列表与手动创建 |
| `GET /api/relay/status` | Relay 地址、状态与 fingerprint |
| `POST /api/runners/provision` | 创建 Relay 节点与一次性部署命令 |
| `POST /api/runners/{runner_id}/provision/regenerate` | 轮换 Relay 部署命令 |
| `POST /api/runners/{runner_id}/update` | 推送子节点受控更新 |
| `GET /api/runners/{runner_id}/update-status` | 查询更新状态 |
| `GET /api/worker/health` | 子节点健康检查 |
| `POST /api/worker/run` | 子节点执行入口 |
| `POST /api/worker/browser-types/install` | 安装浏览器类型 |
| `POST /api/worker/update` | 子节点受控更新入口 |
| `GET /api/runs?runner_id=...` | 按节点筛选运行记录 |
| `GET /api/runs?run_group_id=...` | 查看一次多节点执行 |
| `GET /api/runs/{id}` | 查看运行快照详情 |
| `GET /api/monitoring-trends` | 整体趋势 |
| `GET /api/checks/{id}/trend` | 单任务趋势 |
| `POST /api/heartbeats/{key}` | 被动心跳上报 |

## 目录结构

```text
backend/                    FastAPI 后端、存储、运行器、告警、测试
frontend/                   React 前端、页面、组件和样式
data/                       SQLite 数据目录
reports/                    截图、Trace、Response Body 和归档摘要
docs/                       部署、设计和功能文档
Dockerfile                  主节点镜像
Dockerfile.worker           子节点 worker 镜像
Dockerfile.worker-updater   子节点 updater 镜像
docker-compose.yml          主节点部署
docker-compose.relay.yml    主节点 Relay overlay
docker-compose.worker.yml   子节点部署
docker-compose.relay-worker.yml  Relay 模式子节点部署
pyproject.toml              后端依赖
uv.lock                     后端依赖锁定
```
