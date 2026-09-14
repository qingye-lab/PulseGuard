# PulseGuard 本地页面与接口探活控制台 PRD

版本：v0.1
状态：历史存档，不再反映当前实现
产品类型：本地运行的 UI/API 探活控制台
目标用户：个人开发者、小团队、内网系统维护者、测试/研发人员

> **本文是 v0.1 的历史方案存档，不是当前产品的描述。**
> 实现已超出本文范围（例如一级页面数量、项目隔离、成员、任务版本、配置迁移）。
> 当前产品文档入口：`PRODUCT.md`（产品范围与页面）、`DESIGN.md`（界面规范）、
> `README.md`（能力与部署）。三者与 `AGENTS.md` 一并作为 UI 与产品的事实来源。
> 修改产品行为时请更新上述文档，不要以本文为准。

---

## 1. 产品概述

### 1.1 产品名称

**PulseGuard**

中文名：**脉守**

### 1.2 产品定位

PulseGuard 是一个**本地运行的页面与接口探活控制台**，用于定时检查 Web 页面关键元素是否正常加载，以及接口响应结构是否符合预期。

它不是完整的自动化测试平台，也不是大型可观测平台，而是一个轻量、可控、易维护的本地探活工具。

### 1.3 一句话描述

> PulseGuard 是一个本地运行的 UI/API 探活控制台，支持脚本化规则、定时执行、手动调试、执行历史、失败留证和告警通知。

### 1.4 核心价值

- 本地运行，不依赖 SaaS 平台。
- 支持 UI 页面关键元素探活。
- 支持 API 响应结构探活。
- 支持 Python 脚本化规则，灵活度高。
- 支持手动执行和定时执行。
- 支持执行历史、失败详情、截图、Trace、响应体留存。
- 支持飞书/企微 Webhook 告警。
- 不做权限系统，降低复杂度。

---

## 2. 背景与问题

### 2.1 当前问题

在个人项目、本地服务、后台管理系统或内网系统中，经常需要确认：

1. 页面是否能正常打开。
2. 页面关键元素是否加载成功。
3. 页面是否出现系统异常、接口错误、白屏等问题。
4. 接口是否可访问。
5. 接口返回结构是否符合预期。
6. 定时发现问题并通知。
7. 失败后能快速查看现场。

传统方案存在明显不足：

| 方案 | 问题 |
|---|---|
| Uptime Kuma | 适合基础存活，不适合复杂 UI 元素探活和结构校验 |
| Prometheus/Grafana | 偏指标体系，对页面元素检查过重 |
| Jenkins 定时任务 | 能跑脚本，但不是探活控制台 |
| Postman Monitor | 适合接口，不适合页面 |
| Selenium | 维护成本高，现代项目优先 Playwright |
| 自写脚本 + cron | 缺少可视化、历史、调试和规则管理 |

因此需要一个本地轻量控制台，将任务管理、脚本编辑、手动调试、定时执行和历史查看整合起来。

---

## 3. 产品目标

### 3.1 V0.1 目标

完成一个可本地运行的探活控制台，支持：

- UI 探活任务管理。
- API 探活任务管理。
- Python 脚本编辑。
- 手动执行调试。
- APScheduler 定时执行。
- 执行历史查看。
- 执行详情查看。
- 失败截图、Trace、响应体保存。
- 基础告警配置。
- Docker Compose 本地部署。

### 3.2 非目标

V0.1 不做以下能力：

- 用户登录。
- 权限管理。
- 团队协作。
- 多租户。
- 大屏展示。
- 复杂图表趋势。
- 完整 E2E 自动化测试编排。
- 接口业务逻辑校验平台。
- AI 自动判断页面是否正常。
- SaaS 云端部署。

---

## 4. 用户角色

### 4.1 主要用户

#### 个人开发者

需要本地定时探测自己的 Web 项目、后台页面、接口服务是否正常。

#### 小团队研发/测试

需要轻量方式维护内网系统或测试环境的可用性。

#### 项目维护者

需要快速发现页面白屏、接口结构异常、服务不可用等问题。

