import { Alert, App, Button, Card, Descriptions, Empty, Form, Input, Modal, Popconfirm, Segmented, Select, Skeleton, Space, Switch, Table, Tag, Tooltip, Upload } from "antd";
import type { UploadProps } from "antd";
import { BellRing, Clipboard, Database, FolderKanban, Info, KeyRound, Monitor, Plus, RotateCcw, Send, ShieldCheck, Trash2, Zap } from "lucide-react";

import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../../api";

import type {
  AlertTagPolicy,
  AlertPreview,
  ConfigBundle,
  ConfigImportPreview,
  DatabaseBackup,
  EnvironmentVariable,
  NotificationChannel,
  SettingsValues,
  WebhookType
} from "../../types";
import { copyTextToClipboard, dirtyTagColor, enabledTagColor, formatDate } from "../../utils";
import { useProject } from "../../ProjectContext";
import { SettingsTab, SETTINGS_TAB_LABELS, SETTINGS_TAB_ORDER, BROWSER_TYPES } from "./settingsTypes";
import { SettingsPanel, SaveSettingsButton, NumberItem } from "./panelPrimitives";
import { RunnerNodePanel } from "./RunnerNodePanel";
import { ConfigTransferPanel, triggerConfigDownload, isConfigBundle, configImportApplyDisabledReason, configImportPreviewAllowsApply } from "./ConfigTransferPanel";
import { NotificationChannelEditor } from "./NotificationChannelEditor";
import { createChannel, channelDisplayName, channelTestDisabledReason, isReadyForSend, CHANNEL_OPTIONS } from "./channelMeta";
import { ProjectVariablesPanel, EnvironmentVariableEditor, AlertTagPolicyEditor, AlertPreviewTool, createAlertTagPolicy, createEnvironmentVariable, uniqueIds, toPayloadAlertTagPolicy, toPayloadChannel, toPayloadEnvironmentVariable, firstBlockingError, firstAlertTagPolicyError, firstVariableBlockingError } from "./alertEditors";
import { cloneSettings, settingsChanged, settingsTabChanged, normalizeBrowserTypes, normalizeBrowserPoolSizes, formatBytes } from "./configHelpers";
import { ReadOnlyApiDocs } from "./endpointBits";

/**
 * "项目告警" is served by its own route (`/projects/alerts` renders SettingsPage
 * with a tab override) because it is project-scoped while the other four are
 * system-scoped. The in-page tabs therefore point at the route each tab really
 * lives on rather than all pretending to be `/settings/*`.
 */
function settingsTabPath(tab: SettingsTab): string {
  return tab === "alerts" ? "/projects/alerts" : `/settings/${tab}`;
}

const SETTINGS_TAB_KEYS = new Set<SettingsTab>(Object.keys(SETTINGS_TAB_LABELS) as SettingsTab[]);
const SETTINGS_TAB_ALIASES: Record<string, SettingsTab> = {
  runtime: "execution",
  nodes: "execution",
  browser: "execution",
  retention: "system",
  access: "system",
  maintenance: "system",
  config: "system"
};

function normalizeSettingsTab(value?: string): SettingsTab {
  if (!value) return "execution";
  if (SETTINGS_TAB_KEYS.has(value as SettingsTab)) return value as SettingsTab;
  return SETTINGS_TAB_ALIASES[value] || "execution";
}
type ProjectAlertSection = "channels" | "policies" | "package";
type SystemSettingsSection = "storage" | "access" | "maintenance" | "package";

