# MCP 运营能力与执行记录分层保留

- Status: accepted
- Date: 2026-09-24
- Owners: PulseGuard maintainers
- Scope: storage retention, run-statistics API, MCP adapter and settings consumers
- Supersedes: none; extends [Skill/MCP decision](2026-09-09-skill-mcp.md)
- Superseded by: none

## Context

用户要求 MCP 不限于查询次数，并希望执行记录在磁盘允许时保留更久、详细数据短期保留。
现有 runs 同时承载索引元信息与大体积日志/快照，统一 TTL 和同错整行合并使历史查询失真。
继续保留 SQLite 单实例模型，不引入另一套运行器或分析数据库。

## Evidence

| Claim | Kind | Source and environment | Observed | Reference | Freshness / redaction |
| --- | --- | --- | --- | --- | --- |
| 统一 TTL 和同错压缩删除运行行 | fact | repository storage | 2026-09-24 | `_cleanup_old_data`, `_compact_repeated_failure_runs` | 原实现 |
| 日归档无法按任务/节点或日内时刻拆分 | fact | repository schema | 2026-09-24 | `run_archives`, `_archive_runs` | 无密钥 |
| 最近 1000 条详情平均约 17 KB，清理后含全部现有索引约 1.21 KB/条 | sample | Volcano readonly SQL + in-memory SQLite copy | 2026-09-24 | `.tmp/mcp-retention-sizing.sh` | 仅返回尺寸，不导出日志 |
| 近一天约 33009 条、可用磁盘约 26.8 GB | sample | Volcano readonly count + filesystem stat | 2026-09-24 | same sizing script | 瞬时样本，不是容量保证 |

## Decision

1. 保留每次执行的 ID、项目/任务、时间、类型、节点、来源、状态、耗时、失败类别与通知状态。
   默认轻量 TTL=365 天，上限3650；详情 TTL=30 天，上限365，以更短者为准。
2. 添加 `runs.details_pruned_at` 和仅覆盖尚未清理终态行的部分索引。分批事务清理日志、调用栈、
   请求响应快照，错误简述截断1000字符。运行中/待执行行不清理。同错只清理超出保留数量的详情和附件。
3. 旧实例首次迁移保留原 TTL 作为详情期限，轻量 TTL 至少365天；新字段存在即不再覆盖用户设置。
   `run_history_policy` 记录新策略启用时间，用来保守判断历史完整性。不重建已消失的运行行。
4. 新统计 API 使用必填带时区的半开范围、项目隔离、同一读快照合并轻量行与互斥日归档。
   明确来源、状态口径、被排除归档量及历史缺口。健康趋势不代替执行总量。
5. MCP 增加概览/统计/趋势/异常/审计，扩充记录筛选。执行、配置启停、实例级只读分别开关，默认关闭。
   新写工具仍调用现有 API，项目校验、白名单输出、结果未知不自动重试。平台只读有意不受项目私有范围约束。

## Alternatives considered

- 只延长旧 TTL — 会同时长留大体积快照，未修复同错删除导致的统计损失。
- 永久保存执行行 — 当前每天约3.3万条，含索引一年粗估14.6GB，尚有详情/附件/备份，不能视为零成本。
- 单独历史库或新的执行事实表 — 当前单实例约束下增加迁移与双写复杂度；先用现有索引和轻量行。
- 仅统计趋势 — 健康口径排除节点失败、跳过等，不符合执行量。
- 所有管理 API 直接映射 MCP — 扩大写入和敏感配置面；本次仅开放独立授权的启停与恢复确认。
- 持久化批量/幂等提交、任务创建编辑与回滚 — 由后续 [任务写入契约](2026-09-24-mcp-task-workflows.md) 独立定义。

## Consequences

- Positive: 详情清理不再毁掉逐次执行身份、计数或最近运行引用；客户端明确证据是否可用。
- Negative: 仍有索引和轻量行增长；长区间分组可能较慢；历史丢失不可恢复，归档筛选能力有限。
- Operational: 生产部署前必须备份活动 DB、在副本运行 init_db 并 quick_check、确认迁移行数与 TTL，
  观察部分索引构建时间/空间和首次详情清理的 WAL/锁等待。
- Recovery: 列/表迁移为增量；回退旧程序会恢复旧整行清理语义，因此回退必须评估 TTL 并暂停清理。
  已清理详情不能靠代码回退找回，需受控恢复备份并核对备份后的新数据；不自动覆盖活动 DB。
- Disk: 清理可复用 SQLite 空闲页，文件未必立刻变小；不自动 VACUUM、不增加低磁盘删记录策略。

## Verification

- `backend/tests/test_run_history.py`: 旧 schema 升级幂等、详情与活跃行保留、归档前后计数相等、项目隔离、
  半开区间/时区/亚秒边界、不完整归档告警、详情清理后禁止伪比较。
- `backend/tests/test_storage.py`: 同错详情清理保留每个执行 ID 与附件 TTL 行为。
- `integrations/mcp/tests`: SDK 协议发现、独立权限、跨项目拒绝、未知结果不重试、脱敏与真实 stdio/API 链路。
- 隔离后端全量测试、前端构建及本地设置/详情页面浏览器验收；发布时另执行生产副本迁移预检。

## Revisit when

- 轻量记录/索引增长逼近磁盘预算、统计查询长尾不可接受，或部署模型变为多实例。
- 需要任务/节点维度跨保留期精确统计或历史日内查询时，设计有明确口径的聚合维度与保留合同。
- 需要可靠批量提交/可重试写入、全量配置管理时，先定义服务端幂等、版本校验、回执与审计契约。