### 4.2 使用场景

| 场景 | 描述 |
|---|---|
| 本地服务探活 | 检查本机或局域网服务是否可用 |
| 后台页面探活 | 检查管理后台关键页面是否正常加载 |
| API 结构探活 | 检查接口返回 JSON 结构是否符合预期 |
| 定时巡检 | 每 5 分钟自动执行一次任务 |
| 手动调试 | 新增或修改任务后立即执行检查 |
| 故障定位 | 查看失败截图、Trace、响应体和错误堆栈 |

---

## 5. 产品范围

### 5.1 一级页面

> 已过时：当前实现的一级页面以 `PRODUCT.md` 为准，数量与命名均已变化。
> 下列清单保留 v0.1 的原始范围决定，仅供追溯。

PulseGuard V0.1 只包含 5 个一级页面：

```text
1. 总览
2. UI 监控
3. 接口监控
4. 执行历史
5. 系统设置
```

### 5.2 二级交互

以下能力不作为一级页面：

```text
1. 新增 UI 任务
2. 编辑 UI 任务
3. 新增接口任务
4. 编辑接口任务
5. 脚本调试
6. 执行详情
7. 全屏调试
```

它们通过以下方式触发：

- 右侧抽屉 Drawer。
- 弹框 Modal。
- 二级详情页。

### 5.3 页面信息架构

```text
PulseGuard
├── 总览
├── UI 监控
│   ├── UI 任务列表
│   ├── 新增 UI 任务 Drawer
│   ├── 编辑 UI 任务 Drawer
│   └── UI 全屏调试页，可选
├── 接口监控
│   ├── 接口任务列表
│   ├── 新增接口任务 Drawer
│   ├── 编辑接口任务 Drawer
│   └── 接口全屏调试页，可选
├── 执行历史
│   ├── 全局历史列表
│   ├── UI/API 类型筛选
│   ├── 执行详情 Drawer
│   └── 执行详情页，可选
└── 系统设置
```

---

## 6. 导航设计

### 6.1 左侧导航

```text
PulseGuard

总览
UI 监控
接口监控
执行历史
系统设置
```

### 6.2 不进入左侧导航的能力

以下能力不进入一级导航：

- 任务列表。
- 脚本编辑。
- 调试中心。
- 历史详情。
- 任务详情。

原因：

- UI 监控页本身就是 UI 任务列表。
- 接口监控页本身就是接口任务列表。
- 编辑和详情都是任务上下文中的二级操作。

---

## 7. 功能需求

---

# 7.1 总览页

## 7.1.1 页面路径

```text
/
```

## 7.1.2 页面目标

快速查看系统整体探活状态。

## 7.1.3 展示内容

### 顶部统计卡片

| 指标 | 说明 |
|---|---|
| UI 任务总数 | 当前已创建的 UI 探活任务数 |
| API 任务总数 | 当前已创建的接口探活任务数 |
| 当前失败数 | 当前处于失败状态的任务总数 |
| 今日执行次数 | 当日所有任务累计执行次数 |
| 最近一次执行 | 最近一次任务执行时间 |
| 最近恢复任务 | 最近由失败恢复为正常的任务 |

### 最近失败列表

| 字段 | 说明 |
|---|---|
| 时间 | 最近失败时间 |
| 类型 | UI / API |
| 任务名称 | 失败任务名称 |
| 错误摘要 | 最近错误信息 |
| 连续失败 | 连续失败次数 |
| 操作 | 查看详情 / 立即执行 |

## 7.1.4 操作

- 点击任务名称：进入对应任务所在列表并定位。
- 点击查看详情：打开最近一次失败详情。
- 点击立即执行：触发该任务手动执行。

---

# 7.2 UI 监控页

## 7.2.1 页面路径

```text
/ui-checks
```

## 7.2.2 页面目标

管理所有 UI 页面探活任务。

## 7.2.3 列表字段