export function SettingsPage({ tabOverride }: { tabOverride?: SettingsTab } = {}) {
  const { message } = App.useApp();
  const { currentProject, projects } = useProject();
  const navigate = useNavigate();
  const { tab } = useParams<{ tab?: string }>();
  const activeTab = tabOverride || normalizeSettingsTab(tab);
  const [settings, setSettings] = useState<SettingsValues | null>(null);
  const [savedSettings, setSavedSettings] = useState<SettingsValues | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testingChannelId, setTestingChannelId] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [alertPreview, setAlertPreview] = useState<AlertPreview | null>(null);
  const [editingChannelIds, setEditingChannelIds] = useState<Set<string>>(() => new Set());
  const [newChannelType, setNewChannelType] = useState<WebhookType>("feishu");
  const [exportingConfig, setExportingConfig] = useState(false);
  const [importDrawerOpen, setImportDrawerOpen] = useState(false);
  const [importBundle, setImportBundle] = useState<ConfigBundle | null>(null);
  const [importFileName, setImportFileName] = useState("");
  const [importPreview, setImportPreview] = useState<ConfigImportPreview | null>(null);
  const [importPreviewing, setImportPreviewing] = useState(false);
  const [importingConfig, setImportingConfig] = useState(false);
  const [importConfirmOpen, setImportConfirmOpen] = useState(false);
  const [savingReadOnlyToken, setSavingReadOnlyToken] = useState(false);
  const [readOnlyTokenCreateOpen, setReadOnlyTokenCreateOpen] = useState(false);
  const [readOnlyTokenNameDraft, setReadOnlyTokenNameDraft] = useState("");
  const [readOnlyTokenProjectIds, setReadOnlyTokenProjectIds] = useState<number[]>([currentProject.id]);
  const [createdReadOnlyToken, setCreatedReadOnlyToken] = useState<{ name: string; token: string } | null>(null);
  const [databaseBackups, setDatabaseBackups] = useState<DatabaseBackup[]>([]);
  const [backupBusy, setBackupBusy] = useState(false);
  const [projectAlertSection, setProjectAlertSection] = useState<ProjectAlertSection>("channels");
  const [systemSettingsSection, setSystemSettingsSection] = useState<SystemSettingsSection>("storage");
  const [variableScope, setVariableScope] = useState<"project" | "global">("project");

  useEffect(() => {
    if (tabOverride) return;
    if (tab !== activeTab) {
      navigate(`/settings/${activeTab}`, { replace: true });
    }
  }, [activeTab, navigate, tab, tabOverride]);

  useEffect(() => {
    api
      .settings()
      .then((values) => {
        setSettings(cloneSettings(values));
        setSavedSettings(cloneSettings(values));
      })
      .catch((err: Error) => message.error(err.message))
      .finally(() => setLoading(false));
  }, [message]);

  useEffect(() => {
    if (activeTab !== "system") return;
    void loadDatabaseBackups();
  }, [activeTab]);

  async function loadDatabaseBackups() {
    try {
      setDatabaseBackups(await api.databaseBackups());
    } catch (err) {
      message.error((err as Error).message);
    }
  }

  async function createDatabaseBackup() {
    setBackupBusy(true);
    try {
      await api.createDatabaseBackup();
      await loadDatabaseBackups();
      message.success("数据库备份已创建");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBackupBusy(false);
    }
  }

  async function restoreDatabaseBackup(filename: string) {
    setBackupBusy(true);
    try {
      await api.restoreDatabaseBackup(filename);
      await Promise.all([loadDatabaseBackups(), api.settings().then((values) => {
        setSettings(cloneSettings(values));
        setSavedSettings(cloneSettings(values));
      })]);
      message.success("数据库备份已恢复，并已自动创建恢复前安全备份");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBackupBusy(false);
    }
  }

  function update<K extends keyof SettingsValues>(key: K, value: SettingsValues[K]) {
    setAlertPreview(null);
    setSettings((current) => (current ? { ...current, [key]: value } : current));
  }

  function updateChannel(channelId: string, patch: Partial<NotificationChannel>) {
    setAlertPreview(null);
    setSettings((current) => {
      if (!current) return current;
      const key = activeTab === "system-alerts" ? "system_notification_channels" : "notification_channels";
      return { ...current, [key]: current[key].map((channel) => channel.id === channelId ? { ...channel, ...patch } : channel) };
    });
  }

  function addChannel() {
    const channel = createChannel(newChannelType);
    setAlertPreview(null);
    setSettings((current) => {
      if (!current) return current;
      const key = activeTab === "system-alerts" ? "system_notification_channels" : "notification_channels";
      return { ...current, [key]: [...current[key], channel] };
    });
    setChannelEditing(channel.id, true);
  }

  function removeChannel(channelId: string) {
    setAlertPreview(null);
    setSettings((current) => {
      if (!current) return current;
      if (activeTab === "system-alerts") {
        return {
          ...current,
          system_notification_channels: current.system_notification_channels.filter((channel) => channel.id !== channelId),
          system_notification_channel_ids: (current.system_notification_channel_ids || []).filter((id) => id !== channelId)
        };
      }
      return {
        ...current,
        notification_channels: current.notification_channels.filter((channel) => channel.id !== channelId),
        execution_notification_channel_ids: (current.execution_notification_channel_ids || []).filter((id) => id !== channelId),
        alert_tag_policies: current.alert_tag_policies.map((policy) => ({
          ...policy,
          notification_channel_ids: (policy.notification_channel_ids || []).filter((id) => id !== channelId)
        }))
      };
    });
    setChannelEditing(channelId, false);
  }

  function setChannelEditing(channelId: string, editing: boolean) {
    setEditingChannelIds((current) => {
      const next = new Set(current);
      if (editing) {
        next.add(channelId);
      } else {
        next.delete(channelId);
      }
      return next;
    });
  }

  function updateAlertTagPolicy(policyId: string, patch: Partial<AlertTagPolicy>) {
    setAlertPreview(null);
    setSettings((current) => {
      if (!current) return current;
      return {
        ...current,
        alert_tag_policies: current.alert_tag_policies.map((policy) => (policy.id === policyId ? { ...policy, ...patch } : policy))
      };
    });
  }

  function addAlertTagPolicy() {
    setAlertPreview(null);
    setSettings((current) => {
      if (!current) return current;
      return {
        ...current,
        alert_tag_policies: [...current.alert_tag_policies, createAlertTagPolicy(current)]
      };
    });
  }

  function removeAlertTagPolicy(policyId: string) {
    setAlertPreview(null);
    setSettings((current) => {
      if (!current) return current;
      return {
        ...current,
        alert_tag_policies: current.alert_tag_policies.filter((policy) => policy.id !== policyId)
      };
    });
  }

  function updateEnvironmentVariable(variableId: string, patch: Partial<EnvironmentVariable>) {
    setSettings((current) => {
      if (!current) return current;
      return {
        ...current,
        environment_variables: current.environment_variables.map((variable) =>
          variable.id === variableId ? { ...variable, ...patch } : variable
        )
      };
    });
  }

  function addEnvironmentVariable() {
    setSettings((current) => {
      if (!current) return current;
      return {
        ...current,
        environment_variables: [...current.environment_variables, createEnvironmentVariable()]
      };
    });
  }

  function removeEnvironmentVariable(variableId: string) {
    setSettings((current) => {
      if (!current) return current;
      return {
        ...current,
        environment_variables: current.environment_variables.filter((variable) => variable.id !== variableId)
      };
    });
  }

  function buildPayload(scope: SettingsTab = activeTab): Partial<SettingsValues> {
    if (!settings) return {};
    if (scope === "alerts") {
      return {
        alerts_enabled: settings.alerts_enabled,
        alert_cooldown_minutes: settings.alert_cooldown_minutes,
        recovery_notification: settings.recovery_notification,
        execution_notification_channel_ids: uniqueIds(settings.execution_notification_channel_ids || []),
        alert_tag_policies: settings.alert_tag_policies.map(toPayloadAlertTagPolicy),
        notification_channels: settings.notification_channels.map(toPayloadChannel)
      };
    }
    if (scope === "system-alerts") {
      return {
        system_alerts_enabled: settings.system_alerts_enabled,
        alert_detail_base_url: settings.alert_detail_base_url.trim(),
        alert_delivery_attempts: settings.alert_delivery_attempts,
        system_notification_channel_ids: uniqueIds(settings.system_notification_channel_ids || []),
        system_notification_channels: settings.system_notification_channels.map(toPayloadChannel)
      };
    }
    if (scope === "execution") {
      return {
        default_interval_seconds: settings.default_interval_seconds,
        default_ui_timeout_ms: settings.default_ui_timeout_ms,
        default_api_timeout_ms: settings.default_api_timeout_ms,
        max_concurrency: settings.max_concurrency,
        max_ui_concurrency: settings.max_ui_concurrency,
        max_queue_size: settings.max_queue_size,
        max_task_runtime_seconds: settings.max_task_runtime_seconds,
        api_failure_confirmation_count: settings.api_failure_confirmation_count,
        ui_failure_confirmation_count: settings.ui_failure_confirmation_count,
        recovery_confirmation_count: settings.recovery_confirmation_count,
        api_retry_attempts: settings.api_retry_attempts,
        ui_retry_attempts: settings.ui_retry_attempts,
        stale_after_intervals: settings.stale_after_intervals,
        browser_headless: settings.browser_headless,
        browser_type: settings.enabled_browser_types[0] || "chromium",
        enabled_browser_types: normalizeBrowserTypes(settings.enabled_browser_types),
        prewarmed_browser_types: normalizeBrowserTypes(settings.prewarmed_browser_types, true).filter((browserType) =>
          normalizeBrowserTypes(settings.enabled_browser_types).includes(browserType)
        ),
        browser_pool_sizes: normalizeBrowserPoolSizes(settings.browser_pool_sizes),
        browser_recycle_after_runs: settings.browser_recycle_after_runs,
        browser_proxy: settings.browser_proxy,
        browser_viewport: settings.browser_viewport
      };
    }
    if (scope === "variables") {
      return {
        environment_variables: settings.environment_variables.map(toPayloadEnvironmentVariable)
      };
    }
    if (scope === "system") {
      return {
        run_retention_days: settings.run_retention_days,
        screenshot_retention_days: settings.screenshot_retention_days,
        trace_retention_days: settings.trace_retention_days,
        response_retention_days: settings.response_retention_days,
        similar_failure_retention_count: settings.similar_failure_retention_count,
        trace_artifacts_enabled: settings.trace_artifacts_enabled,
        success_response_artifacts_enabled: settings.success_response_artifacts_enabled,
        database_backup_retention: settings.database_backup_retention,
        maintenance_enabled: Boolean(settings.maintenance_enabled),
        maintenance_title: (settings.maintenance_title || "").trim(),
        maintenance_message: (settings.maintenance_message || "").trim(),
        maintenance_starts_at: (settings.maintenance_starts_at || "").trim(),
        maintenance_ends_at: (settings.maintenance_ends_at || "").trim()
      };
    }
    return {};
  }

  async function save(scope: SettingsTab = activeTab) {
    if (!settings) return;
    setSaving(true);
    try {
      const values = await api.updateSettings(buildPayload(scope));
      setSettings(cloneSettings(values));
      setSavedSettings(cloneSettings(values));
      setAlertPreview(null);
      if (scope === "system") {
        window.dispatchEvent(new Event("pulseguard:maintenance-updated"));
      }
      message.success("当前页设置已保存");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  function openReadOnlyTokenCreate() {
    setReadOnlyTokenNameDraft("");
    setReadOnlyTokenProjectIds([currentProject.id]);
    setReadOnlyTokenCreateOpen(true);
  }

  async function createReadOnlyToken() {
    const tokenName = readOnlyTokenNameDraft.trim() || "未命名令牌";
    setSavingReadOnlyToken(true);
    try {
      const created = await api.createReadOnlyToken(tokenName, readOnlyTokenProjectIds);
      const values = await api.settings();
      setSettings(cloneSettings(values));
      setSavedSettings(cloneSettings(values));
      setReadOnlyTokenCreateOpen(false);
      setReadOnlyTokenNameDraft("");
      setCreatedReadOnlyToken({ name: created.name, token: created.token });
      message.success("开放接口令牌已新建");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSavingReadOnlyToken(false);
    }
  }

  async function deleteReadOnlyToken(tokenId: string) {
    setSavingReadOnlyToken(true);
    try {
      const values = await api.deleteReadOnlyToken(tokenId);
      setSettings(cloneSettings(values));
      setSavedSettings(cloneSettings(values));
      setCreatedReadOnlyToken(null);
      message.success("开放接口令牌已删除");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSavingReadOnlyToken(false);
    }
  }

  async function copyValue(value: string, successText = "已复制") {
    try {
      await copyTextToClipboard(value);
      message.success(successText);
    } catch {
      message.error("复制失败，请手动选中内容复制");
    }
  }

  function buildChannelTestPayload(channel: NotificationChannel): Partial<SettingsValues> {
    const common = {
      alert_delivery_attempts: settings?.alert_delivery_attempts ?? 1,
    };
    if (activeTab === "system-alerts") {
      return {
        ...common,
        system_notification_channels: [toPayloadChannel(channel)],
        system_notification_channel_ids: [channel.id]
      };
    }
    return {
      ...common,
      notification_channels: [toPayloadChannel(channel)]
    };
  }

  async function testAlert(channel: NotificationChannel) {
    if (!settings) return;
    setTestingChannelId(channel.id);
    try {
      const result = activeTab === "system-alerts"
        ? await api.testSystemAlert(buildChannelTestPayload(channel))
        : await api.testAlert(buildChannelTestPayload(channel));
      message.success(`${channelDisplayName(channel)}：${result.message}`);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setTestingChannelId(null);
    }
  }

  async function previewAlert() {
    if (!settings) return;
    setPreviewing(true);
    try {
      setAlertPreview(
        activeTab === "system-alerts"
          ? await api.systemAlertPreview(buildPayload("system-alerts"))
          : await api.alertPreview(buildPayload("alerts"))
      );
    } catch (err) {
      setAlertPreview(null);
      message.error((err as Error).message);
    } finally {
      setPreviewing(false);
    }
  }

  async function exportConfig() {
    setExportingConfig(true);
    try {
      const file = activeTab === "alerts" ? await api.exportProjectConfig() : await api.exportConfig();
      triggerConfigDownload(file);
      message.success(activeTab === "alerts" ? "已导出当前项目包" : "已导出脱敏系统配置包");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setExportingConfig(false);
    }
  }

  async function loadImportFile(file: File) {
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as unknown;
      if (!isConfigBundle(parsed)) {
        message.error("配置文件必须是 JSON 对象");
        return;
      }
      setImportBundle(parsed);
      setImportFileName(file.name);
      setImportPreview(null);
      setImportConfirmOpen(false);
      message.success("已载入配置文件");
    } catch {
      setImportBundle(null);
      setImportFileName("");
      setImportPreview(null);
      message.error("配置文件不是有效 JSON");
    }
  }

  async function previewConfigImport() {
    if (!importBundle) return;
    setImportPreviewing(true);
    try {
      const preview = activeTab === "alerts"
        ? await api.previewProjectConfigImport(importBundle)
        : await api.previewConfigImport(importBundle);
      setImportPreview(preview);
      if (configImportPreviewAllowsApply(preview)) {
        message.success("预检通过");
      } else {
        message.error("预检未通过");
      }
    } catch (err) {
      setImportPreview(null);
      message.error((err as Error).message);
    } finally {
      setImportPreviewing(false);
    }
  }

  async function applyConfigImport() {
    if (!importBundle || !importPreview || !configImportPreviewAllowsApply(importPreview) || hasUnsavedChanges) return;
    setImportingConfig(true);
    try {
      const result = activeTab === "alerts"
        ? await api.importProjectConfig(importBundle)
        : await api.importConfig(importBundle);
      setImportConfirmOpen(false);
      setImportDrawerOpen(false);
      resetImportDraft();
      try {
        const values = await api.settings();
        setSettings(cloneSettings(values));
        setSavedSettings(cloneSettings(values));
        setAlertPreview(null);
        message.success(result.message || (activeTab === "alerts" ? "项目包已导入" : "系统配置已导入"));
      } catch (refreshErr) {
        message.warning(`配置已导入，但刷新设置失败：${(refreshErr as Error).message}`);
      }
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setImportingConfig(false);
    }
  }

  function resetImportDraft() {
    setImportBundle(null);
    setImportFileName("");
    setImportPreview(null);
    setImportConfirmOpen(false);
  }

  function resetDraft() {
    if (!savedSettings) return;
    setSettings(cloneSettings(savedSettings));
    setAlertPreview(null);
    message.success("已撤销未保存更改");
  }

  if (loading || !settings) {
    return (
      <div className="page-content settings-page">
        <Skeleton active paragraph={{ rows: 10 }} />
      </div>
    );
  }

  const activeChannels = activeTab === "system-alerts" ? settings.system_notification_channels : settings.notification_channels;
  const blockingError = firstBlockingError(activeChannels);
  const alertTagPolicyError = firstAlertTagPolicyError(settings.alert_tag_policies);
  const variableBlockingError = firstVariableBlockingError(settings.environment_variables);
  const hasUnsavedChanges = Boolean(savedSettings && settingsChanged(settings, savedSettings));
  const settingsTabCanSave = currentProject.status === "active" || activeTab !== "alerts";
  const projectReadOnly = activeTab === "alerts" && currentProject.status === "archived";
  const activeTabChanged = Boolean(savedSettings && settingsTabCanSave && settingsTabChanged(settings, savedSettings, activeTab));
  const alertSettingsChanged = Boolean(savedSettings && settingsTabChanged(settings, savedSettings, "alerts"));
  const activeTabBlockingError = activeTab === "alerts" ? blockingError || alertTagPolicyError : activeTab === "system-alerts" ? blockingError : activeTab === "variables" ? variableBlockingError : "";
  const saveDisabledReason = activeTab === "alerts" && currentProject.status === "archived"
    ? "归档项目的项目告警为只读"
    : activeTabBlockingError || (!activeTabChanged ? "当前页没有需要保存的更改" : "");
  const previewDisabledReason = activeChannels.length === 0 ? "先添加通知渠道后再预检" : blockingError;
  const sendableChannels = activeChannels.filter(isReadyForSend);
  const sendableChannelCount = sendableChannels.length;
  const sendableChannelIds = new Set(sendableChannels.map((channel) => channel.id));
  const channelOptions = settings.notification_channels.map((channel) => ({
    label: channelDisplayName(channel),
    value: channel.id,
    disabled: !isReadyForSend(channel)
  }));
  const systemChannelOptions = settings.system_notification_channels.map((channel) => ({
    label: channelDisplayName(channel),
    value: channel.id,
    disabled: !isReadyForSend(channel)
  }));
  const executionSelectedChannelCount = settings.execution_notification_channel_ids.filter((channelId) => sendableChannelIds.has(channelId)).length;
  const readySystemChannelIds = new Set(settings.system_notification_channels.filter(isReadyForSend).map((channel) => channel.id));
  const systemSelectedChannelCount = settings.system_notification_channel_ids.filter((channelId) => readySystemChannelIds.has(channelId)).length;
  const executionChannelCount =
    settings.execution_notification_channel_ids.length > 0 ? executionSelectedChannelCount : sendableChannelCount;
  const enabledAlertTagPolicyCount = settings.alert_tag_policies.filter((policy) => policy.enabled).length;
  const systemChannelCount = systemSelectedChannelCount;
  const importApplyDisabledReason = activeTab === "alerts" && currentProject.status === "archived"
    ? "归档项目不能导入项目包"
    : configImportApplyDisabledReason(importBundle, importPreview, hasUnsavedChanges);
  const readOnlyTokens = settings.read_only_tokens || [];
  const importUploadProps: UploadProps = {
    accept: "application/json,.json",
    maxCount: 1,
    showUploadList: false,
    beforeUpload: (file) => {
      void loadImportFile(file as File);
      return Upload.LIST_IGNORE;
    }
  };

  return (
    <div className="page-content settings-page">
      <nav className="settings-tab-bar" aria-label="设置分类">
        {SETTINGS_TAB_ORDER.map((tabKey) => (
          <Link
            key={tabKey}
            to={settingsTabPath(tabKey)}
            className={`settings-tab ${tabKey === activeTab ? "is-active" : ""}`}
            aria-current={tabKey === activeTab ? "page" : undefined}
          >
            {SETTINGS_TAB_LABELS[tabKey]}
          </Link>
        ))}
      </nav>

      {!(activeTab === "variables" && variableScope === "project") && <Card size="small" className="settings-command-card">
        <div className="settings-command-bar">
          <div className="settings-title-row">
            {activeTab === "alerts" && <Tag icon={<FolderKanban size={14} />} color="blue">{currentProject.name}</Tag>}
            <Tooltip title={hasUnsavedChanges ? "当前页存在未保存内容" : "当前内容与已保存配置一致"}>
              <Tag color={dirtyTagColor(hasUnsavedChanges)}>{hasUnsavedChanges ? "未保存更改" : "已保存"}</Tag>
            </Tooltip>
          </div>
          <Space wrap>
            <Button icon={<RotateCcw size={16} />} onClick={resetDraft} disabled={!hasUnsavedChanges}>
              撤销更改
            </Button>
            {settingsTabCanSave && <SaveSettingsButton loading={saving} disabledReason={saveDisabledReason} onClick={() => save()} />}
          </Space>
        </div>
      </Card>}

      <section className={`settings-layout settings-layout-${activeTab}`}>
        {activeTab === "variables" && <Segmented
          aria-label="变量作用域"
          value={variableScope}
          options={[{ label: "项目变量", value: "project" }, { label: "全局变量", value: "global" }]}
          onChange={(value) => setVariableScope(value as "project" | "global")}
        />}
        {activeTab === "variables" && variableScope === "project" && <ProjectVariablesPanel
          key={currentProject.id} projectName={currentProject.name} readOnly={currentProject.status !== "active"}
        />}
        {activeTab === "alerts" && (
          <>
            <fieldset className="settings-readonly-fieldset" disabled={projectReadOnly}>
              <div className="project-primary-grid project-alert-primary-grid">
                <Card
                  title={<Space size={8}><BellRing size={16} /><span>默认告警规则</span></Space>}
                  extra={
                    <Space size={8}>
                      <Tag color={enabledTagColor(settings.alerts_enabled)}>{settings.alerts_enabled ? "运行中" : "已停用"}</Tag>
                      <Switch aria-label="启用运行告警" checked={settings.alerts_enabled} onChange={(value) => update("alerts_enabled", value)} />
                    </Space>
                  }
                >
                  <Form layout="vertical" className="project-alert-rule-form" autoComplete="off">
                    <Form.Item label="默认通知渠道">
                      <Select
                        mode="multiple"
                        allowClear
                        value={settings.execution_notification_channel_ids}
                        onChange={(value) => update("execution_notification_channel_ids", value)}
                        options={channelOptions}
                        placeholder="全部可发送渠道"
                      />
                    </Form.Item>
                    <NumberItem
                      label="失败冷却"
                      value={settings.alert_cooldown_minutes}
                      min={1}
                      max={1440}
                      suffix="分钟"
                      onChange={(value) => update("alert_cooldown_minutes", value)}
                    />
                    <Form.Item label="恢复通知">
                      <Switch aria-label="恢复通知" checked={settings.recovery_notification} onChange={(value) => update("recovery_notification", value)} />
                    </Form.Item>
                  </Form>
                </Card>

                <Card title="发送链路" extra={<Tag>{alertSettingsChanged ? "草稿" : "已保存"}</Tag>}>
                  <Descriptions
                    className="project-state-descriptions"
                    size="small"
                    column={1}
                    items={[
                      { key: "event", label: "事件", children: settings.recovery_notification ? "失败、恢复" : "失败" },
                      { key: "rule", label: "规则", children: enabledAlertTagPolicyCount > 0 ? `默认 + ${enabledAlertTagPolicyCount} 标签` : "默认" },
                      { key: "delivery", label: "送达", children: settings.alerts_enabled ? (executionChannelCount > 0 ? `${executionChannelCount} 个渠道` : "未配置") : "已暂停" }
                    ]}
                  />
                </Card>
              </div>
            </fieldset>

            <Card
              className="project-alert-config-card"
              title="通知配置"
              activeTabKey={projectAlertSection}
              onTabChange={(key) => setProjectAlertSection(key as ProjectAlertSection)}
              tabList={[
                { key: "channels", tab: `通知渠道 ${settings.notification_channels.length}` },
                { key: "policies", tab: `标签策略 ${settings.alert_tag_policies.length}` },
                { key: "package", tab: "项目配置包" }
              ]}
            >
                {projectAlertSection === "channels" && (
                  <>
                    <fieldset className="settings-readonly-fieldset" disabled={projectReadOnly}>
                      <div className="project-alert-section-toolbar">
                        <Tag color={sendableChannelCount > 0 ? "success" : "default"}>{sendableChannelCount} / {settings.notification_channels.length} 可发送</Tag>
                        <Space size={8}>
                          <AlertPreviewTool
                            compact
                            preview={alertPreview}
                            loading={previewing}
                            disabledReason={previewDisabledReason}
                            usesDraft={alertSettingsChanged}
                            onPreview={previewAlert}
                            onClosePreview={() => setAlertPreview(null)}
                          />
                          <Space.Compact>
                            <Select
                              value={newChannelType}
                              onChange={(value) => setNewChannelType(value)}
                              options={CHANNEL_OPTIONS}
                              className="channel-type-picker"
                            />
                            <Button icon={<Plus size={16} />} onClick={addChannel}>添加渠道</Button>
                          </Space.Compact>
                        </Space>
                      </div>
                      {settings.notification_channels.length === 0 ? (
                        <Empty description="暂无通知渠道" />
                      ) : (
                        <div className="notification-channel-list">
                          {settings.notification_channels.map((channel, index) => (
                            <NotificationChannelEditor
                              key={channel.id}
                              channel={channel}
                              index={index}
                              editing={editingChannelIds.has(channel.id)}
                              testing={testingChannelId === channel.id}
                              testDisabledReason={channelTestDisabledReason(channel)}
                              usesDraft={alertSettingsChanged}
                              onPatch={(patch) => updateChannel(channel.id, patch)}
                              onRemove={() => removeChannel(channel.id)}
                              onEdit={() => setChannelEditing(channel.id, true)}
                              onDone={() => setChannelEditing(channel.id, false)}
                              onTest={() => testAlert(channel)}
                            />
                          ))}
                        </div>
                      )}
                    </fieldset>
                  </>
                )}

                {projectAlertSection === "policies" && (
                  <fieldset className="settings-readonly-fieldset" disabled={projectReadOnly}>
                    <div className="project-alert-section-toolbar">
                      <Tag color={enabledAlertTagPolicyCount > 0 ? "success" : "default"}>{enabledAlertTagPolicyCount} 个已启用</Tag>
                      <Button icon={<Plus size={16} />} onClick={addAlertTagPolicy}>添加标签策略</Button>
                    </div>
                    {settings.alert_tag_policies.length === 0 ? (
                      <Empty description="暂无标签告警策略" />
                    ) : (
                      <div className="notification-channel-list">
                        {settings.alert_tag_policies.map((policy, index) => (
                          <AlertTagPolicyEditor
                            key={policy.id}
                            policy={policy}
                            index={index}
                            channels={settings.notification_channels}
                            policies={settings.alert_tag_policies}
                            onPatch={(patch) => updateAlertTagPolicy(policy.id, patch)}
                            onRemove={() => removeAlertTagPolicy(policy.id)}
                          />
                        ))}
                      </div>
                    )}
                  </fieldset>
                )}

                {projectAlertSection === "package" && (
                  <ConfigTransferPanel
                    embedded
                    scope="project"
                    exporting={exportingConfig}
                    uploadProps={importUploadProps}
                    drawerOpen={importDrawerOpen}
                    fileName={importFileName}
                    hasImportBundle={Boolean(importBundle)}
                    preview={importPreview}
                    previewing={importPreviewing}
                    importing={importingConfig}
                    applyDisabledReason={importApplyDisabledReason}
                    importDisabledReason={projectReadOnly ? "归档项目不能导入项目包" : ""}
                    hasUnsavedChanges={hasUnsavedChanges}
                    onExport={exportConfig}
                    onOpenImport={() => setImportDrawerOpen(true)}
                    onCloseImport={() => setImportDrawerOpen(false)}
                    onResetImport={resetImportDraft}
                    onPreviewImport={previewConfigImport}
                    onRequestApply={() => setImportConfirmOpen(true)}
                  />
                )}
            </Card>

          </>
        )}

        {activeTab === "system-alerts" && (
          <>
            <SettingsPanel
              title="告警规则"
              icon={<ShieldCheck size={18} />}
              accent
              extra={
                <Space size={8}>
                  <Tag color={enabledTagColor(settings.system_alerts_enabled)}>{settings.system_alerts_enabled ? "运行中" : "已停用"}</Tag>
                  <Switch
                    aria-label="启用系统告警"
                    checked={settings.system_alerts_enabled}
                    onChange={(value) => update("system_alerts_enabled", value)}
                  />
                </Space>
              }
            >
              <Form layout="vertical" className="settings-form-grid settings-form-grid-single" autoComplete="off">
                <Form.Item label="系统告警渠道" className="span-2">
                  <Select
                    mode="multiple"
                    allowClear
                    value={settings.system_notification_channel_ids}
                    onChange={(value) => update("system_notification_channel_ids", value)}
                    options={systemChannelOptions}
                    placeholder="选择系统通知渠道"
                  />
                </Form.Item>
                <NumberItem
                  label="发送尝试次数"
                  value={settings.alert_delivery_attempts}
                  min={1}
                  max={5}
                  suffix="次"
                  onChange={(value) => update("alert_delivery_attempts", value)}
                />
                <Form.Item label="告警详情链接前缀">
                  <Input
                    name="alert-detail-base-url"
                    value={settings.alert_detail_base_url}
                    onChange={(event) => update("alert_detail_base_url", event.target.value)}
                    placeholder="例如：http://10.168.78.49:8787"
                    autoComplete="off"
                  />
                </Form.Item>
              </Form>
            </SettingsPanel>

            <SettingsPanel title="发送链路" icon={<Send size={18} />}>
              <Descriptions
                bordered
                size="small"
                column={1}
                items={[
                  { key: "status", label: "状态", children: settings.system_alerts_enabled ? "接收系统事件" : "已暂停" },
                  { key: "channels", label: "送达渠道", children: settings.system_alerts_enabled && systemChannelCount > 0 ? `${systemChannelCount} 个渠道` : "不会推送" },
                  { key: "attempts", label: "失败重试", children: `${settings.alert_delivery_attempts} 次尝试` },
                  { key: "detail", label: "详情链接", children: settings.alert_detail_base_url.trim() || "使用当前访问地址" }
                ]}
              />
            </SettingsPanel>

            <SettingsPanel
              title="系统通知渠道"
              icon={<BellRing size={18} />}
              className="settings-panel-wide"
              extra={
                <Space size={8}>
                  <AlertPreviewTool
                    compact
                    preview={alertPreview}
                    loading={previewing}
                    disabledReason={previewDisabledReason}
                    usesDraft={activeTabChanged}
                    onPreview={previewAlert}
                    onClosePreview={() => setAlertPreview(null)}
                  />
                  <Space.Compact>
                    <Select
                      value={newChannelType}
                      onChange={(value) => setNewChannelType(value)}
                      options={CHANNEL_OPTIONS}
                      className="channel-type-picker"
                    />
                    <Button icon={<Plus size={16} />} onClick={addChannel}>添加渠道</Button>
                  </Space.Compact>
                </Space>
              }
            >
              {settings.system_notification_channels.length === 0 ? (
                <Empty description="暂无系统通知渠道" />
              ) : (
                <div className="notification-channel-list">
                  {settings.system_notification_channels.map((channel, index) => (
                    <NotificationChannelEditor
                      key={channel.id}
                      channel={channel}
                      index={index}
                      editing={editingChannelIds.has(channel.id)}
                      testing={testingChannelId === channel.id}
                      testDisabledReason={channelTestDisabledReason(channel)}
                      usesDraft={activeTabChanged}
                      onPatch={(patch) => updateChannel(channel.id, patch)}
                      onRemove={() => removeChannel(channel.id)}
                      onEdit={() => setChannelEditing(channel.id, true)}
                      onDone={() => setChannelEditing(channel.id, false)}
                      onTest={() => testAlert(channel)}
                    />
                  ))}
                </div>
              )}
            </SettingsPanel>
          </>
        )}

        {activeTab === "execution" && (
        <>
        <SettingsPanel title="调度与容量" icon={<Zap size={18} />}>
          <Form layout="vertical" className="settings-form-grid" autoComplete="off">
            <NumberItem label="默认执行频率" value={settings.default_interval_seconds} min={5} max={86400} suffix="秒" onChange={(value) => update("default_interval_seconds", value)} />
            <NumberItem label="最大并发任务数" value={settings.max_concurrency} min={1} max={20} onChange={(value) => update("max_concurrency", value)} />
            <NumberItem label="最大 UI 并发数" value={settings.max_ui_concurrency} min={1} max={5} onChange={(value) => update("max_ui_concurrency", value)} />
            <NumberItem label="执行队列容量" value={settings.max_queue_size} min={1} max={1000} onChange={(value) => update("max_queue_size", value)} />
            <NumberItem label="默认 UI 超时" value={settings.default_ui_timeout_ms} min={500} max={300000} suffix="ms" onChange={(value) => update("default_ui_timeout_ms", value)} />
            <NumberItem label="默认 API 超时" value={settings.default_api_timeout_ms} min={500} max={300000} suffix="ms" onChange={(value) => update("default_api_timeout_ms", value)} />
            <NumberItem label="单任务最大运行时长" value={settings.max_task_runtime_seconds} min={1} max={600} suffix="秒" onChange={(value) => update("max_task_runtime_seconds", value)} />
          </Form>
        </SettingsPanel>

        <SettingsPanel title="故障判定" icon={<ShieldCheck size={18} />}>
          <Form layout="vertical" className="settings-form-grid" autoComplete="off">
            <NumberItem label="API 故障确认轮数" value={settings.api_failure_confirmation_count} min={1} max={10} suffix="轮" onChange={(value) => update("api_failure_confirmation_count", value)} />
            <NumberItem label="UI 故障确认轮数" value={settings.ui_failure_confirmation_count} min={1} max={10} suffix="轮" onChange={(value) => update("ui_failure_confirmation_count", value)} />
            <NumberItem label="恢复确认次数" value={settings.recovery_confirmation_count} min={1} max={10} suffix="次" onChange={(value) => update("recovery_confirmation_count", value)} />
            <NumberItem label="API 失败重试次数" value={settings.api_retry_attempts} min={0} max={3} suffix="次" onChange={(value) => update("api_retry_attempts", value)} />
            <NumberItem label="UI 失败重试次数" value={settings.ui_retry_attempts} min={0} max={3} suffix="次" onChange={(value) => update("ui_retry_attempts", value)} />
            <NumberItem label="观测过期判定周期数" value={settings.stale_after_intervals} min={1} max={10} suffix="个周期" onChange={(value) => update("stale_after_intervals", value)} />
          </Form>
        </SettingsPanel>
        </>
        )}

        {activeTab === "execution" && <RunnerNodePanel />}

        {activeTab === "execution" && (
        <SettingsPanel title="浏览器运行" icon={<Monitor size={18} />} className="settings-panel-wide">
          <Form layout="vertical" className="settings-form-grid" autoComplete="off">
            <Form.Item label="Headless">
              <Switch aria-label="浏览器 Headless 模式" checked={settings.browser_headless} onChange={(value) => update("browser_headless", value)} />
            </Form.Item>
            <Form.Item label="浏览器类型">
              <Select
                mode="multiple"
                value={normalizeBrowserTypes(settings.enabled_browser_types)}
                onChange={(value) => {
                  const enabled = normalizeBrowserTypes(value);
                  update("enabled_browser_types", enabled);
                  update(
                    "prewarmed_browser_types",
                    normalizeBrowserTypes(settings.prewarmed_browser_types, true).filter((browserType) => enabled.includes(browserType))
                  );
                  update("browser_type", enabled[0] || "chromium");
                }}
                options={BROWSER_TYPES.map((browserType) => ({ label: browserType, value: browserType }))}
              />
            </Form.Item>
            <Form.Item label="自动预热 browser type">
              <Select
                mode="multiple"
                value={normalizeBrowserTypes(settings.prewarmed_browser_types, true).filter((browserType) =>
                  normalizeBrowserTypes(settings.enabled_browser_types).includes(browserType)
                )}
                onChange={(value) => update("prewarmed_browser_types", normalizeBrowserTypes(value, true))}
                options={BROWSER_TYPES.map((browserType) => ({
                  label: browserType,
                  value: browserType,
                  disabled: !normalizeBrowserTypes(settings.enabled_browser_types).includes(browserType)
                }))}
              />
            </Form.Item>
            {BROWSER_TYPES.map((browserType) => (
              <NumberItem
                key={browserType}
                label={`${browserType} Context 池`}
                value={normalizeBrowserPoolSizes(settings.browser_pool_sizes)[browserType]}
                min={1}
                max={5}
                suffix="个"
                onChange={(value) =>
                  update("browser_pool_sizes", {
                    ...normalizeBrowserPoolSizes(settings.browser_pool_sizes),
                    [browserType]: value
                  })
                }
              />
            ))}
            <NumberItem
              label="浏览器回收周期"
              value={settings.browser_recycle_after_runs}
              min={1}
              max={1000}
              suffix="次"
              onChange={(value) => update("browser_recycle_after_runs", value)}
            />
            <Form.Item label="代理" className="span-2">
              <Input
                name="browser-proxy"
                value={settings.browser_proxy}
                onChange={(event) => update("browser_proxy", event.target.value)}
                placeholder="例如：http://127.0.0.1:7890"
                autoComplete="off"
              />
            </Form.Item>
            <Form.Item label="Viewport" className="span-2">
              <Input
                name="browser-viewport"
                value={settings.browser_viewport}
                onChange={(event) => update("browser_viewport", event.target.value)}
                placeholder="例如：1440x900"
                autoComplete="off"
              />
            </Form.Item>
          </Form>
        </SettingsPanel>
        )}

        {activeTab === "variables" && variableScope === "global" && (
        <SettingsPanel
          title="环境变量"
          icon={<KeyRound size={18} />}
          className="settings-panel-wide"
          extra={
            <Space size={8}>
              <Tag>{settings.environment_variables.length} 个变量</Tag>
              <Button type="primary" icon={<Plus size={16} />} onClick={addEnvironmentVariable}>添加变量</Button>
            </Space>
          }
        >
          {settings.environment_variables.length === 0 ? (
            <Empty description="暂无环境变量" />
          ) : (
            <div className="environment-variable-list">
              {settings.environment_variables.map((variable, index) => (
                <EnvironmentVariableEditor
                  key={variable.id}
                  variable={variable}
                  variables={settings.environment_variables}
                  index={index}
                  onPatch={(patch) => updateEnvironmentVariable(variable.id, patch)}
                  onRemove={() => removeEnvironmentVariable(variable.id)}
                />
              ))}
            </div>
          )}
        </SettingsPanel>
        )}

        {activeTab === "system" && (
          <Card
            className="settings-system-workspace settings-panel-wide"
            activeTabKey={systemSettingsSection}
            onTabChange={(key) => setSystemSettingsSection(key as SystemSettingsSection)}
            tabList={[
              { key: "storage", tab: "数据与备份" },
              { key: "access", tab: "开放接口" },
              { key: "maintenance", tab: "维护公告" },
              { key: "package", tab: "配置包" }
            ]}
          >
            {systemSettingsSection === "storage" && (
              <div className="settings-system-grid">
                <Card type="inner" title={<Space size={8}><Database size={16} /><span>数据保留</span></Space>}>
                  <Form layout="vertical" className="settings-form-grid" autoComplete="off">
                    <NumberItem label="运行记录保留" value={settings.run_retention_days} min={1} max={365} suffix="天" onChange={(value) => update("run_retention_days", value)} />
                    <NumberItem label="截图保留" value={settings.screenshot_retention_days} min={1} max={365} suffix="天" onChange={(value) => update("screenshot_retention_days", value)} />
                    <NumberItem label="Trace 保留" value={settings.trace_retention_days} min={1} max={365} suffix="天" onChange={(value) => update("trace_retention_days", value)} />
                    <NumberItem label="Response Body 保留" value={settings.response_retention_days} min={1} max={365} suffix="天" onChange={(value) => update("response_retention_days", value)} />
                    <NumberItem label="同错失败保留" value={settings.similar_failure_retention_count} min={1} max={100} suffix="条" onChange={(value) => update("similar_failure_retention_count", value)} />
                    <NumberItem label="数据库备份保留" value={settings.database_backup_retention} min={1} max={30} suffix="份" onChange={(value) => update("database_backup_retention", value)} />
                    <Form.Item label="记录失败 Trace">
                      <Switch aria-label="记录失败 Trace" checked={settings.trace_artifacts_enabled} onChange={(value) => update("trace_artifacts_enabled", value)} />
                    </Form.Item>
                    <Form.Item label="成功响应产物">
                      <Switch aria-label="保存成功响应产物" checked={settings.success_response_artifacts_enabled} onChange={(value) => update("success_response_artifacts_enabled", value)} />
                    </Form.Item>
                  </Form>
                </Card>

                <Card
                  type="inner"
                  title={<Space size={8}><Database size={16} /><span>数据库备份</span></Space>}
                  extra={<Button icon={<Database size={16} />} loading={backupBusy} onClick={createDatabaseBackup}>创建备份</Button>}
                >
                  <Table<DatabaseBackup>
                    className="settings-backup-table"
                    rowKey="filename"
                    size="small"
                    pagination={false}
                    dataSource={databaseBackups}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据库备份" /> }}
                    columns={[
                      { title: "备份文件", dataIndex: "filename", ellipsis: true },
                      { title: "大小", dataIndex: "size_bytes", width: 92, render: (value: number) => formatBytes(value) },
                      { title: "创建时间", dataIndex: "created_at", width: 176, render: (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false }) },
                      {
                        title: "操作",
                        width: 92,
                        render: (_, backup) => (
                          <Popconfirm
                            title="恢复数据库备份"
                            description="恢复会覆盖当前数据库，并自动创建恢复前安全备份。"
                            okText="确认恢复"
                            cancelText="取消"
                            onConfirm={() => restoreDatabaseBackup(backup.filename)}
                          >
                            <Button size="small" icon={<RotateCcw size={15} />} loading={backupBusy}>恢复</Button>
                          </Popconfirm>
                        )
                      }
                    ]}
                  />
                </Card>
              </div>
            )}

            {systemSettingsSection === "access" && (
              <div className="settings-system-stack">
                <Card
                  type="inner"
                  title={<Space size={8}><ShieldCheck size={16} /><span>开放接口令牌</span><Tag>{readOnlyTokens.length} 个</Tag></Space>}
                  extra={<Button type="primary" icon={<KeyRound size={16} />} loading={savingReadOnlyToken} onClick={openReadOnlyTokenCreate}>新建令牌</Button>}
                >
                  {readOnlyTokens.length === 0 ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无开放接口令牌" />
                  ) : (
                    <div className="read-only-token-list">
                      {readOnlyTokens.map((token) => (
                        <Card size="small" key={token.id}>
                          <div className="read-only-token-card">
                            <div>
                              <strong>{token.name || "未命名令牌"}</strong>
                              <span>创建时间 {formatDate(token.created_at)}</span>
                              <span>项目范围：{token.project_ids.map((projectId) => projects.find((project) => project.id === projectId)?.name || `#${projectId}`).join("、")}</span>
                            </div>
                            <Popconfirm
                              title="删除开放接口令牌"
                              description="删除后，使用该令牌的外部调用会立即失效。"
                              okText="删除"
                              cancelText="取消"
                              onConfirm={() => deleteReadOnlyToken(token.id)}
                            >
                              <Button danger icon={<Trash2 size={16} />} loading={savingReadOnlyToken}>删除</Button>
                            </Popconfirm>
                          </div>
                        </Card>
                      ))}
                    </div>
                  )}
                </Card>
                <ReadOnlyApiDocs onCopyPath={(path) => copyValue(path, "已复制 Path")} />
              </div>
            )}

            {systemSettingsSection === "maintenance" && (
              <Card
                type="inner"
                title={<Space size={8}><ShieldCheck size={16} /><span>维护公告</span></Space>}
                extra={
                  <Space size={8}>
                    <Tag color={enabledTagColor(Boolean(settings.maintenance_enabled))}>{settings.maintenance_enabled ? "展示中" : "未启用"}</Tag>
                    <Switch aria-label="启用维护公告" checked={Boolean(settings.maintenance_enabled)} onChange={(value) => update("maintenance_enabled", value)} />
                  </Space>
                }
              >
                <Descriptions size="small" column={1} items={[{ key: "position", label: "展示位置", children: "所有页面顶栏公告入口" }]} />
                <Form layout="vertical" className="settings-form-grid settings-maintenance-form" autoComplete="off">
                  <Form.Item label="标题" className="span-2">
                    <Input value={settings.maintenance_title || ""} onChange={(event) => update("maintenance_title", event.target.value)} />
                  </Form.Item>
                  <Form.Item label="开始时间">
                    <Input value={settings.maintenance_starts_at || ""} onChange={(event) => update("maintenance_starts_at", event.target.value)} placeholder="2026-06-08T22:00:00+08:00" />
                  </Form.Item>
                  <Form.Item label="结束时间">
                    <Input value={settings.maintenance_ends_at || ""} onChange={(event) => update("maintenance_ends_at", event.target.value)} placeholder="2026-06-08T23:00:00+08:00" />
                  </Form.Item>
                  <Form.Item label="公告内容" className="span-2">
                    <Input.TextArea value={settings.maintenance_message || ""} rows={5} onChange={(event) => update("maintenance_message", event.target.value)} />
                  </Form.Item>
                </Form>
              </Card>
            )}

            {systemSettingsSection === "package" && (
              <ConfigTransferPanel
                embedded
                scope="system"
                exporting={exportingConfig}
                uploadProps={importUploadProps}
                drawerOpen={importDrawerOpen}
                fileName={importFileName}
                hasImportBundle={Boolean(importBundle)}
                preview={importPreview}
                previewing={importPreviewing}
                importing={importingConfig}
                applyDisabledReason={importApplyDisabledReason}
                importDisabledReason=""
                hasUnsavedChanges={hasUnsavedChanges}
                onExport={exportConfig}
                onOpenImport={() => setImportDrawerOpen(true)}
                onCloseImport={() => setImportDrawerOpen(false)}
                onResetImport={resetImportDraft}
                onPreviewImport={previewConfigImport}
                onRequestApply={() => setImportConfirmOpen(true)}
              />
            )}
          </Card>
        )}
      </section>

      <Modal
        title="应用导入"
        open={importConfirmOpen}
        okText="应用导入"
        cancelText="取消"
        okButtonProps={{ danger: true, disabled: Boolean(importApplyDisabledReason) }}
        confirmLoading={importingConfig}
        onOk={applyConfigImport}
        onCancel={() => setImportConfirmOpen(false)}
      >
        <div className="config-import-confirm">
          <Alert type="warning" title="导入会应用预检范围内的配置" showIcon />
          {importFileName && (
            <div className="config-import-file">
              <span>文件</span>
              <strong>{importFileName}</strong>
            </div>
          )}
        </div>
      </Modal>

      <Modal
        title="新建开放接口令牌"
        open={readOnlyTokenCreateOpen}
        okText="新建"
        cancelText="取消"
        confirmLoading={savingReadOnlyToken}
        okButtonProps={{ disabled: readOnlyTokenProjectIds.length === 0 }}
        onOk={createReadOnlyToken}
        onCancel={() => setReadOnlyTokenCreateOpen(false)}
      >
        <Space orientation="vertical" size={12} className="drawer-stack">
          <Form layout="vertical">
            <Form.Item
              label={
                <Space size={6}>
                  <span>令牌名称</span>
                  <Tooltip title="名称只用于区分调用方。">
                    <Info size={14} />
                  </Tooltip>
                </Space>
              }
            >
              <Input
                value={readOnlyTokenNameDraft}
                maxLength={120}
                placeholder="例如：外部看板、巡检脚本"
                onChange={(event) => setReadOnlyTokenNameDraft(event.target.value)}
              />
            </Form.Item>
            <Form.Item label="允许访问的项目" required>
              <Select
                mode="multiple"
                value={readOnlyTokenProjectIds}
                onChange={setReadOnlyTokenProjectIds}
                options={projects.map((project) => ({ label: project.name, value: project.id }))}
                placeholder="至少选择一个项目"
              />
            </Form.Item>
          </Form>
        </Space>
      </Modal>

      <Modal
        title="请保存新令牌"
        open={Boolean(createdReadOnlyToken)}
        okText="我已保存"
        cancelButtonProps={{ style: { display: "none" } }}
        mask={{ closable: false }}
        onOk={() => setCreatedReadOnlyToken(null)}
        onCancel={() => setCreatedReadOnlyToken(null)}
      >
        <Space orientation="vertical" size={12} className="drawer-stack">
          <Alert
            type="warning"
            showIcon
            title="令牌明文关闭后不可再次查看"
            description="请立即复制并保存到调用方。新令牌会与已有令牌共存，后续设置页只显示名称和创建时间。"
          />
          <div className="created-token-box">
            <span>{createdReadOnlyToken?.name}</span>
            <code>{createdReadOnlyToken?.token}</code>
            <Tooltip title="复制令牌">
              <Button
                type="text"
                icon={<Clipboard size={15} />}
                onClick={() => createdReadOnlyToken && copyValue(createdReadOnlyToken.token, "已复制令牌")}
                aria-label="复制新令牌"
              />
            </Tooltip>
          </div>
        </Space>
      </Modal>
    </div>
  );
}
