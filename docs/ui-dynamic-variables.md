# UI 动态 URL 与变量

页面选择器扫描、规则检测、试运行和定时拨测共用以下顺序：

读取项目配置 → 创建本次运行上下文 → 执行前置脚本 → 解析 URL → 加载目标页 → 扫描或校验。

不自动滚动、不自动加载更多。源页面需要筛选或滚动时，必须在脚本中明确操作。

## 变量

- 在「变量管理 → 项目变量」保存当前项目的配置。原「全局变量」保留。
- 优先级：本次临时变量 > 项目变量 > 全局变量 > 执行节点进程环境变量。
- `ctx.vars.get("NAME", default)` 读取有效值；`ctx.vars.set("NAME", value, secret=False)` 只写本次执行。
- 值支持文本、数字、布尔值，读取时为文本；null、对象和数组不能直接写入。先提取所需字段。
- 每次扫描、检测、执行及重试均创建独立变量上下文。预览不会回写项目配置；不同节点独立执行脚本。
- 不支持脚本自动写入持久项目变量，以免并发任务覆盖共享状态。
- UI URL、断言文本及 selector 都支持 `${NAME}`。断言在脚本完成后解析。
- URL 查询参数和 fragment 内的项目/临时变量按原始值编码一次；固定部分（例如 `sourceid=6868161%2C010`）保持原样。变量中保存原始图片 URL，不要预先对整个图片 URL 编码。嵌套 URL 内原有 `%20` 属于嵌套地址本身，外层编码后 `%2520` 是正确结果。
- 整个 URL / 基础地址占位符，例如 `${DETAIL_URL}`、`${BASE_URL}/detail`，保留文本替换语义。路径段需要转义时请在脚本中构造完整 URL，不能把任意值直接当作路径结构。
- 既有全局/进程变量保留原来的文本替换语义，API 任务也不变。若要对全局变量的原始值自动编码，可用 `ctx.vars.set("IMAGE", ctx.vars.get("GLOBAL_IMAGE"))` 后在查询参数中引用 `${IMAGE}`。

## 示例：接口取值，详情页拨测

在项目变量 `PRODUCT_API` 中配置商品列表接口。以下脚本的数据路径适配 `result.records`，换其他站点时仅修改脚本，不修改平台代码。

入口 URL：

```text
https://cartech.autohome.com.cn/usedcardetail/cardetail?platform=m&channel=unknown&business=unknown&sourceid=6868161%2C010&skuid=${skuId}&spuid=${spuId}&templeimgurl=${goodsImage640Url}
```

前置脚本：

```python
async def setup(ctx, page):
    response = await ctx.http.get("${PRODUCT_API}")
    ctx.assert_status(response, 200)
    data = response.json()
    if not isinstance(data, dict):
        ctx.fail("响应必须是 JSON 对象")
    if data.get("returncode") != 0:
        ctx.fail("商品接口业务状态异常")
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("records"), list):
        ctx.fail("响应字段 result.records 缺失或不是数组")
    records = result["records"]
    ctx.log("result.records 数量：" + str(len(records)))
    if not records:
        ctx.fail("result.records 为空，无法生成详情页变量，请检查接口筛选条件")
    item = None
    for index in range(len(records)):
        candidate = records[index]
        if isinstance(candidate, dict) and candidate.get("skuId") and candidate.get("spuId"):
            item = candidate
            ctx.log("选中 result.records[" + str(index) + "]，跳过无商品标识的记录")
            break
    if item is None:
        ctx.fail("result.records 没有同时具备 skuId/spuId 的商品记录")
    for field in ("skuId", "spuId", "goodsImage640Url", "goodsName", "goodsPrice"):
        value = item.get(field)
        if value is None or str(value).strip() == "":
            ctx.fail("选中商品缺少字段：" + field)
        ctx.vars.set(field, value)
    detail = item.get("usedCarDetail")
    city = detail.get("cityName") if isinstance(detail, dict) else None
    if not city:
        ctx.fail("选中商品缺少城市")
    ctx.vars.set("cityName", city)
```

点「选择元素」会先执行脚本，再扫描实际详情页。优先保存名称、城市、价格区域的结构化 selector。需要校验数据一致性时，在相应文本规则中使用 `${goodsName}`、`${goodsPrice}`、`${cityName}`，不要保存预览时的固定商品文字。

也可在前置脚本中使用 Playwright `page.expect_response`：先注册监听，再 `page.goto` 源页面，读取响应 JSON 后调用 `ctx.vars.set`。必须匹配明确的接口与分页/筛选条件。`ctx.http` 不共享浏览器 cookie；需要浏览器会话时用 `page.request` 或页面响应监听。

复现浏览器 cURL 时，将真实 URL（不是 Markdown 链接语法）保存为项目变量，并用 `ctx.http.get("${PRODUCT_API}", headers={...})` 显式传入其请求头。H5 页面模式不会自动修改 `ctx.http` 的 User-Agent、Origin、Referer 或其他请求头。接口列表可能混有广告等非商品记录，示例按 SKU/SPU 选择商品，不假设第一项就是商品；平台本身不识别这些业务字段。

## 失败、证据与部署

- 脚本失败、超时或 URL 缺少变量时停止，不回退到上一次商品。
- 阶段沿用任务超时，整条 UI 流程另受 `max_task_runtime_seconds` 上限约束；每次重试重新准备数据。
- 扫描和检测失败会保留错误与「执行详情」，可查看失败阶段、URL 变量状态、本次临时变量、最近 10 次 `ctx.http` 请求/响应及日志，然后重试。成功扫描也提供执行详情。修改 URL、脚本、页面模式或项目后旧扫描结果失效。
- 运行结果「请求」保存同一份诊断快照，不重新读取当前配置。响应正文最多 5000 字符、诊断文本限长；密钥先脱敏再截断。浏览器 `page.request` 和页面响应监听不会自动加入 `ctx.http` 快照，必要时在脚本中记录非敏感摘要。
- `inspect-ui` / `inspect-ui-rules` 的失败响应仍是 HTTP 400 与字符串 `detail`，新增可选 `diagnostics` 对象；成功响应也新增该对象，不改变原字段。脚本语法、排队超时、浏览器启动、前置脚本、URL 解析、导航、扫描及检测失败区分阶段。清理失败不覆盖原始失败。
- 平台不会推断任意脚本中的业务路径。示例通过脚本校验 `result.records` 结构并记录数量；接口合法返回空列表仍应停止，不能偷偷删除筛选条件、使用上次商品或把失败变成成功。
- `secret=True` 和敏感名称的变量在结果与日志中脱敏，包括编码后的 URL；截图/Trace 可能仍显示页面上的敏感内容，不是像素脱敏工具。
- 脚本是可信本地工具，不是安全沙箱。预览执行脚本可能调用外部系统，用户需控制脚本副作用。
- 项目变量通过 `GET/PUT /api/project-variables` 管理，项目归属沿用 `X-PulseGuard-Project-Id`。GET 不返回密钥原文。归档项目只读。
- 新增 `project_variables` 表，不改写现有全局变量。项目包增加可选 `variables` 字段；旧项目包不改动目标项目变量。
- 主节点与执行这些脚本的子节点需同步升级。旧执行器没有 `ctx.vars`，不能执行新脚本。本地预览通过不等于火山和子节点已升级。
