import type {
  AlertPreview,
  ApiInspectPayload,
  ApiInspectResult,
  AuditEvent,
  Check,
  CheckBatchPayload,
  CheckBatchResult,
  CheckVersion,
  CheckTrend,
  CheckPayload,
  CheckType,
  ConfigBundle,
  ConfigExportFile,
  ConfigImportPreview,
  ConfigImportResult,
  DatabaseBackup,
  DatabaseHealth,
  EnvironmentVariable,
  MonitoringTrends,
  NotificationStatus,
  Overview,
  ProbeRunner,
  ProbeRunnerPayload,
  RelayStatus,
  RunnerProvisionPayload,
  RunnerProvisionResult,
  RunnerUpdateRequest,
  RunnerUpdateStatus,
  CreatedReadOnlyToken,
  Run,
  RunArchive,
  RunPage,
  RunComparison,
  RunFailureSummary,
  RunStatus,
  RuntimeStatus,
  SettingsValues,
  Project,
  ProjectAlertSettings,
  StatusPageSnapshot,
  TrendPeriod,
  UiInspectPayload,
  UiInspectResult,
  UiInspectionDiagnostics,
  UiRuleInspectPayload,
  UiRuleInspectResult
} from "./types";

export const PROJECT_STORAGE_KEY = "pulseguard.current-project-id";

let activeProjectId: number | undefined;

export function setActiveProjectId(projectId: number | null | undefined): void {
  activeProjectId = Number.isInteger(projectId) && Number(projectId) > 0 ? Number(projectId) : undefined;
}

function projectHeader(): Record<string, string> {
  return activeProjectId ? { "X-PulseGuard-Project-Id": String(activeProjectId) } : {};
}

const PROJECT_ALERT_KEYS = new Set([
  "alerts_enabled",
  "notification_channels",
  "execution_notification_channel_ids",
  "alert_tag_policies",
  "alert_cooldown_minutes",
  "recovery_notification"
]);

function projectAlertValues(values: Partial<SettingsValues>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(values).filter(([key]) => PROJECT_ALERT_KEYS.has(key)));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...projectHeader(), ...(init?.headers || {}) }
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return response.json() as Promise<T>;
}

async function downloadFile(path: string, init?: RequestInit): Promise<ConfigExportFile> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...projectHeader(), ...(init?.headers || {}) }
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return {
    blob: await response.blob(),
    filename: downloadFilename(response.headers.get("content-disposition")) || "pulseguard-config.json"
  };
}

export class ApiError extends Error {
  constructor(message: string, public readonly diagnostics?: UiInspectionDiagnostics) {
    super(message);
    this.name = "ApiError";
  }
}

async function responseError(response: Response): Promise<Error> {
  let message = `请求失败：${response.status}`;
  let diagnostics: UiInspectionDiagnostics | undefined;
  try {
    const data = await response.json();
    message = formatErrorDetail(data.detail, message);
    if (data.diagnostics && typeof data.diagnostics.stage === "string" && data.diagnostics.status === "failed") {
      diagnostics = data.diagnostics;
    }
  } catch {
    // keep default message
  }
  return new ApiError(message, diagnostics);
}

function downloadFilename(contentDisposition: string | null): string {
  if (!contentDisposition) return "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(contentDisposition);
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1].replace(/^"|"$/g, ""));
    } catch {
      return encoded[1].replace(/^"|"$/g, "");
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(contentDisposition);
  return plain?.[1]?.trim() || "";
}

const FIELD_LABELS: Record<string, string> = {
  name: "任务名称",
  action: "批量操作",
  type: "任务类型",
  enabled: "启用状态",
  interval_seconds: "执行频率",
  timeout_ms: "超时时间",
  entry_url: "目标 URL",
  viewport_mode: "页面模式",
  method: "Method",
  headers_json: "Headers",
  body: "Body",
  assertions_json: "校验项",
  setup_script: "前置脚本",
  script: "Python 脚本",
  tag: "标签",
  tags: "标签",
  expected_count: "命中数量",
  alert_policy_json: "任务告警策略",
  notification_channels: "通知渠道",
  members: "成员",
  member_ids: "关联成员",
  alert_tag_policies: "标签告警策略",
  alert_policy_tag: "标签告警策略标签",
  alert_cooldown_minutes: "告警冷却时间",
  alert_detail_base_url: "告警详情链接前缀",
  max_queue_size: "执行队列容量",
  max_ui_concurrency: "最大 UI 并发数",
  browser_viewport: "浏览器 Viewport",
  local_runner_name: "本机节点名称",
  local_runner_address: "本机节点地址",
  local_runner_region: "本机节点网络区域",
  maintenance_enabled: "维护公告启用状态",
  maintenance_title: "维护公告标题",
  maintenance_message: "维护公告内容",
  maintenance_starts_at: "维护开始时间",
  maintenance_ends_at: "维护结束时间"
};

function formatErrorDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return safeErrorText(detail, fallback);
  if (Array.isArray(detail)) {
    const messages = detail.map(formatValidationItem).filter(Boolean);
    return messages.length ? Array.from(new Set(messages)).join("；") : fallback;
  }
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return safeErrorText(message, fallback);
  }
  return fallback;
}

function safeErrorText(value: string, fallback: string): string {
  const text = value.trim();
  if (!text) return fallback;
  if (/(traceback|exception|stack|authorization|cookie|set-cookie|headers_json|read_only_token|token|secret|password|webhook)/i.test(text)) {
    return fallback;
  }
  if (/[\r\n{}[\]<>]/.test(text)) return fallback;
  return text.length > 240 ? `${text.slice(0, 240)}...` : text;
}

function formatValidationItem(item: unknown): string {
  if (!item || typeof item !== "object") return "";
  const error = item as { loc?: unknown; msg?: unknown; type?: unknown };
  const label = fieldLabel(error.loc);
  const text = validationText(error);
  if (label && text.startsWith(label)) return text;
  if (label && /^(不能为空|过长|过短|必须|不能|格式|低于|超过|选项)/.test(text)) return `${label}${text}`;
  return label ? `${label}：${text}` : text;
}

function fieldLabel(loc: unknown): string {
  if (!Array.isArray(loc)) return "";
  const keys = loc.map(String).filter((item) => !["body", "query", "path"].includes(item) && !/^\d+$/.test(item));
  const key = keys[keys.length - 1];
  return key ? FIELD_LABELS[key] || "" : "";
}

function validationText(error: { msg?: unknown; type?: unknown }): string {
  const type = String(error.type || "");
  const message = typeof error.msg === "string" ? error.msg.replace(/^Value error,\s*/, "").trim() : "";
  if (type === "missing" || type === "string_too_short" || type === "too_short") return "不能为空";
  if (type === "string_too_long" || type === "too_long") return "过长";
  if (["int_parsing", "int_type", "float_parsing", "float_type"].includes(type)) return "必须是数字";
  if (["bool_type", "bool_parsing"].includes(type)) return "必须是布尔值";
  if (["literal_error", "enum"].includes(type)) return "选项无效";
  if (["greater_than_equal", "greater_than"].includes(type)) return "低于允许范围";
  if (["less_than_equal", "less_than"].includes(type)) return "超过允许范围";
  return message || "格式不正确";
}

