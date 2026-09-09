# Skill + MCP 接入

PulseGuard MCP 是由 AI 客户端启动的本地 stdio 进程，通过 HTTP API 连接已有
PulseGuard 实例。执行仍由服务端的运行器负责，保留项目隔离、节点选择、并发控制、
历史记录和告警规则。MCP 不读取 SQLite，也不启动第二套运行器。

## 安装与连接

控制台左侧「Skill / MCP」（`/integrations`）提供连接配置生成、Skill 下载、请求示例和常见问题。填写 AI 客户端所在电脑的仓库绝对路径及可达服务地址后，可复制当前项目的只读配置；此页面不会自动安装或修改客户端配置。前端构建直接打包仓库内的 Skill 原文，Docker 前端构建阶段也会复制该源文件。

要求 Python 3.11–3.13、uv，以及已经运行的 PulseGuard 服务。在仓库根目录执行：

```bash
uv sync --locked --project integrations/mcp
```

在支持本地 stdio MCP 的客户端中添加下面的服务配置。`mcpServers` 是常见 JSON
配置形式；不同客户端的配置文件位置和字段外层不同，但启动命令、参数和环境变量相同。
将目录改为本机仓库绝对路径，将项目 ID 改为实际项目。配置会在客户端启动 MCP
进程时生效，仓库内的示例不会自动注册或修改现有客户端配置。

```json
{
  "mcpServers": {
    "pulseguard": {
      "command": "uv",
      "args": [
        "run", "--locked", "--directory",
        "C:/Users/Qa/Documents/PulseGuard/integrations/mcp",
        "pulseguard-mcp"
      ],
      "env": {
        "PULSEGUARD_MCP_BASE_URL": "http://127.0.0.1:8787",
        "PULSEGUARD_MCP_PROJECT_IDS": "1",
        "PULSEGUARD_MCP_ALLOW_EXECUTION": "false"
      }
    }
  }
}
```

首次连接后调用 `health`、`list_projects`、`list_checks`。需要执行能力时，由连接的
管理者把 `PULSEGUARD_MCP_ALLOW_EXECUTION` 改为 `true` 并重启连接；工具列表才会增加
`run_check`。查询和排障可以在默认只读配置下完成。

## 进程配置

| 环境变量 | 默认值 / 含义 |
| --- | --- |
| `PULSEGUARD_MCP_BASE_URL` | `http://127.0.0.1:8787`；服务根地址，可带反向代理路径前缀，不带 `/api` |
| `PULSEGUARD_MCP_PROJECT_IDS` | 必填，逗号分隔的正整数；只有管理者明确配置 `*` 时允许所有项目 |
| `PULSEGUARD_MCP_ALLOW_EXECUTION` | `false`；仅接受 `true` 或 `false` |
| `PULSEGUARD_MCP_API_TOKEN` | 可选，作为 Bearer 传给上游鉴权网关；不是 PulseGuard 只读快照 Token 或 Worker Token |
| `PULSEGUARD_MCP_REQUEST_TIMEOUT` | 读取响应的超时秒数，默认 30，范围 1–120 |
| `PULSEGUARD_MCP_EXECUTION_TIMEOUT` | 执行响应的超时秒数，默认 300，范围 1–1800 |

连接阶段超时为 10 秒。HTTP 客户端保留系统代理与 CA 环境配置，支持 SOCKS；需要排除
本地服务时用标准 `NO_PROXY`。上游重定向不会自动跟随。携带 Token 的远程连接要求
HTTPS；Token 不放进 URL、命令参数或提交到仓库的配置文件。

本实现只提供本地 stdio 传输，不监听 MCP HTTP 端口。服务地址、项目范围和执行开关
来自启动配置，模型不能通过工具参数更换服务器或扩大范围。这些约束只适用于本 MCP
进程，不是 PulseGuard 管理 API 的用户鉴权系统，也不能阻止有直接 API 访问权的调用方。
当前普通管理 API 没有统一自动化 Token 鉴权；远程实例应由已有的可信网络、隧道或
鉴权网关保护。`PULSEGUARD_MCP_API_TOKEN` 仅在网关实际验证它时有效，不会凭空使后端
具备认证。不要把本实现作为直接公开管理 API 的理由。

## 首版工具

工具名的客户端前缀可能不同，以下为协议中的原始名称。

