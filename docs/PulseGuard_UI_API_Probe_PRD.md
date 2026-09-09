# PulseGuard 本地 UI/API 探活方案

> 当前执行版本：V0.4（2026-06-05）
>
> 本版本以当前代码实现为准，更新原方案中的过时内容：告警从单 Webhook 配置升级为“公共告警策略 + 多通知渠道”；API 结构化校验优先使用 JSON Path 断言，JSON Schema 作为高级脚本能力保留；UI 探活新增结构化校验规则，脚本只作为高级模式；编辑调试结果在当前编辑区域下方内联展示，不新开调试页面。

## 0. V0.4 执行基线

### 0.1 已确认产品边界

```text
本地运行
轻量部署
UI 页面探活
API 结构探活
可视化配置优先
高级脚本兜底
手动调试内联展示
定时执行
失败留证
执行历史查看
多通知渠道告警
```

PulseGuard 不是完整自动化测试平台，也不做复杂 E2E 编排、录制器、元素点选器、AI 页面判断或多用户权限系统。

### 0.2 当前技术实现

```text
前端：React + Vite + Ant Design
代码编辑器：Monaco Editor
后端：FastAPI
数据库：SQLite
任务调度：APScheduler
UI 执行器：Playwright
API 执行器：httpx
API 结构化校验：JSON Path 断言
UI 结构化校验：标题 / URL / 文本 / 元素 / 控制台错误规则
通知：飞书 / 企业微信 / 钉钉 Webhook，多渠道共存
部署：Docker Compose，默认监听 0.0.0.0:8787
```

### 0.3 结构化校验优先级

```text
API：状态码、响应耗时、字段存在、字段等于、字段非空、字段包含、字段类型、字段长度
UI：元素可见、元素隐藏、文本出现、文本不存在、标题包含、URL 包含、元素数量、控制台无错误
高级脚本：复杂登录、多步骤交互、动态 token、自定义判断
```

API 与 UI 都遵循同一个原则：有启用的结构化校验项时，不要求填写 Python 脚本；没有结构化校验项时，必须通过 `async def check(ctx)` 提供高级脚本。

API 同时配置脚本和结构化规则时，先执行脚本，再检查脚本最后一次 HTTP 响应，不重复请求配置 URL。字段预览也执行动态脚本；运行详情显示请求记录，并区分目标请求耗时与探活总耗时。详见 [API 健康判定](api-health-checks.md)。UI 的页面前置脚本与规则执行顺序保持原有约定。

### 0.4 调试与历史详情

```text
编辑时运行草稿：不保存配置，不更新任务健康状态，不触发告警
调试结果：在编辑抽屉或调试页面底部内联展示
执行历史详情：优先使用右侧详情抽屉
UI 与 API 执行详情：结构区分展示，不硬复用同一详情结构
```

### 0.5 告警与通知渠道

```text
公共告警策略：启用告警、失败冷却时间、恢复通知
通知渠道：只是发送通道，可多个共存
每个渠道独立配置：名称、类型、启用状态、Webhook URL、钉钉加签密钥
密钥安全：设置接口不明文回显密钥，清除密钥需要显式操作
```

告警发送规则：

```text
OK -> Failed：立即告警
Failed -> Failed：按冷却时间告警
Failed -> OK：按恢复通知开关发送恢复通知
OK -> OK：不通知
```

## 1. 项目定位

**PulseGuard** 是一个本地运行的轻量级页面与接口探活控制台，用于定时检查系统页面和接口是否处于可用状态。

它不是完整自动化测试平台，也不是大规模监控平台，而是面向开发者和测试人员的本地探活工具。

核心定位：

```text
本地运行
轻量部署
UI 页面探活
API 结构探活
可视化配置
手动调试
定时执行
失败留证
执行历史查看
```

---

## 2. 核心目标

### 2.1 UI 探活目标

UI 探活主要检查：

```text
页面是否能打开
核心元素是否正常加载
关键文本是否出现
错误提示是否不存在
页面标题是否符合预期
URL 是否符合预期
```

UI 探活不做复杂业务流程，不做完整 E2E 自动化。

### 2.2 API 探活目标

API 探活主要检查：

```text
接口是否可访问
HTTP 状态码是否符合预期
响应是否为 JSON
响应结构是否符合 JSON Schema
关键字段类型是否正确
```

API 探活不校验复杂业务逻辑。

---

## 3. 产品边界

### 3.1 第一版要做

