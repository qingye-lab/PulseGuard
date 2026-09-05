import { Alert, Button, Modal, Space, Tabs } from "antd";
import { useState } from "react";
import type { UiInspectionDiagnostics } from "../types";
import { StructuredViewer } from "./StructuredViewer";

const STAGES: Record<string, string> = {
  prepare: "准备任务", compile: "校验脚本", queue: "等待执行", browser: "启动浏览器",
  setup: "前置脚本", resolve_url: "解析 URL", navigate: "加载页面", scan: "扫描元素",
  selectors: "检测规则", assertions: "执行校验", cleanup: "释放资源"
};

export function UiInspectionFeedback({ error, diagnostics, onRetry }: {
  error?: string;
  diagnostics?: UiInspectionDiagnostics;
  onRetry?: () => void;
}) {
  const [open, setOpen] = useState(false);
  if (!error && !diagnostics) return null;
  const detailButton = diagnostics && <Button onClick={() => setOpen(true)}>执行详情</Button>;
  return <>
    {error ? <Alert type="error" message={diagnostics ? `${STAGES[diagnostics.stage] || diagnostics.stage}失败` : "执行失败"}
      description={error} action={<Space wrap>{detailButton}{onRetry && <Button onClick={onRetry}>重试</Button>}</Space>} /> : detailButton}
    <Modal title="执行详情" open={open} onCancel={() => setOpen(false)} footer={null} width={860} destroyOnHidden>
      {diagnostics && <Tabs items={[
        { key: "http", label: "接口请求", children: <StructuredViewer title="最近请求与响应（最多 10 条）" value={diagnostics.http_requests} defaultMode="json" /> },
        { key: "target", label: "目标页面", children: <StructuredViewer title="本次请求" value={diagnostics.request} defaultMode="json" /> },
        { key: "variables", label: "变量", children: <>
          <StructuredViewer title="URL 变量状态" value={Object.fromEntries(Object.entries(diagnostics.url_variables || {}).map(([key, status]) => [key, status === "available" ? "已就绪" : "未生成或未配置"]))} defaultMode="json" />
          <StructuredViewer title="本次生成的临时变量" value={diagnostics.variables} defaultMode="json" />
        </> },
        { key: "logs", label: "日志", children: <StructuredViewer title="本次执行" value={[diagnostics.message, diagnostics.logs, diagnostics.cleanup_error && `资源释放失败：${diagnostics.cleanup_error}`].filter(Boolean).join("\n")} defaultMode="text" /> }
      ]} />}
    </Modal>
  </>;
}