| 工具 | 能力 |
| --- | --- |
| `health` | 服务可达性、项目范围和执行开关；不代表监测目标健康 |
| `list_projects` | 分页返回进程范围内的项目 ID、名称和状态 |
| `list_checks` | 按项目、类型、完整标签和启用状态筛选，提供分页 |
| `get_check` | 任务元信息、访问地址和健康状态，省略脚本、请求头和密钥配置 |
| `list_runs` | 按项目、任务和状态分页读取运行记录；`failed` 包含失败和超时 |
| `get_run` | 运行结果、失败来源和产物是否存在，可按需读取有长度限制的日志 |
| `get_failure_summary` | 服务端失败摘要、诊断信号与下一步 |
| `compare_success` | 对比该任务最近成功运行；无基线时返回 `available=false` |
| `get_screenshot` | 根据项目内的运行记录读取其 PNG 图片，最大 8 MiB |
| `run_check` | 配置启用后出现；执行一个已有任务并等待返回 |

所有项目工具必须传正整数 `project_id`；查询不在配置范围内的项目会在发出 HTTP
请求前拒绝。上游 API 继续验证资源归属。每页最多 100 条。上游目前不支持任务列表
分页，因此 `list_checks` 在 API 返回列表后筛选分页；响应超过 8 MiB 会明确报错，
不会把不完整列表当成功返回。

JSON 文本输出会省略非必要字段、遮盖已知敏感字段与 URL 查询值，并限制日志及嵌套
内容长度。任意业务正文、任务名称或图片仍可能包含敏感信息，脱敏不是保密保证。
原始响应正文、完整 Trace、配置脚本以及密钥不通过首版工具直接导出；需要时在控制台查看。

### 执行语义

`run_check` 复用现有 `POST /api/checks/{check_id}/run`，会执行当前保存的任务定义。
这是正式观测：可以运行用户脚本、向目标发送有副作用的请求、改变健康状态并发送告警。
首版不提供独立验证模式，不修改现有服务端触发来源（记录仍为 `manual`）。

成功调用工具与探活通过是两件事。正常返回中 `passed` 仅在运行状态为 `ok` 时为
`true`；`failed`、`timeout`、`skipped` 均为 `false`。权限、网络、响应格式等工具错误
通过 MCP 的错误结果返回。

当前 API 同步等待执行完成，没有独立的提交回执或幂等键。请求超时、上游 5xx 或
断开连接时，MCP 返回“执行结果未知”，不自动重试。客户端取消等待也不证明服务端
任务已取消。通过同一项目和任务的 `list_runs` 对照时间、状态核对；存在并发执行时，
不能仅凭最新一条记录确定请求关联。调整 MCP 客户端自己的工具等待超时，使其覆盖
预期执行时间；只增加本进程的 HTTP 超时不能改变客户端超时。

## 配套 Skill

入口为 [pulseguard-operations](../.agents/skills/pulseguard-operations/SKILL.md)。它指导 AI
选择项目、按证据区分目标与执行环境失败，以及在已有授权范围内执行任务。Skill 不
替代 MCP 的代码约束或 API 的部署鉴权，也不额外授予重跑、通知或配置修改权限。

支持仓库 `.agents/skills` 的客户端可在该项目中发现此 Skill。其他客户端可将整个
`pulseguard-operations` 文件夹安装到其支持的 Skill 目录，并连接上面的 MCP 服务。
复制后的 Skill 是自包含的，不依赖仓库绝对路径。示例请求：

- “查看指定项目最近有哪些失败，先不要执行。”
- “分析这次超时，是执行节点问题还是目标接口问题？”
- “执行指定项目的这条探活，给出运行 ID 和结果。”

## 验证与开发

MCP 使用独立的 `integrations/mcp/pyproject.toml` 和 `uv.lock`；后端依赖及 Docker
镜像不需要随 MCP 安装或升级。首次执行真实 API 集成测试前，先同步根目录后端环境：

```bash
uv sync --frozen --no-dev --no-install-project
uv sync --locked --project integrations/mcp
uv run --locked --project integrations/mcp python -m unittest discover -s integrations/mcp/tests -p "test_*.py" -v
uv lock --check --project integrations/mcp
uv build --project integrations/mcp
```

协议测试使用官方 SDK 客户端，覆盖输入校验、工具权限提示、项目范围、错误与未知结果、
输出处理和截图路径约束。集成测试用随机本地端口、临时数据库和真实 API/运行器验证
`stdio → API → 探活 → 历史与失败对比`，禁用无关的调度和节点轮询，目标仅为临时本地接口。
它不读取部署数据库或修改运行实例。缺少根目录 `.venv` 时该用例明确跳过；可用
`PULSEGUARD_TEST_BACKEND_PYTHON` 指定已安装后端依赖的 Python 解释器。

协议实现采用 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。
设计边界见 [决策记录](./decisions/2026-09-09-skill-mcp.md)。