```text
UI 监控任务管理
API 监控任务管理
结构化 UI 校验规则
接口请求与 JSON Schema 校验
手动执行调试
定时执行
执行历史
失败详情
失败截图
Playwright Trace
接口响应体保存
本地设置
```

### 3.2 第一版不做

```text
权限管理
多用户系统
云端同步
复杂监控大屏
Prometheus / Grafana 集成
UI 操作录制
页面元素点选器
自动生成脚本
复杂 E2E 编排
AI 自动判断页面正常
复杂接口流程编排
```

---

## 4. 技术方案

### 4.1 推荐技术栈

```text
前端：React + Vite
组件库：Ant Design / Arco Design / Naive UI 三选一
脚本编辑器：Monaco Editor
后端：FastAPI
数据库：SQLite
任务调度：APScheduler
UI 执行器：Playwright
API 执行器：httpx
Schema 校验：jsonschema / Pydantic
部署：Docker Compose
通知：飞书 / 企业微信 / 钉钉 Webhook，多通知渠道共存
```

### 4.2 技术选型说明

React + Monaco 适合脚本、JSON Schema、调试结果等复杂编辑场景。

FastAPI + SQLite 足够支撑本地工具，部署轻，维护成本低。

Playwright 负责 UI 页面探活，httpx 负责 API 探活。

---

## 5. 信息架构

### 5.1 一级导航

```text
PulseGuard

- 总览
- UI 监控
- 接口监控
- 执行历史
- 系统设置
```

### 5.2 不需要的一级页面

不需要：

```text
现有任务列表
脚本编辑
历史详情
调试中心
```

原因：

```text
UI 监控页本身就是 UI 任务列表
接口监控页本身就是接口任务列表
脚本编辑从任务列表触发
历史详情从执行历史触发
```

---

## 6. 页面设计

## 6.1 总览页

路径：

```text
/
```

### 页面目标

快速查看当前探活整体状态。

### 展示内容

```text
UI 任务总数
接口任务总数
当前失败数
今日执行次数
最近一次执行时间
最近失败任务
最近恢复任务
```

### 最近失败列表

| 字段 | 说明 |
|---|---|
| 时间 | 最近失败时间 |
| 类型 | UI / API |
| 任务名称 | 探活任务名称 |
| 错误摘要 | 最近错误 |
| 操作 | 查看详情 / 重新执行 |

---

## 6.2 UI 监控页

路径：

```text
/ui-checks
```

### 页面目标

管理所有 UI 页面探活任务。

### 列表字段

| 字段 | 说明 |
|---|---|
| 名称 | UI 任务名称 |
| URL | 入口页面 URL |
| 状态 | OK / Failed / Never Run |
| 启用状态 | 开关 |
| 执行间隔 | 如 300s |
| 最近执行时间 | last_run_at |
| 最近耗时 | duration_ms |
| 连续失败次数 | consecutive_failures |
| 操作 | 执行 / 编辑 / 历史 / 删除 |

### 操作

```text
新增 UI 任务
编辑 UI 任务
立即执行
查看该任务历史
启用 / 禁用
删除任务
```

新增和编辑通过右侧抽屉完成。

复杂调试默认在编辑抽屉底部内联展示；隐藏二级页仅作为兜底入口：

```text
/ui-checks/{id}/debug
```

---

## 6.3 接口监控页

路径：

```text
/api-checks
```

### 页面目标

管理所有 API 接口探活任务。

### 列表字段

| 字段 | 说明 |
|---|---|
| 名称 | API 任务名称 |
| Method | GET / POST / PUT / DELETE |
| URL | 接口地址 |
| 状态 | OK / Failed / Never Run |
| 启用状态 | 开关 |
| 执行间隔 | 如 300s |
| 最近执行时间 | last_run_at |
| 最近耗时 | duration_ms |
| 连续失败次数 | consecutive_failures |
| 操作 | 执行 / 编辑 / 历史 / 删除 |

新增和编辑通过右侧抽屉完成。

---

## 6.4 执行历史页

路径：

```text
/runs
```

### 页面目标

统一查看 UI 和 API 的所有执行历史。

### 筛选条件

```text
任务类型：全部 / UI / API
执行状态：全部 / OK / Failed
任务名称
时间范围
```

### 列表字段

| 字段 | 说明 |
|---|---|
| 执行时间 | started_at |
| 类型 | UI / API |
| 任务名称 | check_name |
| 状态 | OK / Failed |
| 耗时 | duration_ms |
| 错误摘要 | error_message |
| 产物 | 截图 / Trace / Response |
| 操作 | 查看详情 / 重新执行 |

