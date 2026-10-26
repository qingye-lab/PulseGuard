# 运行列表轻量投影与按 ID 读取

- Status: Accepted
- Date: 2026-10-24
- Scope: SQLite 运行列表的筛选、排序、精确计数与详情读取

## Context

2026-10-22 的请求已把普通分页的项目/来源总数从线性 COUNT 降到 `run_count_totals`，但其他筛选仍在 `runs` 上执行精确统计。线上排障样本中 `runs_count=23,323.94 ms`、匹配 811,300 条；只要带有状态、类型、节点、关键词或时间条件，计数仍会扫描宽表，覆盖索引也不等于能按选择性定位。

本轮采用“轻量记录表负责列表与统计，详情按 ID 查询”：筛选、排序和 COUNT 只访问 `run_records`，先选出本页 ID，再按主键从 `runs` 补齐摘要字段。`runs` 仍是唯一写入事实源。

## Evidence

- 本地 811,000 行机制基准（`.tmp/run-records-final-benchmark-20261024.json`）：失败页在只按时间索引规划时读取 106.03 MiB、中位 195.28 ms；改用按时间排序的失败部分索引后为 0.006 ms、0 MiB。失败计数 0.02 ms，旧 `runs` 计数 105.91 ms / 90.91 MiB。
- 同一基准：类型计数 17.208 ms / 14.07 MiB，节点计数 9.765 ms / 9.05 MiB，任务计数 2.247 ms / 2.03 MiB，告警状态计数 0.02 ms，项目/来源总数 0.003 ms；基础页选 ID 0.007 ms，12 条摘要补齐 0.049 ms。所有对照计数与 `runs` 精确值一致。
- 任意关键词计数使用窄表顺序扫描，中位 112.235 ms / 66.65 MiB。它仍随历史量增长，不承诺毫秒级；未采用“项目/来源+名称”大索引，因为 `LIKE '%term%'` 无法 seek。
- 历史回填 811,000 行约 6.81 秒，完整初始化约 11.0 秒（`.tmp/run-records-benchmark-20261023.json`）。
- 20,000 行单事务插入：无投影 2.265 秒、34.0 MB；有投影 3.269 秒、49.9 MB，写入耗时增加 44.3%，每行投影及索引约 794 字节。
- 真实旧库副本预演（`.tmp/pg-migration-preflight-20261024/legacy.db`，281 行，版本 1/2/3 → 5）：0.13 秒完成，`run_records` 与 `runs` 投影差异 0，计数分组全部一致，`PRAGMA integrity_check=ok`。

## Decision

增加 `run_records`，只保留列表需要的 13 个字段：`id, project_id, check_id, check_name, check_type, status, started_at, notification_status, runner_id, run_group_id, trigger, observation_kind, deduplicated_count`。迁移标记为 `schema_versions=5`。

同步由 SQLite 触发器完成，与原始写入同事务提交或回滚：

- `AFTER INSERT` 执行 `INSERT ... ON CONFLICT(id) DO UPDATE`；
- `AFTER UPDATE OF` 12 个投影字段时执行同一 upsert；
- `AFTER DELETE` 删除对应投影行。

因此任务状态、告警状态、节点、项目/来源变更、直接 SQL 写入和历史清理都无需业务代码双写；回滚不会留下计数增量。投影表可丢弃并从 `runs` 重建。

首次迁移在一个写事务内分批回填，每批 5,000 个 ID，使用主键范围而非 OFFSET。正常重启只比较表、触发器和版本标记；缺表、缺触发器或标记缺失时先重建投影，再对外服务。索引在回填后创建，避免逐行维护。

读取路径：

1. 只含项目/来源两个条件时，总数仍由 `run_count_totals` 提供。
2. 其他筛选在 `run_records` 上执行精确 `COUNT(*)`，不联 `runs`，不使用近似值或时效缓存。
3. 分页先按 `started_at DESC, id DESC` 在 `run_records` 上选 ID，再按主键从 `runs` 取摘要；详情接口继续按唯一 ID 查 `runs`。
4. `list_runs`（导出/API 列表）使用同一投影和补齐路径，保证与分页一致的语义。
5. 运行统计的保留区间查询改读 `run_records`，归档区逻辑不变。