| 字段 | 说明 |
|---|---|
| 名称 | UI 任务名称 |
| URL | 页面地址 |
| 状态 | OK / Failed / Never Run / Disabled |
| 启用 | 开关 |
| 定时 | 执行频率，如每 5 分钟 |
| 最近执行 | 最近一次执行时间 |
| 耗时 | 最近一次执行耗时 |
| 连续失败 | 连续失败次数 |
| 操作 | 执行 / 编辑 / 历史 / 删除 |

## 7.2.4 顶部操作

- 新增 UI 任务。
- 执行全部启用 UI 任务。
- 刷新列表。

## 7.2.5 行内操作

| 操作 | 行为 |
|---|---|
| 执行 | 立即执行当前任务 |
| 编辑 | 打开任务编辑 Drawer |
| 历史 | 跳转执行历史，并按当前任务过滤 |
| 删除 | 删除当前任务，需二次确认 |
| 启用/禁用 | 切换任务定时执行状态 |

## 7.2.6 新增/编辑 UI 任务 Drawer

### 打开方式

- 点击“新增 UI 任务”。
- 点击任务行“编辑”。

### Drawer 建议宽度

```text
720px - 960px
```

### 表单字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| 名称 | Input | 是 | 任务名称 |
| URL | Input | 是 | 页面入口 URL |
| 启用 | Switch | 是 | 是否参与定时执行 |
| 执行频率 | Number/Input | 是 | 单位秒，默认 300 |
| 超时时间 | Number/Input | 是 | 单位 ms，默认 15000 |
| 标签 | Input | 否 | 用于分类筛选 |
| Python 脚本 | Monaco Editor | 是 | `async def check(ctx):` 脚本 |

### 操作按钮

| 按钮 | 说明 |
|---|---|
| 保存 | 保存任务配置 |
| 保存并执行 | 保存后立即执行一次 |
| 运行调试 | 不改变定时状态，立即执行脚本 |
| 全屏调试 | 进入全屏脚本调试页 |
| 取消 | 关闭 Drawer |

### 调试结果区域

运行后在 Drawer 内展示：

- 运行状态。
- 执行耗时。
- 错误摘要。
- 日志输出。
- 截图预览入口。
- Trace 下载入口。

---

# 7.3 接口监控页

## 7.3.1 页面路径

```text
/api-checks
```

## 7.3.2 页面目标

管理所有 API 接口探活任务。

## 7.3.3 列表字段

| 字段 | 说明 |
|---|---|
| 名称 | API 任务名称 |
| Method | GET / POST / PUT / DELETE 等 |
| URL | 接口地址 |
| 状态 | OK / Failed / Never Run / Disabled |
| 启用 | 开关 |
| 定时 | 执行频率 |
| 最近执行 | 最近一次执行时间 |
| 耗时 | 最近一次执行耗时 |
| 连续失败 | 连续失败次数 |
| 操作 | 执行 / 编辑 / 历史 / 删除 |

## 7.3.4 顶部操作

- 新增接口任务。
- 执行全部启用接口任务。
- 刷新列表。

## 7.3.5 新增/编辑接口任务 Drawer

### 表单字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| 名称 | Input | 是 | 接口任务名称 |
| Method | Select | 是 | 请求方法 |
| URL | Input | 是 | 接口 URL |
| 启用 | Switch | 是 | 是否参与定时执行 |
| 执行频率 | Number/Input | 是 | 单位秒，默认 300 |
| 超时时间 | Number/Input | 是 | 单位 ms，默认 10000 |
| Headers | JSON Editor | 否 | 请求头 |
| Body | JSON/Text Editor | 否 | 请求体 |
| 标签 | Input | 否 | 用于分类筛选 |
| Python 脚本 | Monaco Editor | 是 | `async def check(ctx):` 脚本 |

### 操作按钮

| 按钮 | 说明 |
|---|---|
| 保存 | 保存任务配置 |
| 保存并执行 | 保存后立即执行一次 |
| 运行调试 | 立即执行脚本 |
| 全屏调试 | 进入全屏脚本调试页 |
| 取消 | 关闭 Drawer |

### 调试结果区域

接口任务运行后展示：

