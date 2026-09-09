# 项目公共方法库的存储、验证与发布

状态：接受。日期：2026-09-09。

## 当前界面语义修正

公共方法页面采用“列表、新建、编辑 Python、保存即生效”的单次保存流程，不要求用户理解版本、受影响任务、验证或发布。后端新增 `PUT /api/projects/{project_id}/library`，以 `expected_revision` 对当前生效库做 CAS，并在一个事务内完成整库静态校验、不可变内部快照写入和 published 指针切换。新运行继续通过现有 runner 在运行开始时捕获当前生效 bundle，因此正在运行的 snapshot 和历史运行关联不变。

`GET /library` 增加 `effective_revision` 与 `effective_modules`；它们只反映当前 published 指针，遗留 draft 不会因为读取而被提升。旧的 draft/validate/publish/rollback 路由、表和迁移/验收脚本继续保留，作为兼容边界，不再是当前界面的操作链。回退会推进兼容 CAS 编辑标记，避免旧版本指针切换造成 ABA 保存。

## 背景

多个项目任务需要复用准备请求、签名和解析等公共 Python 方法。源码必须与项目隔离，任务执行只能读取当前生效快照；整库保存需要在并发写入和引用删除时保持原子性。旧验证和发布流程仍用于历史迁移与兼容消费者。

## 决策

- SQLite 保存项目级草稿、不可变 versions、published 指针、验证记录及冻结任务快照；公共库属于项目数据，随项目删除保护和数据库备份/恢复处理。
- 草稿使用 `expected_revision` CAS。发布在一个 `BEGIN IMMEDIATE` 事务内重读候选、引用、任务配置和执行依赖指纹；网络执行在事务外完成。
- runtime `script_modules.py` 独占源码归一化、AST 策略、导出和 bundle hash。storage adapter 只保存源码、描述并转换 UI 导出元数据，不复制 AST 规则。
- 任务通过 `ctx.modules.use("模块名")` 显式取得模块，并按导出签名传入参数；异步导出由调用方 `await`，常量使用属性访问。模块源码不得再次调用 `ctx.modules.use()`，也不能通过导入或加载时调用形成跨模块依赖。
- 验证通过 runtime 的 `run_library_validation` 走真实 runner/browser 选择。结果保存完整脱敏 runs，使用 `library_validation` observation；不改变 health、trend、告警或业务通知。受影响启用任务全部成功才允许带引用发布；无引用只做静态兼容检查。
- 依赖指纹覆盖候选源码、引用任务配置、项目变量、有效全局与项目环境变量、相关执行设置和 runner/browser 能力，并保存环境变量/相关设置的持久编辑标记，防止 A→B→A 的值恢复复用旧验证。敏感值只存摘要哈希；健康/last-seen/run 状态等观测字段排除。
- 验证和远程分发使用验证开始时捕获的完整源码 bundle；bundle 带 `project_id`、`release_id`、release/content hash 和模块 hash。远程节点不得回查本地项目库，必须声明 `script_modules_v1`；API 脚本与结构化规则并用时还需 `api_script_assertions_v1`。
- 回退只切换历史版本指针并做当前引用静态兼容检查，不重新执行外部请求。服务重启把遗留验证标记为 cancelled 并记录诊断和审计，不自动重放。
- 项目导出升级为 v2，只带当前 published 源码；v1 仍可导入。导入把 library 与任务、变量和告警放在同一事务中，同源复用、异源预检拒绝；新引用任务默认停用，已有 draft 不得被隐式发布。
- 当前公共方法 UI 使用整库保存接口；它复用现有表和 published 指针，不新增 schema，不运行网络验证，也不把旧 draft 自动发布。静态删除引用检查失败时整笔事务回滚；旧版本、验证记录和运行历史继续保留。

## 选择的边界

版本快照而不是对任务执行时动态读取 draft，保证一次发布内所有任务看到同一源码。验证依赖存为冻结 JSON，而不是在执行完成后重新读取当前任务，避免修改后又恢复原值时错误显示成功。任务脚本与公共方法版本分别持有，回退只移动方法 published 指针，不修改任务脚本。SQLite `BEGIN IMMEDIATE` 作为单实例 CAS 的存储边界，保持现有 WAL 和连接关闭约束，不引入新的队列系统或工作流引擎。

## 后果与限制

验证期间修改任务、变量、相关设置或 runner 能力会使验证失效并要求重跑。远程 runner/browser 不可用会出现在完整 run 矩阵中，并阻止受影响启用任务发布。停用任务可显式验证，但它们不构成发布门禁。回退保证静态兼容，不保证外部服务当前可达。旧 v1 包没有公共方法快照，导入不会凭空创建库。

## 验证依据

专项测试覆盖草稿 CAS 和空库读、项目 header/归档只读、版本指针及回退依赖、并发事务、全局环境变量指纹失效、服务重启中断验证、导入冲突与事务回滚、旧 schema 初始化、ASGI `202` 验证和 inspect API published bundle。后端全量测试使用隔离 SQLite；发布前仍需在目标部署副本上执行现有迁移/备份预检。