### 历史详情

历史详情不作为一级页面。

推荐：

```text
优先使用右侧详情抽屉
内容复杂时提供“打开完整详情”
```

隐藏二级路由：

```text
/runs/{id}
```

---

## 6.5 系统设置页

路径：

```text
/settings
```

### 设置项

```text
默认执行间隔
默认超时时间
最大并发数
失败报警冷却时间
历史保留天数
截图保留天数
Trace 保留天数
浏览器 headless 开关
代理设置
飞书 Webhook
企业微信 Webhook
```

第一版不做用户权限和登录。

---

# 7. UI 探活设计

## 7.1 UI 探活原则

UI 探活第一版不做录制器，不做元素点选，不做 Playwright Inspector 类工具。

核心原则：

```text
表单配置优先
结构化断言优先
高级脚本兜底
失败现场留证
```

### 不做录制的原因

```text
实现复杂
维护成本高
选择器生成难
回放稳定性难保证
容易变成低配 Playwright IDE
偏离探活工具定位
```

---

## 7.2 UI 任务模式

UI 任务支持两种模式：

```text
结构化模式 structured
高级脚本模式 script
```

默认模式：

```text
structured
```

高级模式用于：

```text
复杂登录
特殊等待
动态 token
多步骤页面跳转
自定义 JS 判断
复杂页面交互
```

但不作为默认入口。

---

## 7.3 UI 结构化规则

每个 UI 任务包含：

```text
入口 URL
等待策略
超时时间
必须可见元素
必须隐藏/不存在元素
必须出现文本
必须不存在文本
标题包含
URL 包含
控制台错误检测
```

### 等待策略

支持：

```text
domcontentloaded
load
networkidle
```

默认建议：

```text
domcontentloaded
```

原因：

```text
部分页面存在长连接、轮询、埋点请求，networkidle 可能不稳定。
推荐 domcontentloaded + 核心元素可见断言。
```

---

## 7.4 UI 结构化配置示例

```json
{
  "url": "https://example.com/admin",
  "wait_until": "domcontentloaded",
  "timeout_ms": 15000,
  "assertions": {
    "visible_selectors": [
      "[data-testid='main-layout']",
      "[data-testid='user-menu']"
    ],
    "hidden_selectors": [
      "text=系统异常",
      "[data-testid='error-page']"
    ],
    "texts_present": [
      "商品审核"
    ],
    "texts_absent": [
      "加载失败",
      "暂无权限"
    ],
    "title_contains": "管理后台",
    "url_contains": "/admin",
    "console_error_absent": true
  }
}
```

---

## 7.5 UI 编辑抽屉

### 基础配置

```text
任务名称
入口 URL
启用状态
执行间隔
超时时间
等待策略
```

### 校验规则

```text
必须可见元素
必须不存在元素
必须出现文本
必须不存在文本
标题包含
URL 包含
是否检测 console error
```

### 操作按钮

```text
保存
保存并执行
取消
运行草稿
```

### 调试结果

保存并执行后，在抽屉底部展示：

```text
执行状态
执行耗时
失败规则
错误信息
截图入口
Trace 入口
控制台错误
```

---

## 7.6 UI 探活执行逻辑

伪代码：

```python
async def run_structured_ui_check(config, ctx):
    page = await ctx.new_page()

    await page.goto(
        config["url"],
        wait_until=config.get("wait_until", "domcontentloaded"),
        timeout=config.get("timeout_ms", 15000),
    )

    for selector in config["assertions"].get("visible_selectors", []):
        await ctx.expect_visible(page, selector)

    for selector in config["assertions"].get("hidden_selectors", []):
        await ctx.expect_hidden(page, selector)

    for text in config["assertions"].get("texts_present", []):
        await ctx.expect_text_present(page, text)

    for text in config["assertions"].get("texts_absent", []):
        await ctx.expect_text_absent(page, text)

    if title := config["assertions"].get("title_contains"):
        await ctx.expect_title_contains(page, title)

    if url := config["assertions"].get("url_contains"):
        await ctx.expect_url_contains(page, url)

    if config["assertions"].get("console_error_absent"):
        ctx.assert_no_console_error()
```

---

# 8. API 探活设计

## 8.1 API 探活原则

API 探活不校验复杂业务逻辑，只校验：

```text
可访问性
状态码
JSON 可解析
响应结构
关键字段类型
```

---

## 8.2 API 任务配置