- 运行状态。
- HTTP 状态码。
- 执行耗时。
- 错误摘要。
- Schema 校验错误。
- Response Body 查看入口。
- 请求/响应头查看入口。

---

# 7.4 执行历史页

## 7.4.1 页面路径

```text
/runs
```

## 7.4.2 页面目标

统一查看 UI/API 所有执行历史。

## 7.4.3 筛选条件

| 筛选项 | 可选值 |
|---|---|
| 类型 | 全部 / UI / API |
| 状态 | 全部 / OK / Failed |
| 任务名称 | 模糊搜索 |
| 时间范围 | 开始时间 - 结束时间 |

## 7.4.4 列表字段

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

## 7.4.5 行内操作

| 操作 | 行为 |
|---|---|
| 查看详情 | 打开执行详情 Drawer 或二级详情页 |
| 重新执行 | 立即重新执行该任务 |
| 下载产物 | 下载截图 / Trace / Response Body |

---

# 7.5 执行详情

## 7.5.1 入口

执行详情不作为一级页面。

入口包括：

- 执行历史列表点击“查看详情”。
- 总览页最近失败点击“查看详情”。
- UI/API 任务列表点击最近失败记录。

## 7.5.2 展示方式

优先使用：

```text
右侧详情 Drawer
```

当内容较多时提供：

```text
打开完整详情
```

对应二级路径：

```text
/runs/{id}
```

## 7.5.3 通用信息

| 字段 | 说明 |
|---|---|
| 任务名称 | 执行所属任务 |
| 类型 | UI / API |
| 状态 | OK / Failed |
| 开始时间 | started_at |
| 结束时间 | finished_at |
| 耗时 | duration_ms |
| 错误摘要 | error_message |
| 错误堆栈 | error_stack |
| 日志 | logs |

## 7.5.4 UI 任务详情

UI 失败时展示：

- 当前 URL。
- 失败 Selector。
- 页面截图。
- Playwright Trace 下载。
- Console 错误。
- Network 错误摘要。
- 错误堆栈。

## 7.5.5 API 任务详情

API 失败时展示：

- 请求 Method。
- 请求 URL。
- 请求 Headers。
- 请求 Body。
- 响应 Status Code。
- 响应 Headers。
- Response Body。
- JSON Schema 校验错误。
- 错误堆栈。

---

# 7.6 系统设置页

## 7.6.1 页面路径

```text
/settings
```

## 7.6.2 页面目标

配置全局执行、告警、浏览器和数据保留策略。

## 7.6.3 设置项

### 执行设置

| 设置 | 默认值 | 说明 |
|---|---:|---|
| 默认执行频率 | 300 秒 | 新任务默认定时频率 |
| 默认 UI 超时 | 15000 ms | UI 任务默认超时 |
| 默认 API 超时 | 10000 ms | API 任务默认超时 |
| 最大并发任务数 | 2 | 避免本地资源占用过高 |
| 单任务最大运行时长 | 60 秒 | 防止脚本卡死 |

### 告警设置

| 设置 | 默认值 | 说明 |
|---|---:|---|
| 是否启用告警 | false | 默认关闭 |
| Webhook 类型 | feishu / wecom | 飞书或企微 |
| Webhook URL | 空 | 告警地址 |
| 失败冷却时间 | 30 分钟 | 连续失败时避免刷屏 |
| 恢复通知 | true | 失败恢复时通知 |

### 浏览器设置

| 设置 | 默认值 | 说明 |
|---|---:|---|
| Headless | true | 是否无头运行 |
| 浏览器类型 | chromium | 默认 chromium |
| 代理 | 空 | 可配置 HTTP 代理 |
| Viewport | 1440x900 | 默认页面尺寸 |

### 数据保留

| 设置 | 默认值 | 说明 |
|---|---:|---|
| 执行历史保留天数 | 30 天 | 超期清理 |
| 截图保留天数 | 30 天 | 超期清理 |
| Trace 保留天数 | 7 天 | Trace 文件较大，保留更短 |
| Response Body 保留天数 | 30 天 | 接口失败现场 |

