import type { BrowserType, SettingsValues } from "../../types";
import { SettingsTab, BROWSER_TYPES } from "./settingsTypes";

export function cloneSettings(values: SettingsValues): SettingsValues {
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

export function comparableSettings(values: SettingsValues, tab?: SettingsTab) {
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

export function settingsChanged(current: SettingsValues, saved: SettingsValues): boolean {
  return JSON.stringify(comparableSettings(current)) !== JSON.stringify(comparableSettings(saved));
}

export function settingsTabChanged(current: SettingsValues, saved: SettingsValues, tab: SettingsTab): boolean {
  return JSON.stringify(comparableSettings(current, tab)) !== JSON.stringify(comparableSettings(saved, tab));
}

export function normalizeBrowserTypes(value?: string[] | null, allowEmpty = false): BrowserType[] {
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

export function normalizeBrowserPoolSizes(value?: Partial<Record<BrowserType, number>> | null): Record<BrowserType, number> {
  const source = value || {};
  return BROWSER_TYPES.reduce<Record<BrowserType, number>>((result, browserType) => {
    const raw = Number(source[browserType] || 5);
    result[browserType] = Math.max(1, Math.min(5, Number.isFinite(raw) ? Math.round(raw) : 5));
    return result;
  }, { chromium: 5, firefox: 5, webkit: 5 });
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