每个 API 任务包含：

```text
任务名称
Method
URL
Headers
Query Params
Body
期望状态码
超时时间
执行间隔
JSON Schema
高级 Python 脚本
```

---

## 8.3 API 结构化配置示例

```json
{
  "method": "GET",
  "url": "https://api.example.com/product/list",
  "headers": {
    "Authorization": "Bearer ${TOKEN}"
  },
  "query": {
    "page": 1,
    "pageSize": 10
  },
  "expected_status": 200,
  "timeout_ms": 10000,
  "schema": {
    "type": "object",
    "required": ["code", "message", "data"],
    "properties": {
      "code": { "type": "integer" },
      "message": { "type": "string" },
      "data": { "type": "object" }
    }
  }
}
```

---

## 8.4 API 编辑抽屉

### 基础配置

```text
任务名称
Method
URL
启用状态
执行间隔
超时时间
期望状态码
```

### 请求配置

```text
Headers JSON
Query Params JSON
Body JSON / Text
```

### 结构校验

```text
JSON Path 断言配置
```

### 高级模式

```text
Python check(ctx)
```

高级模式用于：

```text
动态签名
前置 token 获取
特殊加密
复杂响应判断
```

---

# 9. 高级脚本模式

## 9.1 脚本入口

所有高级脚本使用统一入口：

```python
async def check(ctx):
    ...
```

---

## 9.2 UI 高级脚本示例

```python
async def check(ctx):
    page = await ctx.new_page()

    await page.goto("https://example.com/admin", wait_until="domcontentloaded")

    await ctx.expect_visible(page, "[data-testid='main-layout']")
    await ctx.expect_hidden(page, "text=系统异常")
```

---

## 9.3 API 高级脚本示例

```python
async def check(ctx):
    resp = await ctx.http.get("https://api.example.com/product/list")

    ctx.assert_status(resp, 200)

    data = resp.json()

    ctx.assert_json_schema(data, {
        "type": "object",
        "required": ["code", "message", "data"],
        "properties": {
            "code": {"type": "integer"},
            "message": {"type": "string"},
            "data": {"type": "object"}
        }
    })
```

---

## 9.4 脚本安全边界

虽然是本地工具，也需要基本限制：

```text
执行超时
最大并发
单任务最多打开页面数
Docker CPU / 内存限制
历史记录保留限制
失败产物保留限制
```

第一版不做复杂沙箱，但建议容器运行。

---

# 10. 执行机制

## 10.1 执行方式

支持：

```text
手动执行
定时执行
保存并执行
重新执行历史任务
```

## 10.2 定时调度

使用 APScheduler。

任务维度：

```text
每个启用任务按照 interval_seconds 定时执行
max_instances = 1
coalesce = true
```

避免同一任务并发堆积。

---

## 10.3 执行状态

```text
pending
running
ok
failed
timeout
disabled
```

---

## 10.4 失败留证

UI 失败保存：

```text
截图
Playwright Trace
当前 URL
页面标题
console error
错误堆栈
```

API 失败保存：

```text
请求信息
响应状态码
响应头
响应体
Schema 校验错误
错误堆栈
```

---

# 11. 执行历史详情

## 11.1 UI 历史详情

展示：

```text
任务名称
任务类型
执行状态
开始时间
结束时间
耗时
失败规则
错误信息
错误堆栈
截图预览
Trace 下载
当前 URL
页面标题
console error
```

## 11.2 API 历史详情

展示：

```text
任务名称
任务类型
执行状态
开始时间
结束时间
耗时
请求 Method
请求 URL
请求 Headers
请求 Body
响应状态码
响应 Headers
响应 Body
Schema 校验错误
错误堆栈
```

---

# 12. 报警机制

## 12.1 通知渠道

当前版本支持：

```text
飞书 Webhook
企业微信 Webhook
钉钉 Webhook
多个通知渠道共存
```

## 12.2 报警规则

```text
OK -> Failed：立即报警
Failed -> Failed：按冷却时间报警
Failed -> OK：恢复通知
OK -> OK：不通知
```

## 12.3 报警内容

UI 失败：

```text
任务名称
任务类型
失败时间
错误摘要
失败规则
截图路径
Trace 路径
```

API 失败：

```text
任务名称
任务类型
失败时间
错误摘要
HTTP 状态码
Schema 错误
响应体路径
```

---

# 13. 数据模型

## 13.1 checks 表