---

## 8. 脚本方案

### 8.1 总体原则

PulseGuard 不采用纯 YAML，也不采用完全无约束脚本平台。

推荐方案：

```text
任务元信息表单 + Python async check(ctx) 脚本
```

### 8.2 为什么不用纯 YAML

YAML 适合配置，但不适合复杂探活逻辑。

页面探活经常需要：

- 等待元素。
- 判断 loading 消失。
- 处理登录态。
- 点击后再检查。
- 捕获 console 错误。
- 捕获 network 错误。
- 处理弹窗。

如果全部用 YAML 描述，会逐渐演变成自定义 DSL，维护成本反而更高。

### 8.3 为什么不用完全自由 Python

完全自由 Python 灵活，但会导致：

- 结果格式不统一。
- 截图、Trace、日志留存不统一。
- 超时控制复杂。
- 调试输出不可控。
- 安全风险更高。

因此必须固定入口和上下文能力。

### 8.4 脚本入口规范

所有任务脚本必须提供：

```python
async def check(ctx):
    ...
```

系统负责：

- 创建运行环境。
- 注入 ctx。
- 控制超时。
- 捕获异常。
- 保存结果。
- 保存截图/Trace/响应体。
- 写入历史记录。
- 触发告警。

### 8.5 UI 脚本示例

```python
async def check(ctx):
    page = await ctx.new_page()

    await page.goto("https://example.com/admin", wait_until="networkidle")

    await ctx.expect_visible(page, "[data-testid='main-layout']")
    await ctx.expect_visible(page, "[data-testid='user-menu']")
    await ctx.expect_hidden(page, "text=系统异常")
```

### 8.6 API 脚本示例

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

### 8.7 ctx 能力设计

#### 浏览器能力

| 方法 | 说明 |
|---|---|
| `ctx.new_page()` | 创建 Playwright 页面 |
| `ctx.expect_visible(page, selector)` | 断言元素可见 |
| `ctx.expect_hidden(page, selector)` | 断言元素不可见 |
| `ctx.expect_text(page, text)` | 断言文本出现 |
| `ctx.screenshot(page, name=None)` | 手动截图 |
| `ctx.log(message)` | 输出调试日志 |

#### HTTP 能力

| 方法 | 说明 |
|---|---|
| `ctx.http.get(url, **kwargs)` | GET 请求 |
| `ctx.http.post(url, **kwargs)` | POST 请求 |
| `ctx.assert_status(resp, expected)` | 校验状态码 |
| `ctx.assert_json_schema(data, schema)` | 校验 JSON Schema |
| `ctx.save_response(resp)` | 保存响应体 |

#### 通用能力

| 方法 | 说明 |
|---|---|
| `ctx.log(message)` | 写运行日志 |
| `ctx.fail(message)` | 主动标记失败 |
| `ctx.attach_file(path)` | 附加文件到运行结果 |

---

## 9. 执行机制

### 9.1 执行类型

| 类型 | 说明 |
|---|---|
| 手动执行 | 用户点击按钮后立即执行 |
| 定时执行 | 按任务配置周期执行 |
| 保存并执行 | 编辑任务保存后立即执行 |
| 重新执行 | 从历史记录中重新执行所属任务 |

### 9.2 定时执行

使用 APScheduler。

每个启用任务根据 `interval_seconds` 注册定时任务。

关键约束：

```text
1. 同一任务不允许并发重复执行。
2. 上一次未结束时，本次跳过或合并。
3. 系统启动后自动加载启用任务。
4. 编辑任务后刷新对应调度。
5. 禁用任务后移除对应调度。
```

### 9.3 执行状态

| 状态 | 说明 |
|---|---|
| pending | 等待执行 |
| running | 执行中 |
| ok | 执行成功 |
| failed | 执行失败 |
| timeout | 执行超时 |
| skipped | 因并发限制跳过 |

### 9.4 执行结果

每次执行必须记录：

- 任务 ID。
- 任务类型。
- 开始时间。
- 结束时间。
- 耗时。
- 状态。
- 错误摘要。
- 错误堆栈。
- 日志。
- 产物路径。

