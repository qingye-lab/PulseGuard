import type { BrowserType, ConfigImportSummary } from "../../types";

// Shared settings vocabulary. These are referenced by the page shell and by
// several extracted panels, so they live in a leaf module with no local imports
// to keep the dependency direction one-way.

export type SettingsTab = "alerts" | "system-alerts" | "execution" | "variables" | "system";


export const SETTINGS_TAB_LABELS: Record<SettingsTab, string> = {
  alerts: "项目告警",
  "system-alerts": "系统告警",
  execution: "执行配置",
  variables: "变量管理",
  system: "系统配置"
};

export const SETTINGS_TAB_ORDER: SettingsTab[] = ["alerts", "system-alerts", "execution", "variables", "system"];


export const BROWSER_TYPES: BrowserType[] = ["chromium", "firefox", "webkit"];


export const VARIABLE_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;


export const CONFIG_SUMMARY_ITEMS: Array<{ key: keyof ConfigImportSummary; label: string }> = [
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
