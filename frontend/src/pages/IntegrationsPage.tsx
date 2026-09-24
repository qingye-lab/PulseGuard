import { App, Button, Card, Collapse, Form, Input, Space, Tabs, Typography } from "antd";
import { useState } from "react";
import { useProject } from "../ProjectContext";
import { copyTextToClipboard } from "../utils";
import skillSource from "../../../.agents/skills/pulseguard-operations/SKILL.md?raw";
import "./IntegrationsPage.css";

const { Paragraph, Text } = Typography;

export function IntegrationsPage() {
  const { currentProject } = useProject();
  const { message } = App.useApp();
  const [directory, setDirectory] = useState("");
  const [baseUrl, setBaseUrl] = useState("http://127.0.0.1:8787");
  const normalizedDirectory = directory.trim().replace(/\\/g, "/").replace(/\/+$/, "");
  const directoryValid = /^(?:[A-Za-z]:\/|\/)/.test(normalizedDirectory);
  const normalizedUrl = baseUrl.trim().replace(/\/+$/, "");
  let urlValid = false;
  try {
    const url = new URL(normalizedUrl);
    urlValid = ["http:", "https:"].includes(url.protocol) && Boolean(url.hostname)
      && !url.username && !url.password && !url.search && !url.hash
      && !/[\s\\?#]/.test(normalizedUrl);
  } catch { /* The field displays the validation error below. */ }
  const configuration = JSON.stringify({
    mcpServers: {
      pulseguard: {
        command: "uv",
        args: ["run", "--locked", "--directory", `${normalizedDirectory || "<本机仓库绝对路径>"}/integrations/mcp`, "pulseguard-mcp"],
        env: {
          PULSEGUARD_MCP_BASE_URL: normalizedUrl,
          PULSEGUARD_MCP_PROJECT_IDS: String(currentProject.id),
          PULSEGUARD_MCP_ALLOW_EXECUTION: "false",
          PULSEGUARD_MCP_ALLOW_CONFIGURATION: "false",
          PULSEGUARD_MCP_ALLOW_PLATFORM_READ: "false"
        }
      }
    }
  }, null, 2);
  const copy = async (value: string) => {
    try {
      await copyTextToClipboard(value);
      message.success("已复制");
    } catch {
      message.error("复制失败，请手动选择并复制文本");
    }
  };
  const prompts = [
    `查看项目「${currentProject.name}」（project_id=${currentProject.id}）最近有哪些失败，先不要执行。`,
    `分析项目「${currentProject.name}」（project_id=${currentProject.id}）最近一次超时，区分执行节点与目标接口问题，给出运行 ID 和证据，不要重跑。`,
    `使用 pulseguard-operations，列出项目「${currentProject.name}」（project_id=${currentProject.id}）的监控任务和当前健康状态。`
  ];

  return (
    <div className="page-content integrations-page">
      <Tabs defaultActiveKey="mcp" items={[
        {
          key: "mcp", label: "MCP 连接", children: (
            <div className="integrations-grid">
              <Card title="连接到 PulseGuard">
                <Paragraph>在运行 AI 客户端的电脑上安装 Python 3.11–3.13 和 uv，准备 PulseGuard 仓库，并确保可访问服务。</Paragraph>
                <Form layout="vertical">
                  <Form.Item label="本机仓库绝对路径" validateStatus={directory && !directoryValid ? "error" : undefined}
                    help={directory && !directoryValid ? "请输入绝对路径，例如 C:/Projects/PulseGuard 或 /home/me/PulseGuard" : "填写 AI 客户端所在电脑的路径，不是服务器或容器内路径。"}>
                    <Input aria-label="本机仓库绝对路径" placeholder="例如 C:/Projects/PulseGuard" value={directory} onChange={(event) => setDirectory(event.target.value)} />
                  </Form.Item>
                  <Form.Item label="服务地址" validateStatus={!urlValid ? "error" : undefined}
                    help={!urlValid ? "请输入不含账号、查询参数或片段的 HTTP(S) 地址" : "从 AI 客户端可访问的服务根地址，可带代理路径前缀，不带 /api。"}>
                    <Input aria-label="服务地址" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
                  </Form.Item>
                  <Form.Item label="允许访问的项目">
                    <Text>{currentProject.name} · ID {currentProject.id}</Text>
                  </Form.Item>
                </Form>
                <Paragraph>在仓库根目录安装依赖：</Paragraph>
                <Paragraph copyable={{ text: "uv sync --locked --project integrations/mcp" }}><Text code>uv sync --locked --project integrations/mcp</Text></Paragraph>
                <Paragraph type="secondary">MCP 由客户端启动为本地 stdio 进程；服务地址不是 MCP HTTP 端点。</Paragraph>
              </Card>
              <Card title="客户端配置" extra={<Button type="primary" disabled={!directoryValid || !urlValid} onClick={() => void copy(configuration)}>复制配置</Button>}>
                <Paragraph>适用于使用 mcpServers JSON 的客户端。将此服务项合并到已有配置；其他客户端使用相同命令、参数和环境变量。</Paragraph>
                <pre className="code-block integrations-config" aria-label="MCP 客户端配置">{configuration}</pre>
                <Paragraph type="secondary">默认只读。此页面仅生成配置，不会安装 MCP 或修改客户端设置。</Paragraph>
                <Paragraph>保存并重启客户端连接后，依次调用 <Text code>health</Text>、<Text code>list_projects</Text>、<Text code>list_checks</Text>；项目查询传入 <Text code>project_id={currentProject.id}</Text>。</Paragraph>
              </Card>
            </div>
          )
        },
        {
          key: "skill", label: "Skill 使用", children: (
            <div className="integrations-stack">
              <Card title="pulseguard-operations" extra={<Button href={`data:text/markdown;charset=utf-8,${encodeURIComponent(skillSource)}`} download="SKILL.md">下载 SKILL.md</Button>}>
                <Paragraph>先连接 MCP，再使用 Skill 指导 AI 查询状态、分析失败和执行已获授权的探活。</Paragraph>
                <Paragraph>支持仓库 Skill 的客户端可从 <Text code>.agents/skills/pulseguard-operations/</Text> 发现它。其他客户端请在其支持的 Skill 目录创建 <Text code>pulseguard-operations</Text> 文件夹，将下载文件保存为其中的 <Text code>SKILL.md</Text>，再按客户端要求重新加载。</Paragraph>
                <Paragraph type="secondary">Skill 提供操作方法，不会授予执行权限；部署、节点升级和平台配置迁移不在此 Skill 范围内。</Paragraph>
              </Card>
              <Card title="试着这样提问">
                <div className="integrations-stack">{prompts.map((prompt) => <Paragraph key={prompt} copyable={{ text: prompt }}>{prompt}</Paragraph>)}</div>
              </Card>
              <Collapse items={[{ key: "source", label: "查看完整 Skill", children: <pre className="code-block">{skillSource}</pre> }]} />
            </div>
          )
        },
        {
          key: "help", label: "工具与排障", children: (
            <div className="integrations-stack">
              <Card title="按目标选择工具">
                <Space orientation="vertical" size="middle">
                  <Text>检查连接：<Text code>health</Text>。服务可达不代表监控目标健康。</Text>
                  <Text>查询任务：<Text code>list_projects → list_checks → get_check</Text>。</Text>
                  <Text>项目运营：<Text code>get_project_overview</Text> 查看当前状态，<Text code>get_run_statistics</Text> 统计执行次数，<Text code>get_monitoring_trends</Text> 查看健康趋势；<Text code>list_incidents</Text> 和 <Text code>list_audit_events</Text> 追踪异常与变更。</Text>
                  <Text>定位失败：<Text code>list_runs → get_run → get_failure_summary</Text>；按需使用 <Text code>compare_success</Text> 对比成功基线、<Text code>get_screenshot</Text> 获取 PNG 截图。</Text>
                  <Text>执行任务：<Text code>run_check</Text>、<Text code>confirm_recovery</Text>；变更启停：<Text code>set_check_enabled</Text>。两类权限分别启用，使用查询确认的项目和任务 ID。</Text>
                  <Text>批量执行：<Text code>submit_execution_batch → get_execution_batch</Text>。相同幂等键与任务集合只接收一次；取消只影响未派发项。</Text>
                  <Text>创建、编辑或恢复任务：<Text code>get_check_edit_schema / get_check_edit_context → preview_check_change → apply_check_change</Text>；用 <Text code>list_check_versions</Text> 选择历史版本。</Text>
                  <Text>平台排障：<Text code>get_platform_status</Text>、<Text code>list_runners</Text>，仅在启用实例级只读权限后出现。</Text>
                </Space>
              </Card>
              <Collapse items={[
                { key: "execution", label: "如何启用执行？", children: <Paragraph>由连接管理者将配置中的 <Text code>PULSEGUARD_MCP_ALLOW_EXECUTION</Text> 改为 <Text code>true</Text> 并重启连接。执行当前保存的任务可能发送业务请求、更新健康状态并触发告警，没有独立的无告警验证模式。明确请求执行后，AI 才应调用；返回 <Text code>passed=true</Text> 才代表本次探活通过。</Paragraph> },
                { key: "permissions", label: "如何启用任务配置或平台诊断？", children: <Paragraph>任务启停、创建、编辑与版本恢复由 <Text code>PULSEGUARD_MCP_ALLOW_CONFIGURATION</Text> 控制；实例级诊断由 <Text code>PULSEGUARD_MCP_ALLOW_PLATFORM_READ</Text> 控制，可能涉及其他项目共享的节点与队列。两者均默认关闭，需管理者修改连接配置并重启。开启执行权限不会自动授予配置权限。</Paragraph> },
                { key: "workflow", label: "批次或配置提交断线后怎么办？", children: <Paragraph>批次通过原幂等键查询回执，只能用原键和相同任务集合重试，不能换新键重复执行。配置通过预览 ID 查询，只能重试原 ID 和摘要。配置预览30分钟有效，发生并发修改时会拒绝覆盖；新任务默认禁用。未知结果不等于失败或已取消。</Paragraph> },
                { key: "history", label: "执行次数与趋势为什么可能不同？", children: <Paragraph>执行统计包含跳过、准备失败等记录，并明确归档来源和历史缺口；健康趋势只计算健康观测。list_runs 的 total 仅代表仍保留的记录。详情清理不会删除轻量记录，但旧版已删除的执行明细无法补回。</Paragraph> },
                { key: "connection", label: "客户端找不到工具或无法连接", children: <Paragraph>检查 uv 是否在客户端 PATH 中、仓库路径是否属于本机、依赖是否安装，然后重启 MCP 连接。127.0.0.1 指向 AI 客户端所在电脑；远程服务请填写实际可达地址。检查代理配置，必要时用 NO_PROXY 排除本地地址。页面顶部的服务连接状态不代表 MCP 已连接。</Paragraph> },
                { key: "scope", label: "项目不可访问或任务不在列表中", children: <Paragraph>通过 list_projects 确认 ID，并检查 PULSEGUARD_MCP_PROJECT_IDS。此处配置限定当前项目；切换左下角项目后可重新生成。已复制到客户端的配置不会自动更新，需要修改并重启连接。列表有分页，不要把当前页当作全部结果。</Paragraph> },
                { key: "timeout", label: "执行超时或结果未知", children: <Paragraph>不要立即重试。服务端可能仍在执行，客户端取消等待也不代表任务已取消。通过同一项目和任务的 list_runs 对照时间与状态核对；并发运行时不能仅凭最新记录判断关联。客户端等待超时也需覆盖预期执行时间。</Paragraph> },
                { key: "auth", label: "远程地址和 Token 如何配置？", children: <Paragraph>远程服务应使用已有可信网络、隧道或鉴权网关。可通过 PULSEGUARD_MCP_API_TOKEN 向网关发送 Bearer Token；它不是只读快照或 Worker Token，也不会自动为普通管理 API 增加鉴权。远程携带 Token 时使用 HTTPS，不要将凭据放入 URL 或提交到仓库。</Paragraph> }
              ]} />
            </div>
          )
        }
      ]} />
    </div>
  );
}
