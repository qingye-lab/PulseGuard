# 数据库与部署备份保留

- Status: Accepted
- Date: 2026-09-15
- Scope: 在线数据库备份、火山 systemd 部署与数据同步脚本

## Context

实例设置 `database_backup_retention=2`，但服务器曾保留 5 组部署备份、合计约 6.02 GiB。部署和数据同步脚本各自生成备份，没有统一的保留步骤；应用备份清理又把新副本排除在数量预算之外。

## Evidence

- `storage._create_database_backup()` 保护新文件，而旧清理算法仍另外保留 N 份。
- `deploy-china-systemd.ps1` 和 `sync-volcano-main.ps1` 原先直接写入 `/opt/pulseguard-backups`，未读取保留配置。
- 增量发布曾只保存部分源码，不能仅按目录日期删除任意历史目录并假定恢复链完整。

## Decision

1. 沿用 `database_backup_retention`，在线备份与部署恢复点分别按 N 份计数，不增加第二个设置。修改配置后，下次创建在线备份或完成部署时应用新的数量。
2. 在线备份保留最新 N 份，新副本占用名额。恢复时保护恢复源和安全副本；N=1 时允许这两份暂时超限，后续普通备份恢复正常上限。
3. 部署入口统一调用标准库工具 `scripts/deployment_backups.py`。暂停所有写入者后 `create`，保存压缩的 SQLite 在线快照、完整应用源码归档，以及 runner 密钥和 Relay 等运行配置；数据同步还保存报告。排除运行数据库副本、历史备份、虚拟环境、node_modules 等，避免重复备份。
4. 每组恢复点使用随机 32 位十六进制 ID，目录权限为 0700；清单记录文件大小、SHA-256 与状态。数据库先做 quick_check，再验证压缩往返。发布健康及数据验收成功后 `complete`，校验新组文件再删除超额的已完成组。完整源码组彼此独立，不依赖增量恢复链。
5. 保留未完成/失败、未知格式、有额外文件或符号链接的组，必须人工检查。锁文件阻止并发整理；进程被强制终止留下的 `.retention.lock` 应在确认无活动操作后人工移除。旧版本目录不会被自动接管。
6. `complete --dry-run` 只输出拟删除目录，不改变状态或删除备份。重复完成同一最新组可安全重试；拒绝用较早部署的完成请求清理较新恢复点。

## Operational usage

正式脚本自动完成创建与验收后的保留步骤。定向发布也必须使用同一入口，不能继续自行写入无保留策略的备份目录。工具要求 Python 3.11+，默认应用数据库为 `<app-root>/data/pulseguard.db`。

```sh
# 使用本次发布的唯一 ID；在停止应用及 Relay 写入后运行。
python3 scripts/deployment_backups.py create --id "$release_id" --app-root /opt/pulseguard
# 数据同步使用 create --include-reports。

# 发布验收通过后，retention 取当前 /api/settings 的 database_backup_retention。
python3 scripts/deployment_backups.py complete --id "$release_id" --retention "$retention" --dry-run
python3 scripts/deployment_backups.py complete --id "$release_id" --retention "$retention"
```

恢复需要停止写入，配套恢复 `source.tar.gz`、解压 `database.db.gz` 至 `data/pulseguard.db`、恢复 `runtime.tar.gz` 中的配置，并按旧锁文件重建依赖。清除待恢复目标的旧 WAL/SHM 必须在写入停止后进行。部署备份不默认包含历史报告；同步备份包含报告。该工具不自动执行恢复。

## Alternatives considered

- 只修复应用计数：不能解决部署备份积累。
- 对全部目录按日期删除：可能删掉未知数据、失败恢复点或增量恢复链。
- 额外部署保留配置：容易再次与界面设置脱节，当前单实例场景不需要。

## Consequences

正常成功部署后，受管恢复点总数符合配置；每次部署只持久化一份压缩数据库。失败操作可能暂时超限，以保留人工恢复所需证据。保留数量不是空间配额；数据库或同步报告变大时，单份备份仍可能变大。

## Verification

回归覆盖应用总份数、恢复保护、压缩数据库往返、源码/密钥/报告归档边界、已完成组清理、失败和损坏备份保护、预览、乱序完成、锁与路径边界。正式部署需另行授权并进行目标环境验收。

## Revisit when

部署改为多实例、数据库或备份目录可配置，或需要空间配额、异地备份、失败恢复点自动处置时重新评估。
