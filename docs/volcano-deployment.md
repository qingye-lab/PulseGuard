# PulseGuard 火山部署说明

唯一本地源码目录为 `D:\project\PulseGuard`。构建、测试、MCP 和发布均从该目录执行；旧 Documents 路径仅作迁移兼容入口，不再维护独立源码树。

当前推荐拓扑：

- 火山服务器是公网主节点，默认地址 `http://115.190.188.243:8787`。
- 火山 relay 端口使用 `9443`，子节点只需要出站连接，不需要开放 `8788`。
- 本地机器作为 relay 子节点运行 `pulseguard-worker` 和 `pulseguard-relay-client`。
- 旧本地主节点容器可以先停掉，不删除数据卷和 `data/`、`reports/`。

通用的国内网络部署坑和 systemd 部署脚本见 [china-deployment.md](./china-deployment.md)。火山只是这套方法的一个实例，默认使用火山 PyPI 镜像和 `9443` relay 端口。

主机预览回收、压力准入、日志上限和维护资源限制见 [资源保护运维](./volcano-resource-guard.md)。

## 远端服务

不涉及依赖、环境或数据库结构变化的定向发布使用 `scripts/systemd_release.py`，在远端上传目录依次执行 `preflight`、`snapshot`、`apply`。上传目录包含 `manifest.json`、`stage/`、执行脚本和配套的 `deployment_backups.py`。manifest 的 `id` 为 32 位十六进制恢复点 ID，`files` 为相对源码路径到 `baseline`（原文件 SHA256，新增文件为 null）和 `sha256`（候选 SHA256）的映射。

预演使用真实数据库的隔离副本。快照冻结后恢复原实例，完整性检查和压缩继续处理不可变副本。切流只替换 manifest 指定的源码，任何部分替换失败都按事先保存的逐文件原件恢复；不替换在线数据库。验收按真实项目头及 `observation_kind=observation` 检查各个历史分页筛选；HTTP/结构/范围错误必须失败，延迟另行记录。验收成功后正式完成恢复点并执行现有保留规则；保留收尾失败单独处理，不回滚已验收代码。

火山当前使用 systemd 部署，根目录是 `/opt/pulseguard`：

```sh
systemctl status pulseguard.service pulseguard-relay.service
journalctl -u pulseguard.service -u pulseguard-relay.service --since "10 minutes ago" --no-pager
```

主节点关键环境：

```env
PULSEGUARD_ALERT_DETAIL_BASE_URL=http://115.190.188.243:8787
PULSEGUARD_RELAY_PUBLIC_HOST=115.190.188.243
PULSEGUARD_RELAY_PUBLIC_PORT=9443
```

重新部署火山 systemd 版本：

```powershell
.\scripts\deploy-china-systemd.ps1 `
  -RemoteEnvPath "D:\project\PulseGuard\remote.env" `
  -PublicHost "115.190.188.243" `
  -RelayPort 9443
```

脚本会保留 `data/`、`reports/` 和 relay 密钥，先在数据库副本上预演迁移；停服后再备份最新数据库并完成项目隔离切换。回滚必须配套恢复部署前源码包和 `pulseguard-before-cutover-*.db`。

## 本地子节点

本地 relay worker 目录通常是：

```powershell
C:\Users\Qa\Documents\PulseGuard-local-worker
```

重新使用当前源码镜像启动本地子节点：

```powershell
docker compose -f docker-compose.relay-worker.yml up -d --force-recreate pulseguard-worker pulseguard-relay-client
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" | Select-String pulseguard
```

子节点必须带 `PULSEGUARD_RUNNER_ID`，否则 compose 会直接失败，避免误创建重复 worker 身份。

## 数据同步

从本地主节点同步到火山：

```powershell
.\scripts\sync-volcano-main.ps1 -RemoteEnvPath "D:\project\PulseGuard\remote.env" -DeploymentMode auto
```

脚本会：

- 调用本地 `/api/database-backups` 创建在线 SQLite 备份。
- 打包 `pulseguard.db`、`runner-token.key` 和 `data/relay/`。
- 上传到火山并备份远端现有 `data/`、`reports/`。
- 只重启远端服务或容器。
- 等待远端 `/api/health` 恢复，并校验远端任务定义数量和指纹与本地一致。

`-IncludeReports` 只在必须迁移截图、Trace 和 Response Body 时使用，文件可能很大。

## 验证分布

查看最近执行是否按完整窗口 1:1：

```sh
/opt/pulseguard/.venv/bin/python - <<'PY'
import sqlite3
from collections import Counter

con = sqlite3.connect('/opt/pulseguard/data/pulseguard.db')
con.row_factory = sqlite3.Row
rows = con.execute("""
select id, runner_id, coalesce(runner_name, runner_id, 'local') runner_name
from runs
where created_at >= datetime('now', '-20 minutes')
order by id
""").fetchall()
ids = [row['id'] for row in rows]
print('gaps', [(a, b) for a, b in zip(ids, ids[1:]) if b != a + 1])
print(Counter((row['runner_id'] or 'local', row['runner_name']) for row in rows))
PY
```

单分钟 4:2 和 2:4 交替是正常的；按完整轮次、按单个任务累计才应该接近或等于 1:1。
