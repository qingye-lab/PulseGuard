# 项目公共方法库

项目公共方法是项目级、可复用的 Python 源码。任务的 `script` 和 `setup_script` 通过 `ctx.modules.use("模块名")` 引用当前生效的源码快照。源码由运行时统一归一化、解析导出和计算不透明哈希；API 返回的 `exports` 是导出元数据，执行 bundle 内部只携带运行时定义的导出名。

所有请求都必须带 `X-PulseGuard-Project-Id`，并且与 URL 中的 `project_id` 相同。读取接口可以查看归档项目的历史数据；保存只接受 active 项目。跨项目的路径、请求头、版本、验证或 bundle 不会被接受。

## 当前界面保存契约

公共方法页面只编辑模块列表和 Python 源码，保存一次即成为后续新运行使用的整库快照。页面使用：

```http
GET /api/projects/{project_id}/library
PUT /api/projects/{project_id}/library
```

保存请求：

```json
{
  "expected_revision": 3,
  "modules": [
    {
      "name": "auth_helpers",
      "source": "def make_headers(token):\n    return {\"Authorization\": f\"Bearer {token}\"}\n",
      "description": "生成目标请求所需的请求头"
    }
  ]
}
```

`expected_revision` 是当前生效库的单调编辑令牌；没有已保存快照时为 `0`。服务端在一个 SQLite 事务中完成 CAS、源码归一化、AST/导出签名检查、全部任务引用检查、不可变内部快照写入和当前指针切换。源码中的 `hash`、`exports` 或签名字段不会被信任。删除仍被任务 `ctx.modules.use()` 引用的模块或导出会返回 `400`，事务不会留下部分保存；令牌过期返回 `409` 并包含当前修订号。

GET 和保存响应保留旧的 `modules`、`draft_revision`、`published` 等字段供迁移脚本和旧 API 使用，并增加：

```json
{
  "effective_revision": 4,
  "effective_modules": [
    {"name": "auth_helpers", "source": "...", "description": "...", "exports": [], "hash": "opaque"}
  ]
}
```

新界面只使用 `effective_modules` 和 `effective_revision`。存在遗留未发布 draft 时，`effective_modules` 仍只读取当前生效指针；不会因为 GET 或保存而隐式提升旧 draft。当前运行已捕获的 bundle 和历史运行诊断保持不变，下一次新运行从保存后的生效指针读取。

## HTTP 接口

接口返回 JSON。草稿保存使用修订号比较：

```http
GET  /api/projects/{project_id}/library
PUT  /api/projects/{project_id}/library
PUT  /api/projects/{project_id}/library/draft
GET  /api/projects/{project_id}/library/references
GET  /api/projects/{project_id}/library/versions?limit=100
GET  /api/projects/{project_id}/library/versions/{version_id}
POST /api/projects/{project_id}/library/validate
GET  /api/projects/{project_id}/library/validations/{validation_id}
POST /api/projects/{project_id}/library/validations/{validation_id}/cancel
POST /api/projects/{project_id}/library/publish
POST /api/projects/{project_id}/library/rollback
```

草稿请求示例：

```json
{
  "expected_revision": 3,
  "modules": [
    {
      "name": "auth_helpers",
      "source": "def make_headers(token):\n    return {\"Authorization\": f\"Bearer {token}\"}\n",
      "description": "生成目标请求所需的请求头"
    }
  ]
}
```

服务端忽略请求中的 `hash`、`exports` 和导出签名字段，并从 `source` 重新派生。上面的整库 `PUT /library` 是当前界面保存入口，保存后新的执行读取新指针；旧的 `PUT /library/draft` 仍保留给迁移和兼容消费者，它只更新候选草稿，不改变当前执行指针。当前修订号冲突会返回 `409`，不会覆盖其他保存。

## 在任务中调用

公共方法只在任务的 `script` 或 UI 任务的 `setup_script`/`script` 中调用。`ctx.modules.use()` 必须接收一个模块名字符串字面量；取到模块后，按导出签名显式传入必填参数。异步导出需要 `await`，常量通过属性读取，不加括号：

```python
async def check(ctx):
    helpers = ctx.modules.use("auth_helpers")
    headers = helpers.make_headers(ctx.vars.get("TOKEN"))
    await ctx.request(headers=headers)
```

任务正常运行、任务 API preview、UI preview 和 Debug 使用当前项目的生效版本；公共方法源码自身不能调用 `ctx.modules.use()` 或依赖另一个公共方法，模块之间的组合应放在任务的 `check`/`setup` 入口中。模块源码由运行时 AST 校验，只允许说明、字面量常量和函数定义，不允许导入或加载时执行调用。

