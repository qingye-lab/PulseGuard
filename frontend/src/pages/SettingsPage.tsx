import { Alert, App, Button, Card, Checkbox, Descriptions, Drawer, Dropdown, Empty, Form, Input, InputNumber, Modal, Popconfirm, Segmented, Select, Skeleton, Space, Switch, Table, Tag, Tooltip, Upload } from "antd";
import type { MenuProps, UploadProps } from "antd";
import {
  BellRing,
  CheckCircle2,
  Clipboard,
  Database,
  Download,
  FileJson2,
  FolderKanban,
  Info,
  KeyRound,
  Monitor,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Send,
  ShieldCheck,
  Trash2,
  UploadCloud,
  Zap
} from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useId, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { StructuredViewer } from "../components/StructuredViewer";
import type {
  AlertTagPolicy,
  AlertPreview,
  AlertPreviewChannel,
  BrowserType,
  ConfigBundle,
  ConfigExportFile,
  ConfigImportPreview,
  ConfigImportSummary,
  DatabaseBackup,
  EnvironmentVariable,
  NotificationChannel,
  ProbeRunner,
  ProbeRunnerPayload,
  RelayStatus,
  RunnerProvisionResult,
  RunnerUpdateStatus,
  SettingsValues,
  WebhookType
} from "../types";
import { copyTextToClipboard, dirtyTagColor, enabledTagColor, formatDate } from "../utils";
import { useProject } from "../ProjectContext";

type SettingsTab = "alerts" | "system-alerts" | "execution" | "variables" | "system";

const SETTINGS_TAB_LABELS: Record<SettingsTab, string> = {
  alerts: "项目告警",
  "system-alerts": "系统告警",
  execution: "执行配置",
  variables: "变量管理",
  system: "系统配置"
};

const SETTINGS_TAB_ORDER: SettingsTab[] = ["alerts", "system-alerts", "execution", "variables", "system"];

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

const BROWSER_TYPES: BrowserType[] = ["chromium", "firefox", "webkit"];

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

const CHANNEL_OPTIONS: Array<{ label: string; value: WebhookType }> = [
  { label: "飞书", value: "feishu" },
  { label: "企业微信", value: "wecom" },
  { label: "钉钉", value: "dingtalk" }
];

const CHANNEL_GUIDES: Record<WebhookType, { label: string; expectedHost: string; webhookHint: string; secretHint: string }> = {
  feishu: {
    label: "飞书",
    expectedHost: "open.feishu.cn",
    webhookHint: "粘贴飞书群机器人 Webhook 地址",
    secretHint: "飞书文本机器人无需额外密钥"
  },
  wecom: {
    label: "企业微信",
    expectedHost: "qyapi.weixin.qq.com",
    webhookHint: "粘贴企业微信群机器人 Webhook 地址",
    secretHint: "企业微信群机器人无需额外密钥"
  },
  dingtalk: {
    label: "钉钉",
    expectedHost: "oapi.dingtalk.com",
    webhookHint: "粘贴钉钉自定义机器人 Webhook 地址",
    secretHint: "启用加签时填写 SEC 开头的密钥"
  }
};

const VARIABLE_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

const CONFIG_SUMMARY_ITEMS: Array<{ key: keyof ConfigImportSummary; label: string }> = [
  { key: "checks", label: "任务" },
  { key: "ui_checks", label: "UI 任务" },
  { key: "api_checks", label: "API 任务" },
  { key: "runners", label: "执行节点" },
  { key: "settings", label: "设置项" },
  { key: "conflicts", label: "冲突" },
  { key: "notification_channels", label: "通知渠道" },
  { key: "environment_variables", label: "环境变量" },
  { key: "variables", label: "变量" }
];

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

const emptyRunnerDraft: ProbeRunnerPayload = {
  name: "",
  address: "",
  network_region: "local",
  enabled: true,
  token: "",
  allow_insecure_http: false
};

type RunnerCreateMode = "manual" | "relay";
type RunnerProvisionPlatform = "linux" | "powershell";

function runnerStatusLabel(runner: ProbeRunner): string {
  if (!runner.enabled) return "已停用";
  const status = runner.status || "";
  const labels: Record<string, string> = {
    pending_deployment: "待部署",
    expired: "已过期",
    connecting: "连接中",
    updating: "更新中",
    available: "可用",
    unhealthy: "异常",
    unavailable: "异常",
    auth_failed: "认证失败",
    ok: "可用",
    warning: "异常",
    offline: "异常",
    disabled: "已停用"
  };
  return labels[status] || (runner.available ? "可用" : "异常");
}

function runnerStatusColor(runner: ProbeRunner): "default" | "processing" | "success" | "warning" | "error" {
  if (!runner.enabled) return "default";
  if (runner.available || runner.status === "available" || runner.status === "ok") return "success";
  if (runner.status === "pending_deployment" || runner.status === "connecting" || runner.status === "updating") return "processing";
  if (runner.status === "expired" || runner.status === "auth_failed") return "warning";
  return "error";
}