---

## 10. 告警机制

### 10.1 告警渠道

V0.1 支持：

- 飞书 Webhook。
- 企业微信 Webhook。

### 10.2 告警触发规则

| 状态变化 | 是否通知 |
|---|---|
| OK -> Failed | 通知 |
| Failed -> Failed | 按冷却时间通知 |
| Failed -> OK | 恢复通知 |
| OK -> OK | 不通知 |
| Disabled | 不通知 |

### 10.3 告警内容

UI 失败告警：

```text
【PulseGuard 探活失败】
类型：UI
任务：管理后台首页
时间：2026-06-04 18:30:00
错误：selector 未出现 [data-testid='main-layout']
连续失败：3
耗时：8200ms
详情：本地控制台 /runs/123
```

API 失败告警：

```text
【PulseGuard 探活失败】
类型：API
任务：商品列表接口
时间：2026-06-04 18:30:00
错误：JSON Schema 校验失败 data.items 期望 array 实际 object
连续失败：2
耗时：320ms
详情：本地控制台 /runs/124
```

恢复通知：

```text
【PulseGuard 探活恢复】
类型：UI
任务：管理后台首页
时间：2026-06-04 18:45:00
失败持续：15 分钟
```

---

## 11. 数据模型

### 11.1 checks 表

```sql
CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL, -- ui / api
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER NOT NULL DEFAULT 300,
    timeout_ms INTEGER NOT NULL DEFAULT 15000,
    entry_url TEXT,
    method TEXT,
    headers_json TEXT,
    body TEXT,
    script TEXT NOT NULL,
    tags TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

字段说明：

| 字段 | 说明 |
|---|---|
| id | 任务 ID |
| name | 任务名称 |
| type | ui / api |
| enabled | 是否启用 |
| interval_seconds | 执行间隔 |
| timeout_ms | 超时时间 |
| entry_url | UI 页面 URL 或 API URL |
| method | API 请求方法，UI 任务可为空 |
| headers_json | 请求头 JSON |
| body | 请求体，API 任务使用 |
| script | Python 脚本 |
| tags | 标签 |
| created_at | 创建时间 |
| updated_at | 更新时间 |

### 11.2 runs 表

```sql
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL,
    check_name TEXT NOT NULL,
    check_type TEXT NOT NULL, -- ui / api
    status TEXT NOT NULL, -- pending / running / ok / failed / timeout / skipped
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    error_message TEXT,
    error_stack TEXT,
    logs TEXT,
    screenshot_path TEXT,
    trace_path TEXT,
    response_path TEXT,
    request_snapshot TEXT,
    response_snapshot TEXT,
    created_at TEXT NOT NULL
);
```

### 11.3 check_status 表

```sql
CREATE TABLE check_status (
    check_id INTEGER PRIMARY KEY,
    current_status TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    last_failed_at TEXT,
    last_run_at TEXT,
    last_run_id INTEGER,
    last_error TEXT,
    last_notified_at TEXT,
    updated_at TEXT NOT NULL
);
```

### 11.4 settings 表

```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

---

## 12. API 设计

### 12.1 Check 管理

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/checks?type=ui` | 获取 UI 任务列表 |
| GET | `/api/checks?type=api` | 获取 API 任务列表 |
| POST | `/api/checks` | 创建任务 |
| GET | `/api/checks/{id}` | 获取任务详情 |
| PUT | `/api/checks/{id}` | 更新任务 |
| DELETE | `/api/checks/{id}` | 删除任务 |
| POST | `/api/checks/{id}/enable` | 启用任务 |
| POST | `/api/checks/{id}/disable` | 禁用任务 |
| POST | `/api/checks/{id}/run` | 手动执行任务 |

### 12.2 Runs 历史

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/runs` | 获取执行历史 |
| GET | `/api/runs/{id}` | 获取执行详情 |
| POST | `/api/runs/{id}/rerun` | 重新执行所属任务 |

