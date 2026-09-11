import { Alert, Button, Drawer, Space, Tag, Tooltip, Upload } from "antd";
import type { UploadProps } from "antd";
import { CheckCircle2, Download, FileJson2, RotateCcw, ShieldCheck, UploadCloud } from "lucide-react";
import type { ConfigBundle, ConfigExportFile, ConfigImportPreview, ConfigImportSummary } from "../../types";
import { SettingsPanel } from "./panelPrimitives";
import { CONFIG_SUMMARY_ITEMS } from "./settingsTypes";

export function ConfigTransferPanel({
  embedded = false,
  scope,
  exporting,
  uploadProps,
  drawerOpen,
  fileName,
  hasImportBundle,
  preview,
  previewing,
  importing,
  applyDisabledReason,
  importDisabledReason,
  hasUnsavedChanges,
  onExport,
  onOpenImport,
  onCloseImport,
  onResetImport,
  onPreviewImport,
  onRequestApply
}: {
  embedded?: boolean;
  scope: "project" | "system";
  exporting: boolean;
  uploadProps: UploadProps;
  drawerOpen: boolean;
  fileName: string;
  hasImportBundle: boolean;
  preview: ConfigImportPreview | null;
  previewing: boolean;
  importing: boolean;
  applyDisabledReason: string;
  importDisabledReason: string;
  hasUnsavedChanges: boolean;
  onExport: () => void;
  onOpenImport: () => void;
  onCloseImport: () => void;
  onResetImport: () => void;
  onPreviewImport: () => void;
  onRequestApply: () => void;
}) {
  const canResetImport = hasImportBundle || Boolean(preview);

  const content = (
    <>
      <div className="config-transfer-grid">
        <section className="config-transfer-block">
          <div className="config-transfer-heading">
            <strong>导出配置</strong>
            <Tag color="blue">默认脱敏</Tag>
          </div>
          <span className="config-transfer-meta">
            {scope === "project" ? "仅当前项目任务与项目告警，不包含全局设置" : "仅全局设置、Runner 与成员，不包含项目数据"}
          </span>
          <Button icon={<Download size={16} />} loading={exporting} onClick={onExport}>
            导出 JSON
          </Button>
        </section>

        <section className="config-transfer-block">
          <div className="config-transfer-heading">
            <strong>导入配置</strong>
            <Tag color={preview ? (configImportPreviewAllowsApply(preview) ? "success" : "error") : "default"}>
              {preview ? (configImportPreviewAllowsApply(preview) ? "预检通过" : "预检未通过") : "未预检"}
            </Tag>
          </div>
          <span className="config-transfer-meta">选择 JSON 后先预检，预检通过后应用导入</span>
          <Tooltip title={importDisabledReason}>
            <span className="tooltip-button-wrapper">
              <Button icon={<UploadCloud size={16} />} onClick={onOpenImport} disabled={Boolean(importDisabledReason)}>
                导入 JSON
              </Button>
            </span>
          </Tooltip>
        </section>
      </div>

      <Drawer
        title="导入配置"
        open={drawerOpen}
        size={720}
        destroyOnHidden={false}
        closable={!importing}
        mask={{ closable: !importing }}
        onClose={onCloseImport}
        extra={
          <Button icon={<RotateCcw size={15} />} disabled={!canResetImport || importing} onClick={onResetImport}>
            清空
          </Button>
        }
        footer={
          <Space className="drawer-footer-actions" wrap>
            <Button disabled={importing} onClick={onCloseImport}>
              关闭
            </Button>
            <Tooltip title={applyDisabledReason || "应用预检通过的配置"}>
              <span className="tooltip-button-wrapper">
                <Button
                  type="primary"
                  danger
                  icon={<CheckCircle2 size={16} />}
                  disabled={Boolean(applyDisabledReason)}
                  loading={importing}
                  onClick={onRequestApply}
                >
                  应用导入
                </Button>
              </span>
            </Tooltip>
          </Space>
        }
      >
        <div className="config-import-flow">
          {hasUnsavedChanges && (
            <Alert type="warning" title="当前存在未保存设置草稿，应用导入前需要保存或撤销。" showIcon />
          )}

          <Upload.Dragger {...uploadProps} className="config-upload">
            <p className="config-upload-icon">
              <UploadCloud size={28} />
            </p>
            <p className="ant-upload-text">选择配置 JSON</p>
            <p className="ant-upload-hint">{fileName ? `已选择：${fileName}` : "预检前不会写入配置"}</p>
          </Upload.Dragger>

          <div className="config-import-actions">
            {hasImportBundle && <Tag color="processing">{fileName || "已载入配置"}</Tag>}
            <Button icon={<ShieldCheck size={16} />} loading={previewing} disabled={!hasImportBundle || importing} onClick={onPreviewImport}>
              预检导入
            </Button>
          </div>

          {preview && <ConfigImportPreviewResult preview={preview} />}
        </div>
      </Drawer>
    </>
  );

  if (embedded) return <div className="config-transfer-embedded">{content}</div>;
  return (
    <SettingsPanel title={scope === "project" ? "项目包导入导出" : "系统配置包导入导出"} icon={<FileJson2 size={18} />} className="settings-panel-wide">
      {content}
    </SettingsPanel>
  );
}