```sql
CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL, -- ui / api
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER NOT NULL DEFAULT 300,
    timeout_ms INTEGER NOT NULL DEFAULT 15000,
    mode TEXT NOT NULL DEFAULT 'structured', -- structured / script
    entry_url TEXT,
    config_json TEXT,
    script TEXT,
    tags TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

---

## 13.2 runs 表

```sql
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL,
    check_name TEXT NOT NULL,
    check_type TEXT NOT NULL, -- ui / api
    status TEXT NOT NULL, -- ok / failed / timeout
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    error_message TEXT,
    error_stack TEXT,
    logs TEXT,
    screenshot_path TEXT,
    trace_path TEXT,
    response_path TEXT,
    request_json TEXT,
    response_json TEXT,
    artifacts_json TEXT,
    created_at TEXT NOT NULL
);
```

---

## 13.3 check_status 表

```sql
CREATE TABLE check_status (
    check_id INTEGER PRIMARY KEY,
    current_status TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failed_at TEXT,
    last_run_at TEXT,
    last_duration_ms INTEGER,
    last_error TEXT,
    last_notified_at TEXT
);
```

---

## 13.4 settings 表

```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

---

# 14. 项目结构

```text
pulseguard/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── scheduler.py
│   │   ├── runner.py
│   │   ├── storage.py
│   │   ├── notifier.py
│   │   ├── checks/
│   │   │   ├── ui_structured.py
│   │   │   ├── api_structured.py
│   │   │   └── script_runner.py
│   │   ├── web/
│   │   │   ├── checks.py
│   │   │   ├── runs.py
│   │   │   └── settings.py
│   │   └── schemas/
│   ├── data/
│   │   └── pulseguard.db
│   └── reports/
│       ├── screenshots/
│       ├── traces/
│       └── responses/
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Dashboard.tsx
│   │   │   ├── UiChecks.tsx
│   │   │   ├── ApiChecks.tsx
│   │   │   ├── Runs.tsx
│   │   │   └── Settings.tsx
│   │   ├── components/
│   │   │   ├── CheckDrawer.tsx
│   │   │   ├── RunDetailDrawer.tsx
│   │   │   ├── MonacoJsonEditor.tsx
│   │   │   └── MonacoPythonEditor.tsx
│   │   └── api/
├── docker-compose.yml
├── Dockerfile
└── README.md
```

---

# 15. API 设计

## 15.1 任务相关

```text
GET    /api/checks?type=ui
GET    /api/checks?type=api
POST   /api/checks
GET    /api/checks/{id}
PUT    /api/checks/{id}
DELETE /api/checks/{id}
POST   /api/checks/{id}/run
POST   /api/checks/run-all
```

## 15.2 历史相关

```text
GET    /api/runs
GET    /api/runs/{id}
GET    /api/checks/{id}/runs
```

## 15.3 设置相关

```text
GET    /api/settings
PUT    /api/settings
```

## 15.4 产物访问

```text
GET    /api/artifacts/screenshots/{filename}
GET    /api/artifacts/traces/{filename}
GET    /api/artifacts/responses/{filename}
```

---

# 16. MVP 版本规划

## 16.1 V0.1 必须完成

```text
UI 监控列表
接口监控列表
新增 / 编辑 UI 任务
新增 / 编辑 API 任务
结构化 UI 断言
API JSON Path 结构化校验
手动执行
执行历史
历史详情抽屉
SQLite 存储
Docker Compose 启动
```

## 16.2 V0.2 增强

```text
定时执行
失败截图
Playwright Trace
接口响应体保存
飞书 / 企业微信 / 钉钉多渠道通知
失败冷却
恢复通知
系统设置
```

## 16.3 V0.3 优化

```text
高级 Python 脚本模式
全屏调试页
Monaco Python 编辑器
JSON Path 断言配置
任务复制
YAML 导入导出
历史自动清理
```

## 16.4 暂不做

```text
UI 操作录制
元素点选器
多用户权限
复杂图表
多环境管理
复杂接口链路编排
AI 页面判断
```

---

# 17. 最终产品形态

PulseGuard 最终第一版应该是：

```text
本地 UI/API 探活控制台
```

核心能力：

```text
UI 监控：结构化页面规则
接口监控：请求配置 + JSON Schema
执行历史：统一查看 UI/API 历史
任务编辑：从列表触发抽屉
历史详情：从历史列表触发抽屉
高级脚本：作为兜底能力，不作为默认入口
```

一句话：

```text
PulseGuard 不是自动化测试平台，而是本地页面与接口健康探测工具。
```