function runnerMetaText(runner: ProbeRunner, key: string): string {
  const value = runner.metadata?.[key];
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function runnerMetaBool(runner: ProbeRunner, key: string): boolean {
  return runner.metadata?.[key] === true;
}

function runnerImageSummary(image: string): string {
  if (!image) return "-";
  const parts = image.split("/");
  return parts[parts.length - 1] || image;
}

function runnerAddressUsesInsecureHttp(address: string): boolean {
  const value = address.trim().toLowerCase();
  return Boolean(value) && (value.startsWith("http://") || !value.includes("://"));
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function waitForRunnerUpdateStatus(runnerId: string, updateId?: string): Promise<RunnerUpdateStatus | undefined> {
  let latest: RunnerUpdateStatus | undefined;
  for (let attempt = 0; attempt < 75; attempt += 1) {
    await wait(attempt === 0 ? 1000 : 2000);
    const result = await api.runnerUpdateStatus(runnerId);
    const update = result.worker?.update;
    if (!update) continue;
    if (updateId && update.update_id && update.update_id !== updateId) continue;
    latest = update;
    if (latest.status === "succeeded" || latest.status === "failed") return latest;
  }
  return latest;
}

function updateStatusLines(status?: RunnerUpdateStatus): Record<string, unknown> {
  if (!status) return { 状态: "暂无状态" };
  return {
    状态: status.status || "-",
    当前镜像: status.current_image || "-",
    目标镜像: status.target_image || "-",
    原镜像: status.previous_image || "-",
    更新服务: status.update_services?.join(", ") || "-",
    是否可更新: typeof status.update_available === "boolean" ? (status.update_available ? "是" : "否") : "-",
    开始时间: status.started_at || "-",
    完成时间: status.finished_at || "-",
    更新时间: status.updated_at || "-",
    消息: status.message || "-"
  };
}

function RunnerNodePanel() {
  const { message } = App.useApp();
  const [runners, setRunners] = useState<ProbeRunner[]>([]);
  const [relayStatus, setRelayStatus] = useState<RelayStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [createMode, setCreateMode] = useState<RunnerCreateMode>("manual");
  const [provisionPlatform, setProvisionPlatform] = useState<RunnerProvisionPlatform>("linux");
  const [provisionComposeUrl, setProvisionComposeUrl] = useState("");
  const [provisionResult, setProvisionResult] = useState<RunnerProvisionResult | null>(null);
  const [draft, setDraft] = useState<ProbeRunnerPayload>(emptyRunnerDraft);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [authRunner, setAuthRunner] = useState<{ runner: ProbeRunner; token: string } | null>(null);
  const [editRunner, setEditRunner] = useState<{ runner: ProbeRunner; draft: ProbeRunnerPayload } | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);

  useEffect(() => {
    void loadRunners();
  }, []);

  async function loadRunners() {
    setLoading(true);
    try {
      const [nextRunners, nextRelayStatus] = await Promise.all([api.runners(), api.relayStatus()]);
      setRunners(nextRunners);
      setRelayStatus(nextRelayStatus);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  function openCreateDrawer() {
    setDraft(emptyRunnerDraft);
    setCreateMode("manual");
    setProvisionPlatform("linux");
    setProvisionComposeUrl("");
    setDrawerOpen(true);
  }

  function openEditDrawer(runner: ProbeRunner) {
    setEditRunner({
      runner,
      draft: {
        name: runner.name || runner.runner_id,
        address: runner.address || "",
        network_region: runner.network_region || "local",
        enabled: runner.enabled,
        allow_insecure_http: Boolean(runner.allow_insecure_http)
      }
    });
  }

  async function createRunner() {
    if (createMode === "relay") {
      const payload = {
        name: draft.name.trim(),
        network_region: draft.network_region.trim() || "local",
        target_platform: provisionPlatform,
        compose_url: provisionComposeUrl.trim(),
        enabled: draft.enabled !== false
      };
      if (!payload.name || !payload.compose_url) {
        message.error("请填写节点名称和 Compose URL");
        return;
      }
      setSaving(true);
      try {
        const result = await api.provisionRunner(payload);
        setProvisionResult(result);
        setDrawerOpen(false);
        await loadRunners();
        message.success("部署命令已生成");
      } catch (err) {
        message.error((err as Error).message);
      } finally {
        setSaving(false);
      }
      return;
    }
    const payload: ProbeRunnerPayload = {
      name: draft.name.trim(),
      address: draft.address.trim(),
      network_region: draft.network_region.trim() || "local",
      enabled: draft.enabled !== false,
      token: draft.token?.trim() || "",
      allow_insecure_http: runnerAddressUsesInsecureHttp(draft.address) && Boolean(draft.allow_insecure_http)
    };
    if (!payload.name || !payload.address || !payload.token) {
      message.error("请填写节点名称、地址和认证信息");
      return;
    }
    if (runnerAddressUsesInsecureHttp(payload.address) && !payload.allow_insecure_http) {
      message.error("请确认手工 HTTP 节点的明文传输风险，或改用 HTTPS / Relay");
      return;
    }
    setSaving(true);
    try {
      await api.createRunner(payload);
      setDrawerOpen(false);
      await loadRunners();
      message.success("执行节点已创建");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function updateRunnerEnabled(runner: ProbeRunner, enabled: boolean) {
    setBusyId(runner.runner_id);
    try {
      await api.updateRunner(runner.runner_id, { enabled });
      await loadRunners();
      message.success(enabled ? "节点已启用" : "节点已停用");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function saveRunnerEdit() {
    if (!editRunner) return;
    const payload: ProbeRunnerPayload = {
      name: editRunner.draft.name.trim(),
      address: editRunner.draft.address.trim(),
      network_region: editRunner.draft.network_region.trim() || "local",
      enabled: editRunner.draft.enabled !== false,
      allow_insecure_http:
        editRunner.runner.role === "child" &&
        editRunner.runner.connection_mode !== "relay" &&
        runnerAddressUsesInsecureHttp(editRunner.draft.address) &&
        Boolean(editRunner.draft.allow_insecure_http)
    };
    if (!payload.name || !payload.address) {
      message.error("请填写节点名称和地址");
      return;
    }
    if (
      editRunner.runner.role === "child" &&
      editRunner.runner.connection_mode !== "relay" &&
      runnerAddressUsesInsecureHttp(payload.address) &&
      !payload.allow_insecure_http
    ) {
      message.error("请确认手工 HTTP 节点的明文传输风险，或改用 HTTPS / Relay");
      return;
    }
    setSaving(true);
    setBusyId(editRunner.runner.runner_id);
    try {
      await api.updateRunner(editRunner.runner.runner_id, payload);
      setEditRunner(null);
      await loadRunners();
      message.success("执行节点已更新");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSaving(false);
      setBusyId(null);
    }
  }

  async function testRunner(runner: ProbeRunner) {
    setBusyId(runner.runner_id);
    try {
      const result = await api.testRunner(runner.runner_id);
      await loadRunners();
      message.success(result.message || "节点连接正常");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function updateRunnerToken() {
    if (!authRunner) return;
    const token = authRunner.token.trim();
    if (!token) {
      message.error("请填写认证信息");
      return;
    }
    setBusyId(authRunner.runner.runner_id);
    try {
      await api.updateRunner(authRunner.runner.runner_id, { token });
      setAuthRunner(null);
      await loadRunners();
      message.success("节点认证已更新");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function deleteRunner(runner: ProbeRunner) {
    setBusyId(runner.runner_id);
    try {
      await api.deleteRunner(runner.runner_id);
      await loadRunners();
      message.success("执行节点已删除");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  function confirmRunnerDelete(runner: ProbeRunner) {
    Modal.confirm({
      title: "删除执行节点",
      content: "删除后，仍引用该节点的任务会在执行时回退到可用节点。",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => deleteRunner(runner)
    });
  }

  async function copyProvisionCommand(result: RunnerProvisionResult) {
    try {
      await copyTextToClipboard(result.deployment.command);
      message.success("部署命令已复制");
    } catch {
      message.error("复制失败，请手动选择命令");
    }
  }

  async function regenerateProvision(runner: ProbeRunner, platform: RunnerProvisionPlatform = "linux") {
    const composeUrl = window.prompt("请输入 relay worker compose URL", provisionComposeUrl.trim());
    if (composeUrl === null) return;
    const normalizedComposeUrl = composeUrl.trim();
    if (!normalizedComposeUrl) {
      message.error("请填写 Compose URL");
      return;
    }
    setProvisionComposeUrl(normalizedComposeUrl);
    setBusyId(runner.runner_id);
    try {
      const result = await api.regenerateRunnerProvision(runner.runner_id, {
        name: runner.name || runner.runner_id,
        network_region: runner.network_region || "local",
        target_platform: platform,
        compose_url: normalizedComposeUrl,
        enabled: runner.enabled
      });
      setProvisionResult(result);
      await loadRunners();
      message.success("部署命令已重新生成");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  function confirmRunnerUpdate(runner: ProbeRunner) {
    const targetImage = runnerMetaText(runner, "update_target_image");
    const currentImage = runnerMetaText(runner, "image");
    Modal.confirm({
      title: "更新执行节点",
      content: (
        <Space orientation="vertical" size={8}>
          <span>主节点会向子节点下发受控更新请求，由子节点 updater 拉取镜像并重建 worker 服务。</span>
          <span>当前镜像：{currentImage || "-"}</span>
          <span>目标镜像：{targetImage || "子节点默认目标镜像"}</span>
        </Space>
      ),
      okText: "下发更新",
      cancelText: "取消",
      onOk: async () => {
        setBusyId(runner.runner_id);
        try {
          const result = await api.updateRunnerNode(runner.runner_id, targetImage ? { target_image: targetImage } : {});
          const updateId = result.worker?.update?.update_id;
          message.success(result.message || "节点更新任务已下发，正在等待结果");
          const finalStatus = await waitForRunnerUpdateStatus(runner.runner_id, updateId);
          if (finalStatus?.status === "succeeded") {
            message.success("节点更新成功");
          } else if (finalStatus?.status === "failed") {
            message.error(finalStatus.message || "节点更新失败");
          } else {
            message.warning("节点更新仍在执行，请稍后查看更新状态");
          }
          await loadRunners();
        } catch (err) {
          message.error((err as Error).message);
        } finally {
          setBusyId(null);
        }
      }
    });
  }

  async function showRunnerUpdateStatus(runner: ProbeRunner) {
    setBusyId(runner.runner_id);
    try {
      const result = await api.runnerUpdateStatus(runner.runner_id);
      Modal.info({
        title: "节点更新状态",
        width: 620,
        content: <StructuredViewer value={updateStatusLines(result.worker?.update)} />
      });
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <SettingsPanel title="执行节点" icon={<Monitor size={18} />} className="settings-panel-wide">
      <Space orientation="vertical" size={14} className="drawer-stack">
        <div className="read-only-token-toolbar">
          <Space size={8} wrap>
            <strong>节点列表</strong>
            <Tag color="processing">{runners.length} 个节点</Tag>
          </Space>
          <Space size={8} wrap>
            <Button icon={<Info size={16} />} onClick={() => setHelpOpen(true)}>
              部署方法
            </Button>
            <Button icon={<RefreshCw size={16} />} loading={loading} onClick={loadRunners}>
              刷新
            </Button>
            <Button type="primary" icon={<Plus size={16} />} onClick={openCreateDrawer}>
              新增子节点
            </Button>
          </Space>
        </div>

        {loading ? (
          <Skeleton active paragraph={{ rows: 4 }} />
        ) : runners.length === 0 ? (
          <Empty description="暂无执行节点" />
        ) : (
          <Space orientation="vertical" size={10} className="drawer-stack">
            {runners.map((runner) => {
              const version = runnerMetaText(runner, "version");
              const buildSha = runnerMetaText(runner, "build_sha");
              const image = runnerMetaText(runner, "image");
              const targetImage = runnerMetaText(runner, "update_target_image");
              const updateSupported = runnerMetaBool(runner, "update_supported");
              const updateAvailable = runnerMetaBool(runner, "update_available");
              const updateActionEnabled = updateSupported && updateAvailable && runner.available;
              const updateActionLabel = updateAvailable ? (
                <Space size={6}>
                  <span>更新节点</span>
                  <Tag color="warning">可更新</Tag>
                </Space>
              ) : (
                "更新节点"
              );
              const isBusy = busyId === runner.runner_id;
              const tokenStatus = runner.token_set ? runner.token_hint || "已配置" : "未配置";
              const imageSummary = image ? runnerImageSummary(image) : "-";
              const buildSummary = buildSha && buildSha !== "unknown" ? buildSha.slice(0, 8) : "-";
              const runnerMenuItems: MenuProps["items"] = [
                { key: "auth", icon: <KeyRound size={15} />, label: "更新认证" },
                ...(runner.role !== "local" && runner.connection_mode === "relay"
                  ? [{ key: "deploy", icon: <Clipboard size={15} />, label: "重新生成命令" }]
                  : []),
                { key: "update", icon: <Download size={15} />, label: updateActionLabel, disabled: !updateActionEnabled },
                { key: "status", icon: <Info size={15} />, label: "更新状态", disabled: !updateSupported },
                { type: "divider" },
                { key: "delete", icon: <Trash2 size={15} />, label: "删除", danger: true }
              ];
              const onRunnerMenuClick: MenuProps["onClick"] = ({ key }) => {
                if (key === "auth") setAuthRunner({ runner, token: "" });
                if (key === "deploy") void regenerateProvision(runner, "linux");
                if (key === "update") confirmRunnerUpdate(runner);
                if (key === "status") void showRunnerUpdateStatus(runner);
                if (key === "delete") confirmRunnerDelete(runner);
              };
              return (
                <Card size="small" key={runner.runner_id} className="runner-node-card">
                  <div className="runner-node-card-shell">
                    <div className="runner-node-main">
                      <div className="runner-node-heading">
                        <div className="runner-node-title-row">
                          <strong>{runner.name || runner.runner_id}</strong>
                          <Space size={6} wrap className="runner-node-tags">
                            <Tag>{runner.role === "local" ? "本机" : "子节点"}</Tag>
                            {runner.role !== "local" && <Tag>{runner.connection_mode === "relay" ? "Relay 自动" : "手动直连"}</Tag>}
                            {runner.role !== "local" &&
                              runner.connection_mode !== "relay" &&
                              runnerAddressUsesInsecureHttp(runner.address) && (
                                <Tag color={runner.allow_insecure_http ? "warning" : "error"}>
                                  {runner.allow_insecure_http ? "HTTP 风险已确认" : "HTTP 未确认"}
                                </Tag>
                              )}
                            <Tag color={runner.enabled ? "success" : "default"}>{runner.enabled ? "已启用" : "已停用"}</Tag>
                            <Tag color={runnerStatusColor(runner)}>{runnerStatusLabel(runner)}</Tag>
                            <Tag>{runner.network_region || "local"}</Tag>
                            {version && <Tag color="blue">v{version}</Tag>}
                            {runner.role !== "local" && (
                              <Tag color={updateSupported ? (updateAvailable ? "warning" : "processing") : "default"}>
                                {updateSupported ? (updateAvailable ? "有可更新镜像" : "支持平台更新") : "未启用 updater"}
                              </Tag>
                            )}
                          </Space>
                        </div>
                        <span className="runner-node-address">
                          {runner.address || "-"} · 最近心跳 {runner.last_seen_at ? formatDate(runner.last_seen_at) : "无"}
                        </span>
                      </div>
                      <div className="runner-node-facts">
                        <div className="runner-node-fact">
                          <span>认证</span>
                          <strong>{runner.connection_mode === "relay" && runner.relay_token_hint ? `${tokenStatus} / ${runner.relay_token_hint}` : tokenStatus}</strong>
                        </div>
                        <div className="runner-node-fact">
                          <span>镜像</span>
                          <strong>{imageSummary}</strong>
                        </div>
                        <div className="runner-node-fact">
                          <span>构建</span>
                          <strong>{buildSummary}</strong>
                        </div>
                      </div>
                      {runner.connection_mode === "relay" && runner.deploy_command_expires_at && (
                        <span className="runner-node-target">部署命令过期时间 {formatDate(runner.deploy_command_expires_at)}</span>
                      )}
                      {runner.connection_mode === "relay" && runner.last_disconnect_reason && (
                        <span className="runner-node-target">断开原因 {runner.last_disconnect_reason}</span>
                      )}
                      {targetImage && targetImage !== image && <span className="runner-node-target">目标镜像 {runnerImageSummary(targetImage)}</span>}
                    </div>
                    <div className="runner-node-controls">
                      <div className="runner-node-toggle">
                        <span>{runner.enabled ? "启用" : "停用"}</span>
                        <Switch checked={runner.enabled} loading={isBusy} onChange={(enabled) => updateRunnerEnabled(runner, enabled)} aria-label={`切换执行节点 ${runner.runner_id}`} />
                      </div>
                      <div className="runner-node-actions">
                        <Button size="small" icon={<Pencil size={15} />} loading={isBusy} onClick={() => openEditDrawer(runner)}>
                          编辑
                        </Button>
                        <Button size="small" loading={isBusy} onClick={() => testRunner(runner)}>
                          测试
                        </Button>
                        {runner.role !== "local" && (
                          <Dropdown menu={{ items: runnerMenuItems, onClick: onRunnerMenuClick }} trigger={["click"]}>
                            <Button size="small" type={updateActionEnabled ? "primary" : "default"} icon={<MoreHorizontal size={15} />} loading={isBusy}>
                              更多
                            </Button>
                          </Dropdown>
                        )}
                      </div>
                    </div>
                  </div>
                </Card>
              );
            })}
          </Space>
        )}
      </Space>

      <Drawer
        title="新增子节点"
        size={520}
        open={drawerOpen}
        destroyOnHidden
        onClose={() => setDrawerOpen(false)}
        extra={
          <Button type="primary" icon={<Save size={16} />} loading={saving} onClick={createRunner}>
            {createMode === "relay" ? "生成" : "创建"}
          </Button>
        }
      >
        <Form layout="vertical" autoComplete="off">
          <Form.Item label="接入模式">
            <Segmented
              block
              value={createMode}
              onChange={(value) => setCreateMode(value as RunnerCreateMode)}
              options={[
                { label: "手动添加", value: "manual" },
                { label: "生成部署命令", value: "relay" }
              ]}
            />
          </Form.Item>
          <Form.Item label="节点名称" required>
            <Input value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} />
          </Form.Item>
          {createMode === "manual" && (
            <>
              <Form.Item label="节点地址" required help="优先填写 HTTPS 地址；裸 IP/域名会按 worker 默认端口补成 http://地址:8788。">
                <Input
                  value={draft.address}
                  placeholder="10.0.0.12 或 http://10.0.0.12:8788"
                  onChange={(event) => setDraft((current) => ({ ...current, address: event.target.value }))}
                />
              </Form.Item>
              {runnerAddressUsesInsecureHttp(draft.address) && (
                <Form.Item>
                  <Checkbox
                    checked={Boolean(draft.allow_insecure_http)}
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, allow_insecure_http: event.target.checked }))
                    }
                  >
                    我确认该节点位于可信网络，允许 token、任务参数和环境变量通过明文 HTTP 传输
                  </Checkbox>
                </Form.Item>
              )}
              <Form.Item label="认证信息" required help="粘贴 pgrn_ 开头的 token；也可以粘贴日志里的 token: pgrn_... 整行。">
                <Input.Password
                  value={draft.token}
                  placeholder="pgrn_..."
                  onChange={(event) => setDraft((current) => ({ ...current, token: event.target.value }))}
                />
              </Form.Item>
            </>
          )}
          {createMode === "relay" && (
            <>
              <Form.Item label="目标平台">
                <Segmented
                  block
                  value={provisionPlatform}
                  onChange={(value) => setProvisionPlatform(value as RunnerProvisionPlatform)}
                  options={[
                    { label: "Linux shell", value: "linux" },
                    { label: "PowerShell", value: "powershell" }
                  ]}
                />
              </Form.Item>
              <Form.Item label="Relay 状态">
                <Space size={8} wrap>
                  <Tag color={relayStatus?.enabled ? "success" : "warning"}>{relayStatus?.enabled ? "可生成命令" : "未就绪"}</Tag>
                  <span>{relayStatus?.public_url || relayStatus?.error || "-"}</span>
                </Space>
              </Form.Item>
              <Form.Item label="Compose URL" required help="填写 worker 宿主机可访问的 docker-compose.relay-worker.yml 下载地址。">
                <Input
                  value={provisionComposeUrl}
                  placeholder="https://example.com/docker-compose.relay-worker.yml"
                  onChange={(event) => setProvisionComposeUrl(event.target.value)}
                />
              </Form.Item>
            </>
          )}
          <Form.Item label="网络区域">
            <Input
              value={draft.network_region}
              placeholder="local"
              onChange={(event) => setDraft((current) => ({ ...current, network_region: event.target.value }))}
            />
          </Form.Item>
          <Form.Item label="启用状态">
            <Switch checked={draft.enabled !== false} onChange={(enabled) => setDraft((current) => ({ ...current, enabled }))} />
          </Form.Item>
        </Form>
      </Drawer>

      <Drawer
        title="编辑执行节点"
        size={520}
        open={Boolean(editRunner)}
        destroyOnHidden
        onClose={() => setEditRunner(null)}
        extra={
          <Button type="primary" icon={<Save size={16} />} loading={saving} onClick={saveRunnerEdit}>
            保存
          </Button>
        }
      >
        <Form layout="vertical" autoComplete="off">
          <Form.Item label="节点名称" required>
            <Input
              value={editRunner?.draft.name || ""}
              onChange={(event) =>
                setEditRunner((current) =>
                  current ? { ...current, draft: { ...current.draft, name: event.target.value } } : current
                )
              }
            />
          </Form.Item>
          <Form.Item label="节点地址" required help="可填写裸 IP/域名，系统会按 worker 默认端口补成 http://地址:8788。">
            <Input
              value={editRunner?.draft.address || ""}
              placeholder={editRunner?.runner.role === "local" ? "127.0.0.1" : "10.0.0.12 或 http://10.0.0.12:8788"}
              onChange={(event) =>
                setEditRunner((current) =>
                  current ? { ...current, draft: { ...current.draft, address: event.target.value } } : current
                )
              }
            />
          </Form.Item>
          {editRunner?.runner.role === "child" &&
            editRunner.runner.connection_mode !== "relay" &&
            runnerAddressUsesInsecureHttp(editRunner.draft.address) && (
              <Form.Item>
                <Checkbox
                  checked={Boolean(editRunner.draft.allow_insecure_http)}
                  onChange={(event) =>
                    setEditRunner((current) =>
                      current
                        ? {
                            ...current,
                            draft: { ...current.draft, allow_insecure_http: event.target.checked }
                          }
                        : current
                    )
                  }
                >
                  我确认该节点位于可信网络，允许 token、任务参数和环境变量通过明文 HTTP 传输
                </Checkbox>
              </Form.Item>
            )}
          <Form.Item label="网络区域">
            <Input
              value={editRunner?.draft.network_region || ""}
              placeholder="local"
              onChange={(event) =>
                setEditRunner((current) =>
                  current ? { ...current, draft: { ...current.draft, network_region: event.target.value } } : current
                )
              }
            />
          </Form.Item>
          <Form.Item label="启用状态">
            <Switch
              checked={editRunner?.draft.enabled !== false}
              onChange={(enabled) =>
                setEditRunner((current) => (current ? { ...current, draft: { ...current.draft, enabled } } : current))
              }
            />
          </Form.Item>
        </Form>
      </Drawer>

      <Modal
        title="更新节点认证"
        open={Boolean(authRunner)}
        okText="保存"
        cancelText="取消"
        confirmLoading={Boolean(authRunner && busyId === authRunner.runner.runner_id)}
        onOk={updateRunnerToken}
        onCancel={() => setAuthRunner(null)}
      >
        <Form layout="vertical" autoComplete="off">
          <Form.Item label="认证信息" required help="粘贴 pgrn_ 开头的 token；也可以粘贴日志里的 token: pgrn_... 整行。">
            <Input.Password
              value={authRunner?.token || ""}
              placeholder="pgrn_..."
              onChange={(event) => setAuthRunner((current) => (current ? { ...current, token: event.target.value } : current))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="子节点部署命令"
        open={Boolean(provisionResult)}
        width={860}
        okText="复制命令"
        cancelText="关闭"
        onOk={() => provisionResult && copyProvisionCommand(provisionResult)}
        onCancel={() => setProvisionResult(null)}
      >
        <Space orientation="vertical" size={10} className="drawer-stack">
          <Space size={8} wrap>
            <Tag color="processing">{provisionResult?.runner_id}</Tag>
            <Tag>{provisionResult?.deployment.target_platform === "powershell" ? "PowerShell" : "Linux shell"}</Tag>
            {provisionResult?.deployment.compose_url && <Tag>{provisionResult.deployment.compose_url}</Tag>}
            <span>过期时间 {provisionResult?.deployment.expires_at ? formatDate(provisionResult.deployment.expires_at) : "-"}</span>
          </Space>
          {provisionResult?.deployment.server_fingerprint && (
            <div className="runner-provision-fingerprint">
              <span>Server fingerprint</span>
              <code>{provisionResult.deployment.server_fingerprint}</code>
            </div>
          )}
          <pre>{provisionResult?.deployment.command || ""}</pre>
          <span>命令只显示一次。重新生成会轮换 relay token；在线子节点会在部署窗口内继续使用上一版 token，新版本连接成功后旧 token 自动失效。标准部署命令默认启用 updater。</span>
        </Space>
      </Modal>

      <Modal title="创建子节点" open={helpOpen} footer={null} onCancel={() => setHelpOpen(false)} width={820}>
        <Space orientation="vertical" size={12} className="drawer-stack">
          <span>子节点支持两种接入方式：手动添加适合主节点能直接访问 worker 的网络；生成部署命令适合 worker 只能主动出站连接主节点 relay 的网络。</span>
          <div className="worker-help-block">
            <strong>Relay 自动接入</strong>
            <pre>{`docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d`}</pre>
            <span>主节点启用 relay 后，在新增子节点里选择“生成部署命令”，复制命令到 worker 宿主机执行。worker 不需要开放 8788 入站端口，部署命令默认启动 updater。</span>
          </div>
          <span>手动添加模式优先使用 HTTPS。仅在可信内网无法提供 HTTPS 时，才显式确认使用明文 HTTP；否则使用 Relay 自动接入。</span>
          <div className="worker-help-block">
            <strong>推荐部署：公司 Git 源码构建（Windows PowerShell）</strong>
            <pre>{`git clone git@git.corpautohome.com:liyanqing/PulseGuard.git
cd PulseGuard
$env:PULSEGUARD_WORKER_NAME = $env:COMPUTERNAME
$env:PULSEGUARD_WORKER_REGION = "default"
$env:COMPOSE_PROJECT_NAME = "pulseguard-worker"
$env:COMPOSE_PROFILES = "updater"
$env:PULSEGUARD_WORKER_UPDATER_URL = "http://pulseguard-worker-updater:8790"
docker compose -f docker-compose.worker.yml -f docker-compose.worker.build.yml up --build -d
docker update --restart unless-stopped pulseguard-worker pulseguard-worker-updater
docker logs --tail 80 pulseguard-worker`}</pre>
            <span>这条命令会从公司 Git 项目构建 worker 和 updater，后台启动容器并设置 Docker 自启策略。</span>
          </div>
          <div className="worker-help-block">
            <strong>管理平台推送更新（默认启用）</strong>
            <pre>{`$env:COMPOSE_PROJECT_NAME = "pulseguard-worker"
$env:COMPOSE_PROFILES = "updater"
$env:PULSEGUARD_WORKER_UPDATER_URL = "http://pulseguard-worker-updater:8790"
$env:PULSEGUARD_WORKER_UPDATE_IMAGE = "pulseguard-worker:local"
docker compose -f docker-compose.worker.yml -f docker-compose.worker.build.yml up --build -d
docker update --restart unless-stopped pulseguard-worker pulseguard-worker-updater`}</pre>
            <span>updater 会后台运行并跟随 Docker 自启；它会挂载宿主机 Docker socket，只允许更新当前 worker 服务，不支持任意命令。</span>
          </div>
          <div className="worker-help-block">
            <strong>内网镜像部署（可选）</strong>
            <pre>{`$env:PULSEGUARD_WORKER_IMAGE = "registry.example.com/pulseguard-worker:local"
$env:PULSEGUARD_WORKER_UPDATE_IMAGE = "registry.example.com/pulseguard-worker:local"
docker compose -f docker-compose.worker.yml up -d
docker update --restart unless-stopped pulseguard-worker
docker logs --tail 80 pulseguard-worker`}</pre>
            <span>子节点不能访问公司 Git 项目时，先在可访问环境构建并推送到内网镜像仓库，再替换镜像地址。</span>
          </div>
          <div className="worker-help-block">
            <strong>刷新子节点 token</strong>
            <pre>{`docker compose -f docker-compose.worker.yml exec pulseguard-worker python -m app.worker --rotate-token
docker compose -f docker-compose.worker.yml restart pulseguard-worker`}</pre>
          </div>
          <span>添加时地址通常是 <code>http://&lt;子节点 IP&gt;:8788</code>。保存后先测试连接，测试通过后再把任务绑定到该节点。</span>
        </Space>
      </Modal>
    </SettingsPanel>
  );
}

function ReadOnlyApiDocs({ onCopyPath }: { onCopyPath: (path: string) => void }) {
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

function EndpointTitle({ path, onCopy }: { path: string; onCopy: (path: string) => void }) {
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

function ConfigTransferPanel({
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

function ConfigImportPreviewResult({ preview }: { preview: ConfigImportPreview }) {
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

function ConfigImportIssueList({ title, tone, messages }: { title: string; tone: "error" | "warning"; messages: string[] }) {
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

function ProjectVariablesPanel({ projectName, readOnly }: { projectName: string; readOnly: boolean }) {
  const { message } = App.useApp();
  const [variables, setVariables] = useState<EnvironmentVariable[]>([]);
  const [saved, setSaved] = useState<EnvironmentVariable[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api.projectVariables().then((values) => {
      if (active) { setVariables(values); setSaved(values); }
    }).catch((err: Error) => { if (active) setError(err.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  const changed = JSON.stringify(variables) !== JSON.stringify(saved);
  const invalid = firstVariableBlockingError(variables);
  async function saveVariables() {
    setSaving(true);
    try {
      const values = await api.updateProjectVariables(variables.map(toPayloadEnvironmentVariable));
      setVariables(values); setSaved(values);
      message.success("项目变量已保存");
    } catch (err) { message.error((err as Error).message); }
    finally { setSaving(false); }
  }
  return <Card title={`${projectName} · 项目变量`} className="settings-panel-wide" loading={loading} extra={
    <Space>
      <Tooltip title="使用 ${变量名} 引用；本次运行的临时变量优先于项目变量，项目变量优先于全局变量。">
        <Button type="text" aria-label="变量引用说明" icon={<Info size={16} />} />
      </Tooltip>
      <Button disabled={readOnly || loading || saving || Boolean(error)} onClick={() => setVariables([...variables, createEnvironmentVariable()])}>添加变量</Button>
      <Button type="primary" loading={saving} disabled={readOnly || loading || Boolean(error) || !changed || Boolean(invalid)} onClick={saveVariables}>保存变量</Button>
    </Space>
  }>
    {error ? <Alert type="error" message={error} /> : <fieldset className="settings-readonly-fieldset" disabled={readOnly || saving}>
      {variables.length === 0 ? <Empty description="暂无项目变量" /> : <div className="environment-variable-list">
        {variables.map((variable, index) => <EnvironmentVariableEditor key={variable.id} variable={variable} variables={variables} index={index}
          onPatch={(patch) => setVariables((current) => current.map((item) => item.id === variable.id ? { ...item, ...patch } : item))}
          onRemove={() => setVariables((current) => current.filter((item) => item.id !== variable.id))}
        />)}
      </div>}
    </fieldset>}
  </Card>;
}

function EnvironmentVariableEditor({
  variable,
  variables,
  index,
  onPatch,
  onRemove
}: {
  variable: EnvironmentVariable;
  variables: EnvironmentVariable[];
  index: number;
  onPatch: (patch: Partial<EnvironmentVariable>) => void;
  onRemove: () => void;
}) {
  const nameError = environmentVariableNameError(variable, variables);
  const status = environmentVariableValueStatus(variable);
  const displayName = variable.name.trim() || `变量 ${index + 1}`;
  const canClearSavedSecret = variable.secret && Boolean(variable.value_set) && !variable.value_clear;

  return (
    <section className={`environment-variable-card ${status.color === "success" ? "environment-variable-ready" : ""}`}>
      <div className="environment-variable-header">
        <div className="environment-variable-title">
          <strong>{displayName}</strong>
          <span>{variable.name.trim() ? `\${${variable.name.trim()}}` : "等待填写变量名"}</span>
        </div>
        <Space size={8} wrap>
          <Tag color={variable.secret ? "processing" : "default"}>{variable.secret ? "密钥" : "明文"}</Tag>
          <Tag color={status.color}>{status.label}</Tag>
          <Popconfirm title="删除环境变量" description="删除后保存设置才会生效。" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={onRemove}>
            <Tooltip title="删除变量">
              <Button danger aria-label="删除环境变量" icon={<Trash2 size={15} />} />
            </Tooltip>
          </Popconfirm>
        </Space>
      </div>

      <Form layout="vertical" className="environment-variable-grid" autoComplete="off">
        <Form.Item label="变量名" validateStatus={nameError ? "error" : undefined} help={nameError || "使用 NAME 或 SERVICE_TOKEN 格式"}>
          <Input
            name={`environment-variable-name-${variable.id}`}
            value={variable.name}
            onChange={(event) => onPatch({ name: event.target.value })}
            placeholder="SERVICE_TOKEN"
            autoComplete="off"
            status={nameError ? "error" : undefined}
          />
        </Form.Item>
        <Form.Item label="密钥变量">
          <Switch
            aria-label={`${displayName} 是否作为密钥变量`}
            checked={variable.secret}
            onChange={(secret) => onPatch({ secret, value_clear: secret ? variable.value_clear : false })}
          />
        </Form.Item>
        <Form.Item
          label={
            <Space size={8}>
              <span>变量值</span>
              {variable.secret && variable.value_set && !variable.value && !variable.value_clear && <Tag color="success">已配置</Tag>}
              {variable.secret && variable.value && <Tag color="processing">{variable.value_set ? "待替换" : "待保存"}</Tag>}
              {variable.value_clear && <Tag color="warning">待清空</Tag>}
            </Space>
          }
          className="span-2"
          help={environmentVariableValueHelp(variable)}
        >
          <Space.Compact className="environment-variable-value-row">
            {variable.secret ? (
              <Input.Password
                name={`environment-variable-value-${variable.id}`}
                value={variable.value}
                onChange={(event) => onPatch({ value: event.target.value, value_clear: false })}
                placeholder={variable.value_set ? "输入新值可替换已保存密钥" : "输入变量值"}
                autoComplete="new-password"
                prefix={<KeyRound size={15} />}
              />
            ) : (
              <Input
                name={`environment-variable-value-${variable.id}`}
                value={variable.value}
                onChange={(event) => onPatch({ value: event.target.value, value_clear: false })}
                placeholder="变量值"
                autoComplete="off"
              />
            )}
            {canClearSavedSecret && (
              <Popconfirm
                title="清空已保存密钥"
                description="保存设置后，该变量的已保存密钥值会被清空。"
                okText="清空"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => onPatch({ value: "", value_clear: true })}
              >
                <Tooltip title="清空已保存密钥">
                  <Button danger aria-label="清空已保存密钥" icon={<Trash2 size={15} />} />
                </Tooltip>
              </Popconfirm>
            )}
            {variable.value_clear && (
              <Tooltip title="撤销清空密钥">
                <Button aria-label="撤销清空密钥" icon={<RotateCcw size={15} />} onClick={() => onPatch({ value_clear: false })} />
              </Tooltip>
            )}
          </Space.Compact>
        </Form.Item>
      </Form>
    </section>
  );
}

function AlertTagPolicyEditor({
  policy,
  index,
  channels,
  policies,
  onPatch,
  onRemove
}: {
  policy: AlertTagPolicy;
  index: number;
  channels: NotificationChannel[];
  policies: AlertTagPolicy[];
  onPatch: (patch: Partial<AlertTagPolicy>) => void;
  onRemove: () => void;
}) {
  const tagError = alertTagPolicyTagError(policy, policies);
  return (
    <section className={`notification-channel-card ${policy.enabled && !tagError ? "notification-channel-ready" : ""}`}>
      <div className="notification-channel-header">
        <div className="notification-channel-title">
          <strong>{policy.name.trim() || `标签策略 ${index + 1}`}</strong>
          <span>{policy.enabled ? (tagError || "匹配标签后覆盖全局告警策略") : "已停用"}</span>
        </div>
        <Space size={8}>
          <Tag color={policy.enabled && !tagError ? "success" : "default"}>{policy.enabled ? "启用" : "停用"}</Tag>
          <Switch aria-label="标签告警策略启用状态" checked={policy.enabled} onChange={(enabled) => onPatch({ enabled })} />
          <Popconfirm title="删除标签策略？" okText="删除" cancelText="取消" onConfirm={onRemove}>
            <Button icon={<Trash2 size={16} />} danger />
          </Popconfirm>
        </Space>
      </div>
      <Form layout="vertical" className="notification-channel-grid" autoComplete="off">
        <Form.Item label="策略名称">
          <Input value={policy.name} onChange={(event) => onPatch({ name: event.target.value })} autoComplete="off" />
        </Form.Item>
        <Form.Item label="匹配标签" required validateStatus={tagError ? "error" : undefined} help={tagError || undefined}>
          <Input value={policy.tag} onChange={(event) => onPatch({ tag: event.target.value })} autoComplete="off" />
        </Form.Item>
        <Form.Item label="通知渠道" className="span-2">
          <Select
            mode="multiple"
            value={policy.notification_channel_ids || []}
            onChange={(value) => onPatch({ notification_channel_ids: value })}
            options={channels.map((channel) => ({
              label: channel.name || channelTypeLabel(channel.type),
              value: channel.id,
              disabled: !channel.enabled
            }))}
          />
        </Form.Item>
        <NumberItem
          label="失败冷却时间"
          value={policy.alert_cooldown_minutes ?? 30}
          min={1}
          max={1440}
          suffix="分钟"
          onChange={(value) => onPatch({ alert_cooldown_minutes: value })}
        />
        <Form.Item label="恢复通知">
          <Switch
            aria-label="标签恢复通知"
            checked={policy.recovery_notification ?? true}
            onChange={(recovery_notification) => onPatch({ recovery_notification })}
          />
        </Form.Item>
      </Form>
    </section>
  );
}

function NotificationChannelEditor({
  channel,
  index,
  editing,
  testing,
  testDisabledReason,
  usesDraft,
  onPatch,
  onRemove,
  onEdit,
  onDone,
  onTest
}: {
  channel: NotificationChannel;
  index: number;
  editing: boolean;
  testing: boolean;
  testDisabledReason: string;
  usesDraft: boolean;
  onPatch: (patch: Partial<NotificationChannel>) => void;
  onRemove: () => void;
  onEdit: () => void;
  onDone: () => void;
  onTest: () => void;
}) {
  const guide = CHANNEL_GUIDES[channel.type];
  const urlError = channelUrlError(channel);
  const hostHint = channelHostHint(channel);
  const secretError = channelSecretError(channel);
  const ready = isReadyForSend(channel);
  const displayName = channelDisplayName(channel, index);

  function changeType(type: WebhookType) {
    onPatch({
      type,
      dingtalk_secret: "",
      dingtalk_secret_set: type === "dingtalk" ? channel.dingtalk_secret_set : false,
      dingtalk_secret_clear: type === "dingtalk" ? channel.dingtalk_secret_clear : false
    });
  }

  const menuItems: MenuProps["items"] = [
    ...(!editing ? [{ key: "edit", icon: <Pencil size={15} />, label: "编辑" }] : []),
    ...(!editing ? [{ type: "divider" as const }] : []),
    { key: "delete", icon: <Trash2 size={15} />, label: "删除", danger: true }
  ];

  const onMenuClick: MenuProps["onClick"] = ({ key }) => {
    if (key === "edit") {
      onEdit();
      return;
    }
    if (key === "delete") {
      Modal.confirm({
        title: "删除通知渠道",
        content: "删除后保存设置才会生效。",
        okText: "删除",
        cancelText: "取消",
        okButtonProps: { danger: true },
        onOk: onRemove
      });
    }
  };

  return (
    <section className={`notification-channel-card ${ready ? "notification-channel-ready" : ""} ${editing ? "notification-channel-editing" : "notification-channel-compact"}`}>
      <div className="notification-channel-header">
        <div className="notification-channel-title">
          <strong>{displayName}</strong>
        </div>
        <Space size={8} wrap className="notification-channel-actions">
          <Tag>{guide.label}</Tag>
          <span className="notification-channel-enable">
            <Switch aria-label={`${displayName} 渠道启用状态`} checked={channel.enabled} onChange={(value) => onPatch({ enabled: value })} />
          </span>
          <Tooltip title={testDisabledReason || (usesDraft ? "使用当前渠道草稿发送测试，不会保存设置" : "发送一条测试告警到当前渠道")}>
            <span className="tooltip-button-wrapper">
              <Button size="small" icon={<Send size={15} />} loading={testing} disabled={Boolean(testDisabledReason)} onClick={onTest}>
                发送测试
              </Button>
            </span>
          </Tooltip>
          {editing && (
            <Button size="small" icon={<CheckCircle2 size={15} />} onClick={onDone}>
              完成
            </Button>
          )}
          <Dropdown menu={{ items: menuItems, onClick: onMenuClick }} trigger={["click"]}>
            <Button size="small" aria-label={`${displayName} 更多操作`} icon={<MoreHorizontal size={15} />} />
          </Dropdown>
        </Space>
      </div>

      {editing ? (
        <Form layout="vertical" className="notification-channel-grid" autoComplete="off">
          <Form.Item label="渠道名称">
            <Input
              name={`notification-channel-name-${channel.id}`}
              value={channel.name}
              onChange={(event) => onPatch({ name: event.target.value })}
              placeholder="例如：值班群"
              autoComplete="off"
            />
          </Form.Item>
          <Form.Item label="渠道类型">
            <Select value={channel.type} onChange={(value) => changeType(value)} options={CHANNEL_OPTIONS} />
          </Form.Item>
          <Form.Item label="Webhook URL" className="span-2" validateStatus={urlError ? "error" : hostHint ? "warning" : undefined} help={urlError || hostHint || guide.webhookHint}>
            <Input.Password
              name={`notification-channel-webhook-${channel.id}`}
              value={channel.webhook_url}
              onChange={(event) => onPatch({ webhook_url: event.target.value })}
              placeholder="https://…"
              status={urlError ? "error" : hostHint ? "warning" : undefined}
              autoComplete="new-password"
            />
          </Form.Item>
          {channel.type === "dingtalk" && (
            <Form.Item
              label={
                <Space size={8}>
                  <span>钉钉加签密钥</span>
                  {channel.dingtalk_secret_set && !channel.dingtalk_secret_clear && !channel.dingtalk_secret && <Tag color="success">已配置</Tag>}
                  {channel.dingtalk_secret_clear && <Tag color="warning">待清除</Tag>}
                </Space>
              }
              className="span-2"
              validateStatus={secretError ? "warning" : undefined}
              help={secretError || guide.secretHint}
            >
              <Space.Compact className="secret-input-row">
                <Input.Password
                  name={`notification-channel-secret-${channel.id}`}
                  value={channel.dingtalk_secret || ""}
                  onChange={(event) => onPatch({ dingtalk_secret: event.target.value, dingtalk_secret_clear: false })}
                  autoComplete="new-password"
                  placeholder={channel.dingtalk_secret_set ? "输入新密钥可替换现有密钥" : "SEC 开头的加签密钥"}
                  prefix={<KeyRound size={15} />}
                />
                {channel.dingtalk_secret_set && !channel.dingtalk_secret_clear && (
                  <Popconfirm
                    title="清除钉钉加签密钥"
                    description="保存设置后，该渠道将不再携带钉钉签名参数。"
                    okText="清除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => onPatch({ dingtalk_secret: "", dingtalk_secret_set: false, dingtalk_secret_clear: true })}
                  >
                    <Tooltip title="清除已保存密钥">
                      <Button danger aria-label="清除已保存密钥" icon={<Trash2 size={15} />} />
                    </Tooltip>
                  </Popconfirm>
                )}
                {channel.dingtalk_secret_clear && (
                  <Tooltip title="撤销清除密钥">
                    <Button aria-label="撤销清除密钥" icon={<RotateCcw size={15} />} onClick={() => onPatch({ dingtalk_secret_clear: false, dingtalk_secret_set: true })} />
                  </Tooltip>
                )}
              </Space.Compact>
            </Form.Item>
          )}
        </Form>
      ) : null}
    </section>
  );
}

function AlertPreviewTool({
  compact = false,
  preview,
  loading,
  disabledReason,
  usesDraft,
  onPreview,
  onClosePreview
}: {
  compact?: boolean;
  preview: AlertPreview | null;
  loading: boolean;
  disabledReason: string;
  usesDraft: boolean;
  onPreview: () => void;
  onClosePreview?: () => void;
}) {
  const disabled = Boolean(disabledReason);
  const previewContent = preview && (
    <div className="alert-preview-body">
      {preview.channels.length === 0 ? (
        <Empty description="暂无可预检渠道" />
      ) : (
        <div className="alert-preview-channel-list">
          {preview.channels.map((channel) => (
            <PreviewChannel key={channel.id || `${channel.type}-${channel.name}`} channel={channel} />
          ))}
        </div>
      )}
      <StructuredViewer title="消息正文" value={preview.message_text} defaultMode="markdown" />
    </div>
  );

  if (compact) {
    return (
      <>
        <Tooltip title={disabledReason || (usesDraft ? "使用当前草稿生成，不保存、不发送" : "生成预检，不发送网络请求")}>
          <span className="tooltip-button-wrapper">
            <Button icon={<ShieldCheck size={16} />} loading={loading} disabled={disabled} onClick={onPreview}>
              预检渠道
            </Button>
          </span>
        </Tooltip>
        <Drawer title="发送预检" open={Boolean(preview)} size={780} onClose={onClosePreview}>
          {previewContent}
        </Drawer>
      </>
    );
  }

  return (
    <section className="alert-preview-tool">
      <div className="alert-preview-header">
        <div>
          <strong>本地预检</strong>
          <span>{usesDraft ? "使用当前页面草稿生成，不保存也不发送" : "基于已保存配置生成，不发送网络请求"}</span>
        </div>
        <Tooltip title={disabledReason || "生成脱敏目标、消息正文和 Webhook Payload"}>
          <span className="tooltip-button-wrapper">
            <Button icon={<ShieldCheck size={16} />} loading={loading} disabled={disabled} onClick={onPreview}>
              预检配置
            </Button>
          </span>
        </Tooltip>
      </div>

      {previewContent}
    </section>
  );
}

function PreviewChannel({ channel }: { channel: AlertPreviewChannel }) {
  return (
    <section className="alert-preview-channel">
      <div className="alert-preview-channel-header">
        <strong>{channel.name || channelTypeLabel(channel.type)}</strong>
        <Space size={6} wrap>
          <Tag>{channelTypeLabel(channel.type)}</Tag>
          <Tag color={enabledTagColor(channel.enabled)}>{channel.enabled ? "启用" : "停用"}</Tag>
        </Space>
      </div>
      <div className="alert-preview-facts">
        <PreviewFact label="目标 Origin" value={channel.target.origin || "-"} />
        <PreviewFact label="路径" value={channel.target.path || "-"} />
        <PreviewFact label="查询参数" value={channel.target.query_keys.length ? channel.target.query_keys.join(", ") : "-"} />
        <PreviewFact label="钉钉加签" value={channel.signing_enabled ? "发送时追加 timestamp/sign" : "不追加签名"} tone={channel.signing_enabled ? "ok" : "muted"} />
      </div>
      {channel.target.issues.length > 0 && <Alert className="compact-alert" type="warning" title={channel.target.issues.join("；")} showIcon />}
      <StructuredViewer title="Webhook Payload" value={channel.payload} defaultMode="json" />
    </section>
  );
}

function PreviewFact({ label, value, tone = "muted" }: { label: string; value: string; tone?: "ok" | "muted" }) {
  return (
    <div className={`meta-field meta-field-preview meta-field-preview-${tone}`}>
      <small>{label}</small>
      <span>{value}</span>
    </div>
  );
}

function SettingsPanel({
  title,
  icon,
  children,
  accent,
  className,
  extra
}: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
  accent?: boolean;
  className?: string;
  extra?: ReactNode;
}) {
  return (
    <Card
      className={["settings-panel", accent ? "settings-panel-accent" : "", className].filter(Boolean).join(" ")}
      title={<Space size={8}>{icon}<span>{title}</span></Space>}
      extra={extra}
    >
      {children}
    </Card>
  );
}

function SaveSettingsButton({
  loading,
  disabledReason,
  onClick
}: {
  loading: boolean;
  disabledReason: string;
  onClick: () => void;
}) {
  const disabled = Boolean(disabledReason);
  return (
    <Tooltip title={disabledReason || "保存当前页设置"}>
      <span className="tooltip-button-wrapper">
        <Button type="primary" icon={<Save size={16} />} onClick={onClick} loading={loading} disabled={disabled}>
          保存当前页
        </Button>
      </span>
    </Tooltip>
  );
}

function NumberItem({
  label,
  value,
  onChange,
  suffix,
  min,
  max
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  suffix?: string;
  min?: number;
  max?: number;
}) {
  const inputId = useId();
  return (
    <Form.Item label={label} htmlFor={inputId}>
      <InputNumber
        id={inputId}
        aria-label={label}
        min={min}
        max={max}
        value={value}
        suffix={suffix}
        onChange={(next) => onChange(Number(next || 0))}
      />
    </Form.Item>
  );
}

function triggerConfigDownload(file: ConfigExportFile) {
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

function isConfigBundle(value: unknown): value is ConfigBundle {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function configImportApplyDisabledReason(
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

function configImportPreviewAllowsApply(preview: ConfigImportPreview): boolean {
  return preview.valid !== false && preview.ok !== false && configImportIssueMessages(preview, "error").length === 0;
}

function configImportIssueMessages(preview: ConfigImportPreview, severity: "error" | "warning"): string[] {
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

function createChannel(type: WebhookType): NotificationChannel {
  return {
    id: `channel-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: "",
    type,
    enabled: true,
    webhook_url: "",
    dingtalk_secret: ""
  };
}

function createAlertTagPolicy(settings: SettingsValues): AlertTagPolicy {
  return {
    id: `tag-policy-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: "",
    tag: "",
    enabled: true,
    alert_cooldown_minutes: settings.alert_cooldown_minutes,
    recovery_notification: settings.recovery_notification,
    notification_channel_ids: settings.notification_channels.filter((channel) => channel.enabled).map((channel) => channel.id)
  };
}

function createEnvironmentVariable(): EnvironmentVariable {
  return {
    id: `variable-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: "",
    value: "",
    secret: false,
    value_set: false,
    value_clear: false
  };
}

function toPayloadAlertTagPolicy(policy: AlertTagPolicy): AlertTagPolicy {
  return {
    id: policy.id,
    name: policy.name.trim(),
    tag: policy.tag.trim(),
    enabled: policy.enabled,
    alert_cooldown_minutes: Math.max(1, Math.min(1440, Number(policy.alert_cooldown_minutes || 1))),
    recovery_notification: Boolean(policy.recovery_notification),
    notification_channel_ids: Array.from(new Set((policy.notification_channel_ids || []).map((id) => id.trim()).filter(Boolean)))
  };
}

function toPayloadChannel(channel: NotificationChannel): NotificationChannel {
  const payload: NotificationChannel = {
    id: channel.id,
    name: channel.name.trim(),
    type: channel.type,
    enabled: channel.enabled,
    webhook_url: channel.webhook_url.trim()
  };
  if (channel.type === "dingtalk") {
    payload.dingtalk_secret = (channel.dingtalk_secret || "").trim();
    if (channel.dingtalk_secret_clear) {
      payload.dingtalk_secret_clear = true;
    }
  }
  return payload;
}

function toPayloadEnvironmentVariable(variable: EnvironmentVariable): EnvironmentVariable {
  const payload: EnvironmentVariable = {
    id: variable.id,
    name: variable.name.trim(),
    value: variable.value.trim(),
    secret: variable.secret
  };
  if (variable.secret && variable.value_clear) {
    payload.value_clear = true;
  }
  return payload;
}

function uniqueIds(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function firstBlockingError(channels: NotificationChannel[]): string {
  for (const channel of channels) {
    const error = channelUrlError(channel) || channelSecretError(channel);
    if (error) return error;
  }
  return "";
}

function firstAlertTagPolicyError(policies: AlertTagPolicy[]): string {
  for (const policy of policies) {
    const error = alertTagPolicyTagError(policy, policies);
    if (error) return error;
  }
  return "";
}

function alertTagPolicyTagError(policy: AlertTagPolicy, policies: AlertTagPolicy[]): string {
  const tag = policy.tag.trim();
  if (!tag) return "标签告警策略标签不能为空";
  if (tag.includes(",")) return "标签告警策略标签不能包含逗号";
  const duplicate = policies.some((candidate) => candidate.id !== policy.id && candidate.tag.trim().toLowerCase() === tag.toLowerCase());
  if (duplicate) return "标签告警策略标签不能重复";
  return "";
}

function firstVariableBlockingError(variables: EnvironmentVariable[]): string {
  for (const variable of variables) {
    const error = environmentVariableNameError(variable, variables);
    if (error) return error;
  }
  return "";
}

function environmentVariableNameError(variable: EnvironmentVariable, variables: EnvironmentVariable[]): string {
  const name = variable.name.trim();
  if (!name) return "变量名称不能为空";
  if (!VARIABLE_NAME_PATTERN.test(name)) return "变量名称必须使用 NAME 或 SERVICE_TOKEN 格式";
  const duplicate = variables.some((candidate) => candidate.id !== variable.id && candidate.name.trim() === name);
  if (duplicate) return "变量名称不能重复";
  return "";
}

function environmentVariableValueStatus(variable: EnvironmentVariable): { label: string; color: "default" | "success" | "warning" | "processing" } {
  if (variable.value_clear) return { label: "待清空", color: "warning" };
  if (variable.secret && variable.value.trim()) {
    return { label: variable.value_set ? "待替换" : "待保存", color: "processing" };
  }
  if (variable.secret && variable.value_set) return { label: "已配置", color: "success" };
  if (variable.value.trim()) return { label: "已配置", color: "success" };
  return { label: "未配置", color: "default" };
}

function environmentVariableValueHelp(variable: EnvironmentVariable): string {
  if (variable.secret) {
    if (variable.value_clear) return "保存后清空已保存密钥";
    if (variable.value_set && !variable.value) return "留空会保留已保存密钥，输入新值会替换";
    return "保存后不会在设置页回显";
  }
  if (variable.value_set && !variable.value) return "切换为明文后需要重新输入值，否则保存为空";
  const placeholder = variable.name.trim() || "NAME";
  return `使用 \${${placeholder}} 引用该变量`;
}

function isReadyForSend(channel: NotificationChannel): boolean {
  return channel.enabled && Boolean(channel.webhook_url.trim()) && !channelUrlError(channel) && !channelSecretError(channel);
}

function channelDisplayName(channel: NotificationChannel, index?: number): string {
  const name = channel.name.trim();
  if (name) return name;
  const suffix = typeof index === "number" ? ` ${index + 1}` : "";
  return `${channelTypeLabel(channel.type)}渠道${suffix}`;
}

function channelTestDisabledReason(channel: NotificationChannel): string {
  if (!channel.enabled) return "启用渠道后再发送测试";
  if (!channel.webhook_url.trim()) return "填写 Webhook URL 后再发送测试";
  return channelUrlError(channel) || channelSecretError(channel);
}

function channelUrlError(channel: NotificationChannel): string {
  const value = channel.webhook_url.trim();
  if (!value) return "";
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return "Webhook URL 需要是完整的 http:// 或 https:// 地址";
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return "Webhook URL 仅支持 http:// 或 https://";
  }
  return "";
}

function channelHostHint(channel: NotificationChannel): string {
  const value = channel.webhook_url.trim();
  if (!value || channelUrlError(channel)) return "";
  const parsed = new URL(value);
  if (isLocalWebhookHost(parsed.hostname)) return "";
  const guide = CHANNEL_GUIDES[channel.type];
  if (!parsed.hostname.endsWith(guide.expectedHost)) {
    return `当前 URL 不像${guide.label}官方机器人地址；如果通过内部网关转发，可以忽略此提示`;
  }
  return "";
}

function channelSecretError(channel: NotificationChannel): string {
  if (channel.type !== "dingtalk") return "";
  const secret = (channel.dingtalk_secret || "").trim();
  if (secret && !secret.startsWith("SEC")) {
    return "钉钉加签密钥应以 SEC 开头";
  }
  return "";
}

function isLocalWebhookHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

function channelTypeLabel(type: WebhookType): string {
  return CHANNEL_GUIDES[type]?.label || type;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function normalizeBrowserTypes(value?: string[] | null, allowEmpty = false): BrowserType[] {
  const seen = new Set<string>();
  const result: BrowserType[] = [];
  for (const item of value || []) {
    const browserType = String(item || "").trim() as BrowserType;
    if (!BROWSER_TYPES.includes(browserType) || seen.has(browserType)) continue;
    seen.add(browserType);
    result.push(browserType);
  }
  if (result.length) return result;
  return allowEmpty ? [] : ["chromium"];
}

function normalizeBrowserPoolSizes(value?: Partial<Record<BrowserType, number>> | null): Record<BrowserType, number> {
  const source = value || {};
  return BROWSER_TYPES.reduce<Record<BrowserType, number>>((result, browserType) => {
    const raw = Number(source[browserType] || 5);
    result[browserType] = Math.max(1, Math.min(5, Number.isFinite(raw) ? Math.round(raw) : 5));
    return result;
  }, { chromium: 5, firefox: 5, webkit: 5 });
}

function settingsChanged(current: SettingsValues, saved: SettingsValues): boolean {
  return JSON.stringify(comparableSettings(current)) !== JSON.stringify(comparableSettings(saved));
}

function settingsTabChanged(current: SettingsValues, saved: SettingsValues, tab: SettingsTab): boolean {
  return JSON.stringify(comparableSettings(current, tab)) !== JSON.stringify(comparableSettings(saved, tab));
}

function comparableSettings(values: SettingsValues, tab?: SettingsTab) {
  const alerts = {
    alerts_enabled: values.alerts_enabled,
    alert_cooldown_minutes: values.alert_cooldown_minutes,
    recovery_notification: values.recovery_notification,
    execution_notification_channel_ids: values.execution_notification_channel_ids || [],
    alert_tag_policies: (values.alert_tag_policies || []).map((policy) => ({
      id: policy.id,
      name: policy.name,
      tag: policy.tag,
      enabled: Boolean(policy.enabled),
      alert_cooldown_minutes: policy.alert_cooldown_minutes ?? null,
      recovery_notification: policy.recovery_notification ?? null,
      notification_channel_ids: policy.notification_channel_ids || []
    })),
    notification_channels: values.notification_channels.map((channel) => ({
      id: channel.id,
      name: channel.name,
      type: channel.type,
      enabled: channel.enabled,
      webhook_url: channel.webhook_url,
      dingtalk_secret: channel.dingtalk_secret || "",
      dingtalk_secret_set: Boolean(channel.dingtalk_secret_set),
      dingtalk_secret_clear: Boolean(channel.dingtalk_secret_clear)
    }))
  };
  const systemAlerts = {
    system_alerts_enabled: values.system_alerts_enabled,
    alert_detail_base_url: values.alert_detail_base_url,
    alert_delivery_attempts: values.alert_delivery_attempts,
    system_notification_channel_ids: values.system_notification_channel_ids || [],
    system_notification_channels: (values.system_notification_channels || []).map((channel) => ({
      id: channel.id,
      name: channel.name,
      type: channel.type,
      enabled: channel.enabled,
      webhook_url: channel.webhook_url,
      dingtalk_secret: channel.dingtalk_secret || "",
      dingtalk_secret_set: Boolean(channel.dingtalk_secret_set),
      dingtalk_secret_clear: Boolean(channel.dingtalk_secret_clear)
    }))
  };
  const runtime = {
    default_interval_seconds: values.default_interval_seconds,
    default_ui_timeout_ms: values.default_ui_timeout_ms,
    default_api_timeout_ms: values.default_api_timeout_ms,
    max_concurrency: values.max_concurrency,
    max_ui_concurrency: values.max_ui_concurrency,
    max_queue_size: values.max_queue_size,
    max_task_runtime_seconds: values.max_task_runtime_seconds,
    api_failure_confirmation_count: values.api_failure_confirmation_count,
    ui_failure_confirmation_count: values.ui_failure_confirmation_count,
    recovery_confirmation_count: values.recovery_confirmation_count,
    api_retry_attempts: values.api_retry_attempts,
    ui_retry_attempts: values.ui_retry_attempts,
    stale_after_intervals: values.stale_after_intervals
  };
  const browser = {
    browser_headless: values.browser_headless,
    browser_type: values.browser_type,
    enabled_browser_types: normalizeBrowserTypes(values.enabled_browser_types),
    prewarmed_browser_types: normalizeBrowserTypes(values.prewarmed_browser_types, true),
    browser_pool_sizes: normalizeBrowserPoolSizes(values.browser_pool_sizes),
    browser_recycle_after_runs: values.browser_recycle_after_runs,
    browser_proxy: values.browser_proxy,
    browser_viewport: values.browser_viewport
  };
  const variables = {
    environment_variables: values.environment_variables.map((variable) => ({
      id: variable.id,
      name: variable.name,
      value: variable.value || "",
      secret: Boolean(variable.secret),
      value_set: Boolean(variable.value_set),
      value_clear: Boolean(variable.value_clear)
    }))
  };
  const retention = {
    run_retention_days: values.run_retention_days,
    screenshot_retention_days: values.screenshot_retention_days,
    trace_retention_days: values.trace_retention_days,
    response_retention_days: values.response_retention_days,
    similar_failure_retention_count: values.similar_failure_retention_count,
    trace_artifacts_enabled: values.trace_artifacts_enabled,
    success_response_artifacts_enabled: values.success_response_artifacts_enabled,
    database_backup_retention: values.database_backup_retention
  };
  const access = {
    read_only_token_set: Boolean(values.read_only_token_set),
    read_only_tokens: (values.read_only_tokens || []).map((token) => ({
      id: token.id,
      name: token.name || "",
      created_at: token.created_at || "",
      project_ids: token.project_ids || []
    }))
  };
  const maintenance = {
    maintenance_enabled: Boolean(values.maintenance_enabled),
    maintenance_title: values.maintenance_title || "",
    maintenance_message: values.maintenance_message || "",
    maintenance_starts_at: values.maintenance_starts_at || "",
    maintenance_ends_at: values.maintenance_ends_at || ""
  };
  const execution = { runtime, browser };
  const system = { retention, access, maintenance };
  if (tab === "alerts") return alerts;
  if (tab === "system-alerts") return systemAlerts;
  if (tab === "execution") return execution;
  if (tab === "variables") return variables;
  if (tab === "system") return system;
  return { alerts, systemAlerts, execution, variables, system };
}

function cloneSettings(values: SettingsValues): SettingsValues {
  const cloned = JSON.parse(JSON.stringify(values)) as SettingsValues;
  return {
    ...cloned,
    read_only_token: "",
    read_only_token_set: Boolean(cloned.read_only_token_set),
    read_only_tokens: cloned.read_only_tokens || [],
    local_runner_name: cloned.local_runner_name || "local",
    local_runner_address: cloned.local_runner_address || "127.0.0.1",
    local_runner_region: cloned.local_runner_region || "local",
    api_failure_confirmation_count: cloned.api_failure_confirmation_count ?? 2,
    ui_failure_confirmation_count: cloned.ui_failure_confirmation_count ?? 3,
    recovery_confirmation_count: cloned.recovery_confirmation_count ?? 2,
    api_retry_attempts: cloned.api_retry_attempts ?? 1,
    ui_retry_attempts: cloned.ui_retry_attempts ?? 1,
    maintenance_enabled: Boolean(cloned.maintenance_enabled),
    maintenance_title: cloned.maintenance_title || "",
    maintenance_message: cloned.maintenance_message || "",
    maintenance_starts_at: cloned.maintenance_starts_at || "",
    maintenance_ends_at: cloned.maintenance_ends_at || "",
    notification_channels: cloned.notification_channels || [],
    system_notification_channels: cloned.system_notification_channels || [],
    execution_notification_channel_ids: cloned.execution_notification_channel_ids || [],
    system_notification_channel_ids: cloned.system_notification_channel_ids || [],
    system_alerts_enabled: cloned.system_alerts_enabled ?? true,
    members: cloned.members || [],
    alert_tag_policies: (cloned.alert_tag_policies || []).map((policy) => ({
      id: policy.id,
      name: policy.name || "",
      tag: policy.tag || "",
      enabled: Boolean(policy.enabled),
      alert_cooldown_minutes: policy.alert_cooldown_minutes,
      recovery_notification: policy.recovery_notification,
      notification_channel_ids: policy.notification_channel_ids || []
    })),
    environment_variables: (cloned.environment_variables || []).map((variable) => ({
      id: variable.id,
      name: variable.name || "",
      value: variable.value || "",
      secret: Boolean(variable.secret),
      value_set: Boolean(variable.value_set),
      value_clear: Boolean(variable.value_clear)
    }))
  };
}
