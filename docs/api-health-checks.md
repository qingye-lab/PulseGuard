# API 健康判定与动态请求

普通接口在请求配置中填写 URL、方法、Headers 和 Body，在「健康判定规则」中设置 HTTP 状态、业务状态码和关键字段类型。探活判断服务可用性，不需要展开分页、筛选组合或验证价格计算公式。合法空列表不等于接口故障；只有业务明确要求必须有数据时才添加非空规则。

需要先获取车源、token 或签名的接口使用 `async def check(ctx)` 发起请求。执行顺序是：

1. 有脚本时先执行脚本；没有脚本时发起配置的单次请求。
2. 有健康规则时，检查本次脚本的最后一次 HTTP 响应。平台不会额外重复请求配置 URL。
3. 脚本没有留下 HTTP 响应但配置了规则时，明确失败；脚本失败时停止，不使用前一次成功响应。

脚本可以调用 `ctx.vars.set()` 设置本次变量，再调用 `ctx.request()`。URL、Headers、Body 在请求发起时解析。直接调用 `ctx.http.get/post()` 时也会解析本次变量。前置依赖需要的检查留在脚本中；目标接口的可用性条件放在可见的健康规则中。

```python
async def check(ctx):
    seed = await ctx.http.get("${SEED_API}")
    ctx.assert_status(seed, 200)
    ctx.vars.set("ITEM_ID", seed.json()["id"])
    await ctx.request()
```

上述任务的配置 URL 可以是 `${BASE_URL}/items/${ITEM_ID}`，健康规则可以是 HTTP 200、`$.returncode` 等于 `0`、`$.result` 为对象。所用变量来自任务所属项目。脚本独立判定的旧任务仍可运行，但不会自动生成可视化规则。

「执行请求」也会运行动态脚本，提供最终响应字段用于选择规则；「试运行当前配置」验证脚本和全部启用规则。两者均执行真实请求，请求脚本必须由任务作者控制副作用。API 的 `setup_script` 不使用 UI 的页面前置入口。

运行详情保留最近 10 次 HTTP 请求、响应和错误，并对敏感值脱敏、对正文限长。前置失败可从请求记录定位。目标请求耗时只计算最后一次 HTTP 请求；探活总耗时包含取样、签名、规则判定等整个过程，趋势记录仍采用整条任务耗时。

界面、主节点和执行脚本的节点需要使用支持这一执行顺序的版本。升级前应检查同时配置脚本和规则的历史任务，确认原本被忽略的脚本确实应当执行。

项目公共方法库、版本发布、候选验证和导入限制见[项目公共方法库](project-script-library.md)。

任务脚本可以通过项目已发布的方法库复用请求准备或解析逻辑。`ctx.modules.use()` 只接受一个模块名字符串字面量；方法调用要显式传入签名要求的参数，异步方法使用 `await`，常量直接读取属性。公共方法模块不能再调用 `ctx.modules.use()`，因此模块组合写在任务的 `check` 或 UI `setup`/`check` 入口：

```python
async def check(ctx):
    auth = ctx.modules.use("auth_helpers")
    token = await auth.issue(ctx, ctx.vars.get("USER_ID"))
    ctx.vars.set("TOKEN", token)
    await ctx.request()
```

保存后的任务运行、API preview 和 UI preview 使用当前项目的 published 方法版本；库页验证使用保存的候选 draft，并把源码快照发送到真实执行节点。远程节点需具备 `script_modules_v1` capability；API 脚本同时执行结构化规则时还需 `api_script_assertions_v1`。每次运行的 `request_snapshot.diagnostics.modules` 记录项目、release、content hash 和模块 hash，便于确认实际使用的版本。公共方法回退只切换 published 指针，任务自己的脚本不会被改写。