export function ConfigImportPreviewResult({ preview }: { preview: ConfigImportPreview }) {
  const errors = configImportIssueMessages(preview, "error");
  const warnings = configImportIssueMessages(preview, "warning");
  const summaryItems = configImportSummaryItems(preview);
  const canApply = configImportPreviewAllowsApply(preview);

  return (
    <section className="config-preview-result">
      <Alert type={canApply ? "success" : "error"} title={canApply ? "预检通过" : "预检未通过"} showIcon />

      {summaryItems.length > 0 && (
        <div className="config-preview-summary">
          {summaryItems.map((item) => (
            <div key={item.label} className="config-preview-summary-item">
              <small>{item.label}</small>
              <strong>{item.value}</strong>
            </div>
          ))}
        </div>
      )}

      {errors.length > 0 && <ConfigImportIssueList title="错误" tone="error" messages={errors} />}
      {warnings.length > 0 && <ConfigImportIssueList title="警告" tone="warning" messages={warnings} />}
    </section>
  );
}

export function ConfigImportIssueList({ title, tone, messages }: { title: string; tone: "error" | "warning"; messages: string[] }) {
  return (
    <section className={`config-import-issues config-import-issues-${tone}`}>
      <strong>{title}</strong>
      <ul>
        {messages.map((item, index) => (
          <li key={`${tone}-${index}`}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

export function triggerConfigDownload(file: ConfigExportFile) {
  const url = URL.createObjectURL(file.blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = safeDownloadFilename(file.filename);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function safeDownloadFilename(filename: string): string {
  const safe = filename.trim().replace(/[\\/:*?"<>|]+/g, "-");
  return safe || "pulseguard-config.json";
}

export function isConfigBundle(value: unknown): value is ConfigBundle {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

export function configImportApplyDisabledReason(
  bundle: ConfigBundle | null,
  preview: ConfigImportPreview | null,
  hasUnsavedChanges: boolean
): string {
  if (!bundle) return "先选择配置 JSON";
  if (!preview) return "先完成导入预检";
  if (!configImportPreviewAllowsApply(preview)) return "预检未通过，不能应用导入";
  if (hasUnsavedChanges) return "先保存或撤销当前设置草稿";
  return "";
}

export function configImportPreviewAllowsApply(preview: ConfigImportPreview): boolean {
  return preview.valid !== false && preview.ok !== false && configImportIssueMessages(preview, "error").length === 0;
}

export function configImportIssueMessages(preview: ConfigImportPreview, severity: "error" | "warning"): string[] {
  const direct = issueMessagesFromUnknown(severity === "error" ? preview.errors : preview.warnings);
  const fromIssues = Array.isArray(preview.issues)
    ? preview.issues
        .filter((item) => configIssueMatchesSeverity(item, severity))
        .map(configIssueMessage)
        .filter(Boolean)
    : [];
  return Array.from(new Set([...direct, ...fromIssues])).slice(0, 20);
}

function configIssueMatchesSeverity(issue: unknown, severity: "error" | "warning"): boolean {
  if (typeof issue === "string") return severity === "error";
  if (!issue || typeof issue !== "object") return severity === "error";
  const issueSeverity = "severity" in issue && typeof issue.severity === "string" ? issue.severity : "";
  if (severity === "warning") return issueSeverity === "warning";
  return !issueSeverity || issueSeverity === "error";
}

function issueMessagesFromUnknown(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(configIssueMessage).filter(Boolean);
}

function configIssueMessage(issue: unknown): string {
  if (typeof issue === "string") return issue.trim();
  if (!issue || typeof issue !== "object" || !("message" in issue)) return "";
  return typeof issue.message === "string" ? issue.message.trim() : "";
}

function configImportSummaryItems(preview: ConfigImportPreview): Array<{ label: string; value: string }> {
  const summary = configSummarySource(preview);
  if (!summary) return [];
  return CONFIG_SUMMARY_ITEMS.map((item) => {
    const value = summary[item.key];
    if (typeof value !== "number" && typeof value !== "string") return null;
    return { label: item.label, value: String(value) };
  }).filter((item): item is { label: string; value: string } => Boolean(item));
}

function configSummarySource(preview: ConfigImportPreview): ConfigImportSummary | null {
  const summary = preview.summary && typeof preview.summary === "object" ? preview.summary : {};
  const counts = preview.counts && typeof preview.counts === "object" ? preview.counts : {};
  const merged = { ...summary, ...counts };
  if (Object.keys(merged).length > 0) return merged;
  return null;
}