### 12.3 总览

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/overview` | 获取总览统计 |

### 12.4 设置

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/settings` | 获取系统设置 |
| PUT | `/api/settings` | 更新系统设置 |

### 12.5 产物访问

| Method | Path | 说明 |
|---|---|---|
| GET | `/artifacts/screenshots/{file}` | 查看截图 |
| GET | `/artifacts/traces/{file}` | 下载 Trace |
| GET | `/artifacts/responses/{file}` | 查看响应体 |

---

## 13. 前端设计建议

### 13.1 技术栈

推荐：

```text
React + Vite + TypeScript + Monaco Editor + FastAPI
```

原因：

- 脚本编辑是核心体验。
- Monaco Editor 与 React 集成成熟。
- 任务管理、Drawer、表格、状态刷新更方便。
- 后续扩展全屏调试页成本低。

### 13.2 组件建议

| 组件 | 说明 |
|---|---|
| `Layout` | 左侧导航 + 顶部状态 |
| `OverviewPage` | 总览页 |
| `UIChecksPage` | UI 监控页 |
| `APIChecksPage` | 接口监控页 |
| `RunsPage` | 执行历史页 |
| `SettingsPage` | 系统设置页 |
| `CheckEditorDrawer` | 任务编辑抽屉 |
| `RunDetailDrawer` | 执行详情抽屉 |
| `ScriptEditor` | Monaco 脚本编辑器 |
| `RunResultPanel` | 调试结果区域 |
| `StatusBadge` | 状态展示 |

### 13.3 交互原则

- 一级页面保持少。
- 编辑通过 Drawer 完成。
- 历史详情通过 Drawer 完成。
- 复杂调试提供全屏入口。
- 不做权限管理。
- 不做复杂大屏。

---

## 14. 后端设计建议

### 14.1 技术栈

```text
FastAPI
SQLite
SQLAlchemy / SQLModel
APScheduler
Playwright async
httpx async
jsonschema
Docker Compose
```

### 14.2 后端模块

```text
app/
├── main.py              # FastAPI 入口
├── scheduler.py         # 定时任务管理
├── runner.py            # 任务执行器
├── context.py           # ctx 能力封装
├── checks/
│   ├── ui.py            # UI 任务执行逻辑
│   └── api.py           # API 任务执行逻辑
├── storage.py           # 数据访问
├── notifier.py          # 告警通知
├── artifacts.py         # 产物保存
├── settings.py          # 系统设置
└── schemas.py           # Pydantic Schema
```

### 14.3 执行器职责

Runner 负责：

- 加载任务。
- 创建执行记录。
- 构建 ctx。
- 执行脚本。
- 捕获异常。
- 保存产物。
- 更新状态。
- 触发告警。

---

## 15. 部署方案

### 15.1 推荐部署方式

使用 Docker Compose 本地运行。

```text
pulseguard/
├── data/
│   └── pulseguard.db
├── reports/
│   ├── screenshots/
│   ├── traces/
│   └── responses/
├── docker-compose.yml
└── .env
```

### 15.2 docker-compose.yml 示例

```yaml
services:
  pulseguard:
    build: .
    container_name: pulseguard
    restart: unless-stopped
    ports:
      - "8787:8787"
    volumes:
      - ./data:/app/data
      - ./reports:/app/reports
    environment:
      - TZ=Asia/Shanghai
    mem_limit: 1g
    cpus: 1.0
```

### 15.3 访问地址

```text
http://localhost:8787
```

---

## 16. 安全与边界

### 16.1 不做权限管理

原因：

- 产品定位为本地工具。
- 默认只在本机或局域网使用。
- 权限系统会显著增加复杂度。

### 16.2 脚本安全边界

虽然不做用户权限，但必须做执行约束：

- 单任务运行超时。
- 并发数限制。
- Docker CPU/内存限制。
- 产物目录隔离。
- 历史数据定期清理。
- 禁止同一任务重复并发。

### 16.3 网络边界

默认建议：

- 只监听 `127.0.0.1`。
- 如需局域网访问，再手动改为 `0.0.0.0`。

---