索引按真实筛选维度设置，而不是一个覆盖全部字段的大索引：

- 项目时间、项目+来源时间作为基础页索引；
- 状态、告警状态、类型、节点、任务各一条“项目+来源+维度+时间”索引；
- `run_group_id` 使用非空部分索引；
- 失败标签会把 `status=failed` 展开为 `failed,timeout`，使用 `(project_id[, observation_kind], started_at DESC, id DESC) WHERE status IN ('failed','timeout')` 两个部分索引，避免先按状态枚举再临时排序；
- 页查询固定匹配当前筛选维度的索引。否则 SQLite 在 `ORDER BY ... LIMIT` 下可能沿时间索引寻找稀疏旧失败，或为 `COUNT(*)` 选择项目/来源索引后逐行回表；基准已复现这两类计划。
- 仅关键词（以及同样未建索引的 `trigger` 等值）计数使用 `NOT INDEXED` 顺序扫描窄表，避免 811,000 次随机行查找（旧计划 869.31 ms / 1,372.38 MiB）；有关键词加其他可索引条件时仍交给规划器使用维度索引。

不删除 `runs` 上既有的页面覆盖索引。它们当前不服务列表读取，但保留可减少代码回退时的计划退化；是否移除应在独立维护窗口按实际写入开销和回滚策略决定。

## Alternatives considered

- 继续扩大 `runs` 覆盖索引：上一版候选仍会扫描整个项目；实测 561 条失败记录计数读取约 100 MiB。覆盖索引不能替代正确的前导列和选择性。
- 近似总数或短时缓存：改变精确语义，且不能消除第一个冷请求的扫描。
- 仅缓存页面 ID 或前端分块：不解决筛选计数，深页和冷缓存仍然线性。
- 为关键词建立名称覆盖索引：`LIKE '%term%'` 无法 seek，索引只会增加写入和存储。
- 为列表预联 `runs` 宽字段：会在筛选前读取日志、快照和大错误文本，违背按 ID 补齐的目的。

## Consequences

- 普通筛选的计数与页面选择只读窄投影；失败等稀疏条件不再因历史长度产生随机宽表读取。
- 每条运行多一行投影和一次触发器维护。20,000 行批量插入的机制基准显示 44.3% 写入耗时增加；该数字包含投影表、触发器和索引，未在生产 I/O 限额下复测。
- 首次迁移需要一次全历史回填并持有写锁；811,000 行约 6.81 秒。部署前必须在数据库副本上执行迁移并核对行数、投影差异、计数分组和完整性。
- 关键词精确计数、深页 OFFSET 和其他尚未建立索引的组合仍可能随历史量线性增长；本次没有对它们作统一毫秒级保证。
- 投影是派生数据。源表回退不需要恢复投影；旧代码可继续写 `runs`，触发器继续维护投影和计数。若要移除结构，应作为独立维护操作。
- 触发器只覆盖已知的 12 个投影字段。若以后新增列表筛选字段，必须同时改表、触发器、索引和迁移版本。

## Verification

- `uv run python -m unittest discover -s backend/tests -p 'test_*.py' -v`：750 项，749 通过、1 项因 Windows 符号链接权限跳过，57.053 秒。
- 新增 `backend/tests/test_run_records.py`：分批回填、二次初始化不重填、INSERT/UPDATE/DELETE 触发器、非投影大字段不重写投影、范围变更回滚、缺触发器重建、21 组筛选/计数/分页与 `runs` 直接查询对齐、清理删除。
- 重写 `backend/tests/test_console_read_performance.py`：基础计数禁止历史 COUNT、各标签计数与页面只访问投影、稀疏失败页固定失败索引且无临时排序、关键词计数顺序扫描、统计只读投影、12 条按主键补齐。
- `backend/tests/test_run_page_counts.py` 与 `backend/tests/test_run_history.py` 继续覆盖计数汇总、快照一致性和时间语义。
- 真实旧库副本预演见上方 Evidence；尚未对生产副本执行迁移，也未测量生产接口的 `Server-Timing`。

## Revisit when

写入放大在生产 I/O 限额下造成可见延迟，失败/超时记录占比升高使部分索引变大，出现新的列表筛选字段，关键词计数成为常态入口，或部署模式改为多实例。届时重新评估投影字段、索引数量与统计所有权。