旧验证 API 请求为：

```json
{"candidate_revision": 4, "include_disabled": false}
```

接口立即返回 `202` 和 `validation_id`。验证结果包括 `status`（`queued`、`running`、`succeeded`、`failed` 或 `cancelled`）、候选修订号、`release_hash`、`content_hash`、诊断，以及 `checks[].runs[]` 中运行时返回的完整脱敏 run 结果。验证会冻结候选源码、引用任务配置、项目变量、有效全局/项目环境变量、相关执行设置和 runner/browser 能力。运行时通过 `run_library_validation(check, bundle, trigger="library-validation")` 使用真实配置的节点和浏览器矩阵；验证 run 不更新 health、trend 或通知。

候选 bundle 在验证开始前包含完整源码快照、每个模块的源码 hash 和候选 content hash。远程执行节点接收这份快照，不从自己的项目数据库重新解析方法库；节点需要声明 `script_modules_v1` capability。API 脚本同时保留结构化健康规则时，还需要 `api_script_assertions_v1`；不支持所需 capability 的节点会返回可定位的 runner 失败，并阻止受影响启用任务发布。

默认只验证受源码变化影响的启用任务；`include_disabled=true` 可额外运行停用任务，但停用任务失败不阻止发布。没有任务引用公共方法时，发布只需要静态引用检查，不需要虚构 `validation_id`。缺失模块、缺失导出或静态调用签名错误会在验证和发布前拒绝。

旧发布 API 请求为：

```json
{"candidate_revision": 4, "validation_id": 17}
```

有引用时，`validation_id` 必须是同一候选源码和同一依赖指纹的成功验证，并且所有受影响的启用任务都成功。无引用时可省略 `validation_id`。发布事务会重新读取修订号、引用、源码和依赖指纹，因此草稿或任务在验证后发生变化会返回 `409`。成功响应至少包含 `version_id`、`revision`、`release_id`、`release_hash` 和 `content_hash`；`release_id` 是正整数，候选 bundle 的 `release_id` 始终为 `null`。

回退请求为 `{"version_id": 12}`。回退只切换项目的 `published` 指针并记录审计；它要求历史源码对当前所有引用静态兼容，但不会为了回退再次发起外部 HTTP 请求。成功响应可以复用历史 `version_id`。验证或发布期间发生冲突时，原发布指针保持不变。

回退只还原公共方法的 published 源码快照。任务自己的 `script` 和 `setup_script` 独立保存在任务版本中，不会被回退动作改写；下一次任务运行或 preview 会按新的 published 指针重新捕获方法 bundle。没有引用公共方法的任务不会因回退而改变。

## 持久化和生命周期

每个项目最多一个草稿行。发布版本是不可变源码快照，按项目保存修订号、release/content hash 和源码；published 表只保存当前指针。验证表保存候选源码、冻结的任务快照、变更模块集合、依赖指纹和完整结果。指纹只纳入会改变执行含义的字段；健康状态、last-seen、队列计数和 run 状态等观测字段不会使结果失效。敏感变量值只参与内部哈希，接口和诊断不会返回原文。

环境变量和相关执行设置还带有持久编辑标记。即使值在验证期间经历 A→B→A，旧验证仍会因编辑标记变化而失效。

服务重启时遗留的 `queued`/`running` 验证会被标记为 `cancelled`，追加 `validation_interrupted` 诊断并写入审计；不会自动重放真实请求。项目删除会检查草稿、版本、发布指针、验证及其快照，含有任一库数据的项目不能按空项目删除。SQLite 备份/恢复复制整个数据库，因而包含这些表和 published 指针；恢复后仍执行正常初始化检查。

## 项目导入导出

项目导出使用 `pulseguard.project` v2，包含当前 published 源码快照，不包含草稿或历史版本。v1 项目包仍可导入，缺少 library 时按旧格式处理，不会凭空创建公共方法版本。导入会在任务、变量、告警和库的同一事务中完成：同名同源码模块复用，同名异源码在预检阶段拒绝；导入任务引用的模块、导出和静态调用必须与合并后的 published 库兼容。

导入新引用任务默认停用，避免导入后未经验证立即执行。已有未发布草稿不会被静默发布：已发布源码只合入导入模块，草稿继续保留自己的修改并合入新增模块。任一冲突、引用错误或后续写入失败都会回滚整个导入。