## 17. MVP 优先级

### 17.1 P0 必须实现

- 左侧导航。
- 总览页。
- UI 监控任务列表。
- 接口监控任务列表。
- 新增/编辑任务 Drawer。
- Python 脚本编辑器。
- 手动执行任务。
- 执行历史列表。
- 执行详情 Drawer。
- SQLite 存储。
- UI/API 执行器。
- APScheduler 定时执行。
- Docker Compose 部署。

### 17.2 P1 重要增强

- 失败截图。
- Playwright Trace。
- API Response Body 留存。
- 飞书/企微 Webhook 告警。
- 恢复通知。
- 告警冷却。
- 历史清理。
- 全屏调试页。
- 设置页。

### 17.3 P2 后续能力

- YAML 导入/导出。
- 任务复制。
- 标签筛选。
- 环境变量管理。
- 登录态管理。
- 多环境配置。
- 失败趋势图。
- 任务执行耗时趋势。

---

## 18. 验收标准

### 18.1 UI 监控验收

- 可以新增 UI 任务。
- 可以编辑 UI 任务。
- 可以启用/禁用 UI 任务。
- 可以手动执行 UI 任务。
- UI 任务失败时能保存截图。
- UI 任务失败时能保存 Trace。
- UI 任务执行结果进入历史。

### 18.2 API 监控验收

- 可以新增 API 任务。
- 可以编辑 API 任务。
- 可以启用/禁用 API 任务。
- 可以手动执行 API 任务。
- API 任务能校验 HTTP 状态码。
- API 任务能校验 JSON Schema。
- API 失败时能保存 Response Body。
- API 执行结果进入历史。

### 18.3 执行历史验收

- 可以查看 UI/API 全部执行历史。
- 可以按类型筛选 UI/API。
- 可以按状态筛选 OK/Failed。
- 可以查看执行详情。
- 可以从历史记录重新执行任务。

### 18.4 定时任务验收

- 启用任务后按频率自动执行。
- 禁用任务后不再自动执行。
- 编辑执行频率后调度生效。
- 同一任务不会并发重复执行。

### 18.5 告警验收

- 任务首次失败发送告警。
- 连续失败按冷却时间告警。
- 失败恢复后发送恢复通知。
- OK 状态不重复通知。

---

## 19. 默认模板

### 19.1 UI 基础模板

```python
async def check(ctx):
    page = await ctx.new_page()

    await page.goto("https://example.com", wait_until="networkidle")

    await ctx.expect_visible(page, "[data-testid='main-layout']")
    await ctx.expect_hidden(page, "text=系统异常")
```

### 19.2 API 基础模板

```python
async def check(ctx):
    resp = await ctx.http.get("https://api.example.com/health")

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

### 19.3 API 简单结构模板

```python
async def check(ctx):
    resp = await ctx.http.get("https://api.example.com/list")
    ctx.assert_status(resp, 200)

    body = resp.json()

    assert isinstance(body.get("code"), int)
    assert isinstance(body.get("message"), str)
    assert isinstance(body.get("data"), dict)
```

---

## 20. 最终产品定义

PulseGuard V0.1 的最终定义：

> 一个本地运行的 UI/API 探活控制台，通过 Python 脚本定义探活规则，支持 UI 页面关键元素检查、API 响应结构检查、定时执行、手动调试、执行历史、失败详情、截图/Trace/响应体留存和 Webhook 告警。

最终一级页面：

```text
1. 总览
2. UI 监控
3. 接口监控
4. 执行历史
5. 系统设置
```

核心交互：

```text
UI 监控页 = UI 任务列表
接口监控页 = API 任务列表
执行历史页 = UI/API 统一历史
任务编辑 = 列表触发 Drawer
历史详情 = 历史列表触发 Drawer / 二级页
复杂调试 = 可选全屏二级页
```

技术路线：

```text
前端：React + Vite + TypeScript + Monaco Editor
后端：FastAPI + APScheduler + Playwright + httpx
存储：SQLite
部署：Docker Compose
告警：飞书/企微 Webhook
```
