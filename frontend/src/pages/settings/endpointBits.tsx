import { Button, Card, Space, Table, Tooltip } from "antd";
import { Clipboard } from "lucide-react";

export function ReadOnlyApiDocs({ onCopyPath }: { onCopyPath: (path: string) => void }) {
  const endpoints = [
    {
      key: "snapshot",
      paths: ["/api/read-only/snapshot"],
      purpose: "读取任务状态与最近运行",
      auth: "Bearer 只读令牌"
    },
    {
      key: "metrics",
      paths: ["/api/metrics", "/api/metrics.json"],
      purpose: "Prometheus 或 JSON 指标采集",
      auth: "无需令牌"
    }
  ];
  return (
    <Card type="inner" title={<Space size={8}><Clipboard size={16} /><span>开放接口</span></Space>}>
      <Table
        className="settings-endpoint-table"
        rowKey="key"
        size="small"
        pagination={false}
        dataSource={endpoints}
        columns={[
          {
            title: "接口",
            dataIndex: "paths",
            width: 360,
            render: (paths: string[]) => (
              <Space orientation="vertical" size={2}>
                {paths.map((path) => <EndpointTitle key={path} path={path} onCopy={onCopyPath} />)}
              </Space>
            )
          },
          { title: "用途", dataIndex: "purpose" },
          { title: "认证", dataIndex: "auth", width: 180 }
        ]}
      />
    </Card>
  );
}

export function EndpointTitle({ path, onCopy }: { path: string; onCopy: (path: string) => void }) {
  return (
    <span className="endpoint-title">
      <span className="endpoint-title-main">
        <span>GET</span>
        <code>{path}</code>
      </span>
      <Tooltip title="复制 Path">
        <Button type="text" size="small" icon={<Clipboard size={14} />} onClick={() => onCopy(path)} aria-label={`复制 ${path}`} />
      </Tooltip>
    </span>
  );
}