export const api = {
  projects: () => request<Project[]>("/api/projects"),
  createProject: (name: string) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify({ name }) }),
  renameProject: (id: number, name: string) =>
    request<Project>(`/api/projects/${id}`, { method: "PUT", body: JSON.stringify({ name }) }),
  archiveProject: (id: number) => request<Project>(`/api/projects/${id}/archive`, { method: "POST" }),
  restoreProject: (id: number) => request<Project>(`/api/projects/${id}/restore`, { method: "POST" }),
  deleteProject: (id: number) => request<{ ok: boolean }>(`/api/projects/${id}`, { method: "DELETE" }),
  overview: () => request<Overview>("/api/overview"),
  runtime: () => request<RuntimeStatus>("/api/runtime"),
  statusPage: () => request<StatusPageSnapshot>("/api/status-page"),
  checks: (type: CheckType) => request<Check[]>(`/api/checks?type=${type}`),
  check: (id: number) => request<Check>(`/api/checks/${id}`),
  createCheck: (payload: CheckPayload) =>
    request<Check>("/api/checks", { method: "POST", body: JSON.stringify(payload) }),
  updateCheck: (id: number, payload: CheckPayload) =>
    request<Check>(`/api/checks/${id}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteCheck: (id: number) => request<{ ok: boolean }>(`/api/checks/${id}`, { method: "DELETE" }),
  enableCheck: (id: number) => request<Check>(`/api/checks/${id}/enable`, { method: "POST" }),
  disableCheck: (id: number) => request<Check>(`/api/checks/${id}/disable`, { method: "POST" }),
  runCheck: (id: number) => request<Run>(`/api/checks/${id}/run`, { method: "POST" }),
  confirmRecovery: (id: number) => request<Run>(`/api/checks/${id}/confirm-recovery`, { method: "POST" }),
  debugCheck: (payload: CheckPayload) =>
    request<Run>("/api/checks/debug", { method: "POST", body: JSON.stringify(payload) }),
  inspectApi: (payload: ApiInspectPayload) =>
    request<ApiInspectResult>("/api/checks/inspect-api", { method: "POST", body: JSON.stringify(payload) }),
  inspectUi: (payload: UiInspectPayload) =>
    request<UiInspectResult>("/api/checks/inspect-ui", { method: "POST", body: JSON.stringify(payload) }),
  inspectUiRules: (payload: UiRuleInspectPayload) =>
    request<UiRuleInspectResult>("/api/checks/inspect-ui-rules", { method: "POST", body: JSON.stringify(payload) }),
  batchChecks: (payload: CheckBatchPayload) =>
    request<CheckBatchResult>("/api/checks/batch", { method: "POST", body: JSON.stringify(payload) }),
  auditEvents: () => request<AuditEvent[]>("/api/audit-events"),
  runArchives: () => request<RunArchive[]>("/api/run-archives"),
  monitoringTrends: (params: {
    period: TrendPeriod;
    start?: string;
    end?: string;
    type?: CheckType | "";
    q?: string;
    page: number;
    page_size: number;
    hour_start?: string;
    hour_end?: string;
  }) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== "" && value !== null && value !== undefined) query.set(key, String(value));
    });
    return request<MonitoringTrends>(`/api/monitoring-trends?${query.toString()}`);
  },
  runners: () => request<ProbeRunner[]>("/api/runners"),
  relayStatus: () => request<RelayStatus>("/api/relay/status"),
  createRunner: (payload: ProbeRunnerPayload) =>
    request<ProbeRunner>("/api/runners", { method: "POST", body: JSON.stringify(payload) }),
  provisionRunner: (payload: RunnerProvisionPayload) =>
    request<RunnerProvisionResult>("/api/runners/provision", { method: "POST", body: JSON.stringify(payload) }),
  regenerateRunnerProvision: (runnerId: string, payload: RunnerProvisionPayload) =>
    request<RunnerProvisionResult>(`/api/runners/${encodeURIComponent(runnerId)}/provision/regenerate`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateRunner: (runnerId: string, payload: Partial<ProbeRunnerPayload>) =>
    request<ProbeRunner>(`/api/runners/${encodeURIComponent(runnerId)}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteRunner: (runnerId: string) =>
    request<{ ok: boolean }>(`/api/runners/${encodeURIComponent(runnerId)}`, { method: "DELETE" }),
  rotateRunnerToken: (runnerId: string) =>
    request<ProbeRunner>(`/api/runners/${encodeURIComponent(runnerId)}/rotate-token`, { method: "POST" }),
  testRunner: (runnerId: string) =>
    request<{ ok: boolean; message: string; runner?: ProbeRunner; worker?: unknown }>(
      `/api/runners/${encodeURIComponent(runnerId)}/test`,
      { method: "POST" }
    ),
  updateRunnerNode: (runnerId: string, payload: RunnerUpdateRequest = {}) =>
    request<{ ok: boolean; message: string; worker?: { update?: RunnerUpdateStatus } }>(`/api/runners/${encodeURIComponent(runnerId)}/update`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  runnerUpdateStatus: (runnerId: string) =>
    request<{ ok: boolean; worker?: { update?: RunnerUpdateStatus } }>(`/api/runners/${encodeURIComponent(runnerId)}/update-status`),
  checkVersions: (id: number) => request<CheckVersion[]>(`/api/checks/${id}/versions`),
  checkTrend: (id: number, params: { period: TrendPeriod; start?: string; end?: string; hour_start?: string; hour_end?: string }) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value) query.set(key, String(value));
    });
    return request<CheckTrend>(`/api/checks/${id}/trend?${query.toString()}`);
  },
  restoreCheckVersion: (versionId: number) =>
    request<Check>(`/api/check-versions/${versionId}/restore`, { method: "POST" }),
  runs: (params: {
    type?: CheckType | "";
    status?: RunStatus | "failed" | "";
    notification_status?: NotificationStatus | "";
    q?: string;
    check_id?: string | null;
    runner_id?: string | null;
    run_group_id?: string | null;
    start?: string;
    end?: string;
    limit?: number;
  }) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value) query.set(key, String(value));
    });
    return request<Run[]>(`/api/runs?${query.toString()}`);
  },
  runsPage: (params: {
    type?: CheckType | "";
    status?: RunStatus | "failed" | "";
    notification_status?: NotificationStatus | "";
    observation_kind?: "observation" | "verification" | "draft" | "";
    q?: string;
    check_id?: string | null;
    runner_id?: string | null;
    run_group_id?: string | null;
    start?: string;
    end?: string;
    page: number;
    page_size: number;
  }) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== "" && value !== null && value !== undefined) query.set(key, String(value));
    });
    return request<RunPage>(`/api/runs-page?${query.toString()}`);
  },
  run: (id: number) => request<Run>(`/api/runs/${id}`),
  runComparison: (id: number) => request<RunComparison>(`/api/runs/${id}/compare-success`),
  runFailureSummary: (id: number) => request<RunFailureSummary>(`/api/runs/${id}/failure-summary`),
  rerun: (id: number) => request<Run>(`/api/runs/${id}/rerun`, { method: "POST" }),
  settings: async () => {
    const [globalSettings, projectAlerts] = await Promise.all([
      request<SettingsValues>("/api/settings"),
      request<ProjectAlertSettings>("/api/project-alert-settings")
    ]);
    return { ...globalSettings, ...projectAlerts } as SettingsValues;
  },
  projectAlertSettings: () => request<ProjectAlertSettings>("/api/project-alert-settings"),
  projectVariables: () => request<EnvironmentVariable[]>("/api/project-variables"),
  updateProjectVariables: (values: EnvironmentVariable[]) => request<EnvironmentVariable[]>("/api/project-variables", {
    method: "PUT", body: JSON.stringify(values)
  }),
  updateSettings: async (values: Partial<SettingsValues>) => {
    const projectValues: Record<string, unknown> = {};
    const globalValues: Record<string, unknown> = {};
    Object.entries(values).forEach(([key, value]) => {
      (PROJECT_ALERT_KEYS.has(key) ? projectValues : globalValues)[key] = value;
    });
    await Promise.all([
      Object.keys(globalValues).length
        ? request<SettingsValues>("/api/settings", { method: "PUT", body: JSON.stringify({ values: globalValues }) })
        : Promise.resolve(),
      Object.keys(projectValues).length
        ? request<ProjectAlertSettings>("/api/project-alert-settings", {
            method: "PUT",
            body: JSON.stringify({ values: projectValues })
          })
        : Promise.resolve()
    ]);
    return api.settings();
  },
  createReadOnlyToken: (name: string, projectIds?: number[]) => {
    const scope = projectIds?.length ? projectIds : activeProjectId ? [activeProjectId] : [];
    return request<CreatedReadOnlyToken>("/api/read-only-tokens", {
      method: "POST",
      body: JSON.stringify({ read_only_token_name: name, project_ids: scope })
    });
  },
  deleteReadOnlyToken: (id: string) =>
    request<SettingsValues>(`/api/read-only-tokens/${encodeURIComponent(id)}`, { method: "DELETE" }),
  alertPreview: (values: Partial<SettingsValues>) =>
    request<AlertPreview>("/api/settings/alert-preview", {
      method: "POST",
      body: JSON.stringify({ values: projectAlertValues(values) })
    }),
  testAlert: (values: Partial<SettingsValues>) =>
    request<{ ok: boolean; message: string }>("/api/settings/test-alert", {
      method: "POST",
      body: JSON.stringify({ values: projectAlertValues(values) })
    }),
  systemAlertPreview: (values: Partial<SettingsValues>) =>
    request<AlertPreview>("/api/settings/system-alert-preview", { method: "POST", body: JSON.stringify({ values }) }),
  testSystemAlert: (values: Partial<SettingsValues>) =>
    request<{ ok: boolean; message: string }>("/api/settings/test-system-alert", {
      method: "POST",
      body: JSON.stringify({ values })
    }),
  exportConfig: () => downloadFile("/api/config/export"),
  previewConfigImport: (bundle: ConfigBundle) =>
    request<ConfigImportPreview>("/api/config/import-preview", { method: "POST", body: JSON.stringify({ bundle }) }),
  importConfig: (bundle: ConfigBundle) =>
    request<ConfigImportResult>("/api/config/import", { method: "POST", body: JSON.stringify({ bundle }) }),
  exportProjectConfig: () => downloadFile("/api/project-config/export"),
  previewProjectConfigImport: (bundle: ConfigBundle) =>
    request<ConfigImportPreview>("/api/project-config/import-preview", {
      method: "POST",
      body: JSON.stringify({ bundle })
    }),
  importProjectConfig: (bundle: ConfigBundle) =>
    request<ConfigImportResult>("/api/project-config/import", {
      method: "POST",
      body: JSON.stringify({ bundle })
    }),
  databaseHealth: () => request<DatabaseHealth>("/api/database-health"),
  databaseBackups: () => request<DatabaseBackup[]>("/api/database-backups"),
  createDatabaseBackup: () => request<DatabaseBackup>("/api/database-backups", { method: "POST" }),
  restoreDatabaseBackup: (filename: string) =>
    request<{ restored: DatabaseBackup; safety_backup: DatabaseBackup }>(
      `/api/database-backups/${encodeURIComponent(filename)}/restore`,
      { method: "POST" }
    )
};
