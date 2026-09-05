# UI 前置脚本与运行时变量

- Status: accepted
- Date: 2026-09-04
- Owners: PulseGuard maintainers
- Scope: variables / RunContext / UI preparation / project configuration
- Supersedes: none
- Superseded by: none

## Context

目标页面地址可能依赖实时接口数据。用户要求脚本产生临时变量、URL 引用变量，并在选择元素前完成同一条准备流程；方案必须通用且默认不滚动。

## Evidence

- 现有 RunContext 初始化立即解析 entry_url，无法引用脚本生成的值。
- 扫描与规则检查提前捕获 URL；结构化执行提前解析断言，三个入口不一致。
- 原 environment_variables 是全局设置；项目已有明确的请求头隔离边界，但没有项目变量存储。
- 实际商品接口在相邻请求间可返回不同首条记录，因此不能把预览样本回写为最终静态 URL。

## Decision

1. RuntimeVariables 持有独立 settings 副本。本次临时值覆盖项目/全局默认值，不写数据库或进程环境。
2. prepare_ui_target 是扫描、规则检测和结构化执行的唯一准备入口：setup → URL resolution → navigation。断言随后使用同一上下文解析。
3. 复用 Python 标准库 URL 编码、httpx 和 Playwright；不增加工作流引擎、JSONPath 依赖或站点专属分支。
4. 项目变量保存在按 project_id 隔离的新表，复用现有变量校验和编辑器。主节点分发有效配置快照，子节点不查询自己的同号项目。
5. 请求证据保留模板与真实 URL、变量、准备时间和最后一次 HTTP 快照，运行时密钥在每次尝试结束前脱敏。
6. 扫描/检测接口在既有字符串 detail 旁新增可选 diagnostics，前端与运行历史消费本次快照。统一记录阶段、变量状态、最近 10 次 ctx.http 请求和响应；不对脚本业务错误做文本猜测。失败保留证据并可重试，输入变化使旧异步结果失效。

## Alternatives considered

- 只在 setup 中覆盖 ctx.entry_url：未覆盖扫描、断言和项目隔离，拒绝。
- 每次脚本更新持久变量：引入并发覆盖、预览副作用与陈旧值，拒绝。
- 独立数据提取工作流：当前可用已有脚本表达，暂不引入。

## Consequences

- 原静态任务、全局/进程变量和 API URL 替换不变；新的项目/临时变量通过运行配置元数据声明原始值语义，在 UI 查询参数中编码。
- 表创建是增量迁移；不自动移动全局变量。项目配置包新增可选 variables，缺省不修改已有项目变量。
- 老执行器无法执行 ctx.vars 脚本，发布必须同步目标执行节点；不进行静默降级。
- 不依赖新库、不改变脚本可信工具边界；截图/Trace 不提供像素脱敏。
- 回滚应用使用匹配版本的脚本/配置与部署前备份；新增表可保留但旧版本不会读取，回滚前禁用动态变量任务。

## Verification

- 单元及集成测试覆盖三个入口、缺失/失败、编码、并发、密钥、项目隔离和既有行为。
- 仓库全套后端测试和前端构建；独立临时数据库和真实浏览器中验证变量保存、元素扫描与动态商品详情拨测。

## Revisit when

- 用户需要事务性持久写变量、跨步骤浏览器会话复用或批量多商品拨测。
- 多实例部署、严格租户安全隔离或混合版本执行能力协商成为明确要求。
