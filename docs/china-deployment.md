# 中国网络环境部署

国内服务器部署 PulseGuard 的主要坑不在业务代码，而在依赖源和浏览器运行时：

- Docker 冷构建会拉 Docker Hub、GHCR、MCR，国内网络经常失败。
- Playwright 镜像和浏览器系统依赖缺失时，UI 任务会启动失败。
- PyPI/npm 默认源慢或失败。
- systemd 部署必须显式设置 `PULSEGUARD_STATIC_DIR`，否则前端静态文件不会按预期提供。
- 迁移主节点时必须同步 `runner-token.key` 和 `data/relay/`，否则已有子节点 token 或 relay fingerprint 会失效。

## 推荐路径

1. Docker 基础镜像可稳定拉取时，用标准 Compose：

```powershell
docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d
```

2. Docker 基础镜像拉取不稳定时，用 host/systemd 部署：

```powershell
.\scripts\deploy-china-systemd.ps1 `
  -RemoteEnvPath "D:\project\PulseGuard\remote.env" `
  -PublicHost "<公网 IP 或域名>" `
  -RelayPort 9443
```

脚本会在本地运行后端测试和前端构建，打包源码，上传到远端，保留远端 `data/`、`reports/`、`.venv`，使用固定版本 `uv` 和仓库 `uv.lock` 同步后端依赖。若远端已有数据库，脚本会先通过 SQLite backup API 创建带 SHA-256 的一致性备份，并在副本上执行数据库迁移预检；只有预检通过后才停止服务、安装 Chromium 并创建/重启：

- `pulseguard.service`
- `pulseguard-relay.service`

OpenSSH 可能会提示输入远端密码；脚本不会把 `remote.env` 里的密码拼到命令行。

## 可换镜像源

默认 PyPI 镜像：

```powershell
-PyPiMirror "https://mirrors.ivolces.com/pypi/simple"
```

npm 推荐在部署机或 CI 里提前配置：

```powershell
npm config set registry https://registry.npmmirror.com
```

Ubuntu apt 源建议优先使用云厂商同地域镜像。火山云可优先使用火山镜像；其他云用各自内网/同地域镜像。

## 端口

主节点 API 默认 `8787`，relay 默认 `9443`。relay 子节点只需要出站连接主节点 relay 端口，不需要开放 `8788` 入站。

## 部署后检查

```powershell
Invoke-RestMethod http://<公网 IP 或域名>:8787/api/health
Invoke-RestMethod http://<公网 IP 或域名>:8787/api/settings
Invoke-RestMethod http://<公网 IP 或域名>:8787/api/relay/status
```

远端 systemd：

```sh
systemctl is-active pulseguard.service pulseguard-relay.service
journalctl -u pulseguard.service -u pulseguard-relay.service --since "10 minutes ago" --no-pager
```

## 数据迁移

主节点数据迁移不要直接复制正在写入的 SQLite 文件。先用应用在线备份，再同步：

```powershell
.\scripts\sync-volcano-main.ps1 -RemoteEnvPath "D:\project\PulseGuard\remote.env" -DeploymentMode auto
```

该脚本会同步 `pulseguard.db`、`runner-token.key` 和 `data/relay/`，并校验远端任务定义数量和指纹。
