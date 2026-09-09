export type CheckType = "ui" | "api";
export type ViewportMode = "web" | "h5";
export type RunStatus = "pending" | "running" | "ok" | "failed" | "timeout" | "skipped";
export type NotificationStatus = "disabled" | "not_required" | "suppressed" | "sent" | "failed";
export type FailureKind = "none" | "target" | "runner" | string;
export type RunnerSelectionMode = "selected_parallel" | "round_robin_all";
export type BrowserType = "chromium" | "firefox" | "webkit";
export type BrowserSelectionMode = "selected_parallel" | "round_robin_all";
export type ProbeRunnerRole = "local" | "child" | string;
export type ProbeRunnerConnectionMode = "manual" | "relay" | string;
export type TaskSurfaceStatus =
  | "healthy"
  | "suspected_failing"
  | "failing"
  | "suspected_recovery"
  | "unknown"
  | "stale"
  | "disabled";
export type ObservationKind = "observation" | "verification" | "draft";
export type WebhookType = "feishu" | "wecom" | "dingtalk";
export type ConfigBundle = Record<string, unknown>;
export type CheckBatchAction = "enable" | "disable" | "run";

export interface ModuleExport {
  name: string;
  signature: string | null;
  docstring: string;
  kind: "async" | "sync" | "constant";
  positional: string[];
  required_positional: number;
  keyword_only: string[];
  required_keyword_only: string[];
}

export interface ProjectModule {
  name: string;
  source: string;
  description?: string | null;
  exports: ModuleExport[];
}

export interface ModuleLibrary {
  project_id: number;
  /** Current saved library exposed by the atomic library endpoint. */
  effective_revision: number;
  effective_modules: ProjectModule[];
}

export interface Project {
  id: number;
  name: string;
  status: "active" | "archived";
  archived: boolean;
  created_at: string;
  updated_at: string;
}
export interface AlertPolicy {
  alert_cooldown_minutes?: number;
  recovery_notification?: boolean;
  notification_channel_ids?: string[];
  member_ids?: string[];
}

export interface AlertTagPolicy extends AlertPolicy {
  id: string;
  name: string;
  tag: string;
  enabled: boolean;
}

export type ApiAssertionType =
  | "status_code"
  | "response_time"
  | "json_path_exists"
  | "json_path_equals"
  | "json_path_not_empty"
  | "json_path_contains"
  | "json_path_type"
  | "json_path_length";
export type ApiJsonValueType = "string" | "number" | "boolean" | "object" | "array" | "null";
export type ApiLengthOperator = "eq" | "ne" | "gt" | "gte" | "lt" | "lte";
export type SelectorStability = "high" | "medium" | "low";
export type UiSelectorMatchMode = "first" | "all" | "every_scope";
export type UiSelectorCountMode = "attached" | "visible";
export type UiAssertionType =
  | "element_visible"
  | "element_hidden"
  | "element_not_empty"
  | "page_not_blank"
  | "text_present"
  | "text_absent"
  | "title_contains"
  | "url_contains"
  | "console_error_absent"
  | "element_count";
export type UiCountOperator = ApiLengthOperator;

export interface ApiAssertion {
  id: string;
  type: ApiAssertionType;
  enabled: boolean;
  expected_status?: number;
  max_ms?: number;
  path?: string;
  expected_value?: string;
  expected_type?: ApiJsonValueType;
  operator?: ApiLengthOperator;
  expected_length?: number;
}

export interface UiAssertion {
  id: string;
  type: UiAssertionType;
  enabled: boolean;
  selector?: string;
  selector_type?: string;
  selector_stability?: SelectorStability;
  selector_score?: number;
  scope_selector?: string;
  relative_selector?: string;
  match_mode?: UiSelectorMatchMode;
  count_mode?: UiSelectorCountMode;
  frame_path?: number[];
  expected_text?: string;
  operator?: UiCountOperator;
  expected_count?: number;
}

export interface ApiJsonPathOption {
  path: string;
  type: ApiJsonValueType;
  preview: string;
  length?: number | null;
}

export interface ApiInspectPayload {
  type: "api";
  entry_url: string;
  method: string;
  headers_json: string;
  body: string;
  script: string;
  timeout_ms: number;
}

export interface ApiHttpExchange {
  request: unknown;
  response: unknown;
  error?: string | null;
  duration_ms?: number | null;
}

export interface ApiInspectionTimings {
  request_ms?: number | null;
  task_ms?: number | null;
}

export interface ApiInspectionDiagnostics {
  stage: string;
  status: "ok" | "failed";
  message: string | null;
  error_type: string | null;
  url_variables: Record<string, "available" | "missing">;
  variables: Record<string, string>;
  request: unknown;
  response: unknown;
  http_requests: ApiHttpExchange[];
  timings: ApiInspectionTimings;
  logs: string;
  cleanup_error?: string;
}

export interface ApiInspectResult {
  status_code: number;
  duration_ms: number;
  headers: Record<string, string>;
  body: string;
  json_valid: boolean;
  json_paths: ApiJsonPathOption[];
  timings?: ApiInspectionTimings;
  diagnostics?: ApiInspectionDiagnostics;
}

export interface UiInspectPayload {
  type: "ui";
  entry_url: string;
  timeout_ms: number;
  viewport_mode: "web" | "h5";
  viewport_width: number;
  viewport_height: number;
  setup_script: string;
}

export type UiRuleInspectStatus = "ok" | "missing" | "multiple" | "invalid_selector" | "disabled" | "error";

export interface UiRuleInspectPayload extends UiInspectPayload {
  assertions_json: string;
}

export interface UiRuleInspectItem {
  id: string;
  type: UiAssertionType;
  selector: string;
  status: UiRuleInspectStatus;
  count?: number | null;
  message: string;
}

export interface UiRuleInspectResult {
  diagnostics?: UiInspectionDiagnostics;
  title: string;
  url: string;
  results: UiRuleInspectItem[];
  logs?: string;
}

export interface UiElementCandidate {
  candidate_id?: string;
  name?: string;
  semantic_label?: string;
  selector: string;
  selector_type?: string;
  stability?: SelectorStability;
  score?: number;
  selector_options?: UiSelectorOption[];
  scope_selector?: string;
  relative_selector?: string;
  match_mode?: UiSelectorMatchMode;
  count_mode?: UiSelectorCountMode;
  family_id?: string;
  frame_path?: number[];
  text: string;
  tag: string;
  role: string;
  kind?: "interactive" | "media" | "text" | "component" | "structure";
  box: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
}

export interface UiSelectorOption {
  selector: string;
  selector_type: string;
  stability?: SelectorStability;
  score?: number;
  unique?: boolean;
  match_count?: number | null;
  scope_selector?: string;
  relative_selector?: string;
  match_mode?: UiSelectorMatchMode;
  count_mode?: UiSelectorCountMode;
  family_id?: string;
  frame_path?: number[];
}

export interface UiInspectResult {
  diagnostics?: UiInspectionDiagnostics;
  title: string;
  url: string;
  viewport: {
    width: number;
    height: number;
  };
  page_size?: {
    width: number;
    height: number;
  };
  candidates: UiElementCandidate[];
  screenshot: string;
}

export interface UiInspectionDiagnostics {
  stage: string;
  status: "ok" | "failed";
  message?: string | null;
  error_type?: string | null;
  url_variables: Record<string, "available" | "missing">;
  variables: Record<string, string>;
  request: unknown;
  http_requests: unknown[];
  logs: string;
  cleanup_error?: string;
}

export type InspectionDiagnostics = UiInspectionDiagnostics | ApiInspectionDiagnostics;

export interface Check {
  project_id: number;
  id: number;
  name: string;
  type: CheckType;
  enabled: boolean;
  interval_seconds: number;
  timeout_ms: number;
  entry_url: string;
  viewport_mode: ViewportMode;
  method: string;
  headers_json: string;
  body: string;
  assertions_json: string;
  setup_script: string;
  script: string;
  tags: string;
  alert_policy_json: string;
  runner_selection_mode: RunnerSelectionMode;
  runner_ids: string[];
  browser_selection_mode: BrowserSelectionMode;
  browser_types: BrowserType[];
  created_at: string;
  updated_at: string;
  current_status?: TaskSurfaceStatus | null;
  monitor_status?: TaskSurfaceStatus | null;
  consecutive_failures: number;
  consecutive_successes?: number;
  last_success_at?: string | null;
  last_failed_at?: string | null;
  last_run_at?: string | null;
  last_run_id?: number | null;
  last_error?: string | null;
  last_duration_ms?: number | null;
  last_scheduled_at?: string | null;
  last_scheduled_run_id?: number | null;
  last_state_changed_at?: string | null;
}

export interface RunCheckRequestInfo {
  id: number;
  name: string;
  type: CheckType;
  entry_url: string;
  timeout_ms?: number | null;
  viewport_mode?: ViewportMode;
  method?: string;
  headers?: Record<string, unknown>;
  body?: string;
}

export interface CheckBatchPayload {
  action: CheckBatchAction;
  type: CheckType;
  ids: number[];
  expected_count?: number;
}

export interface CheckBatchResult {
  matched: number;
  changed: number;
  ids: number[];
  runs: Run[];
}

export interface AuditEvent {
  id: number;
  action: string;
  entity_type: string;
  entity_id?: string | null;
  entity_name: string;
  summary: string;
  payload: unknown;
  created_at: string;
}

export interface CheckVersion {
  id: number;
  check_id: number;
  action: string;
  snapshot: Record<string, unknown>;
  created_at: string;
}

export interface RunArchive {
  id: number;
  archive_date: string;
  check_type: CheckType;
  status: RunStatus;
  run_count: number;
  duration_sum_ms: number;
  duration_sample_count: number;
  last_run_at?: string | null;
  updated_at: string;
}

export type TrendPeriod = "24h" | "7d" | "30d" | "custom";

export interface TrendPoint {
  bucket_start: string;
  success_count: number;
  failure_count: number;
  duration_sum_ms: number;
  avg_duration_ms?: number | null;
  p95_duration_ms?: number | null;
  p99_duration_ms?: number | null;
}

export interface TrendMetrics {
  success_count: number;
  failure_count: number;
  duration_sum_ms: number;
  avg_duration_ms?: number | null;
  p95_duration_ms?: number | null;
  p99_duration_ms?: number | null;
  points: TrendPoint[];
}

export interface MonitoringTrendSummary extends TrendMetrics {
  check_type: CheckType;
  label: string;
}

export interface MonitoringTrendTask extends TrendMetrics {
  check_id: number;
  name: string;
  check_type: CheckType;
  enabled: boolean;
  monitor_status: TaskSurfaceStatus | string;
  last_run_at?: string | null;
  last_duration_ms?: number | null;
}

export interface MonitoringTrends {
  period: TrendPeriod | string;
  start: string;
  end: string;
  summaries: MonitoringTrendSummary[];
  tasks: {
    items: MonitoringTrendTask[];
    total: number;
    page: number;
    page_size: number;
  };
}

export interface CheckTrend extends TrendMetrics {
  period: TrendPeriod | string;
  start: string;
  end: string;
  check: {
    id: number;
    name: string;
    type: CheckType;
    enabled: boolean;
    monitor_status: TaskSurfaceStatus | string;
    last_run_at?: string | null;
    last_duration_ms?: number | null;
  };
}

export interface ProbeRunner {
  runner_id: string;
  name: string;
  address: string;
  network_region: string;
  browser_version: string;
  installed_browser_types: BrowserType[];
  available_browser_types: BrowserType[];
  browser_type_status?: Record<string, BrowserTypeStatus>;
  status: "ok" | "warning" | "offline" | string;
  enabled: boolean;
  role: ProbeRunnerRole;
  connection_mode?: ProbeRunnerConnectionMode;
  allow_insecure_http?: boolean;
  available: boolean;
  token_set?: boolean;
  token_hint?: string;
  relay_token_set?: boolean;
  relay_token_hint?: string;
  relay_token_version?: number;
  relay_token_rotation_pending?: boolean;
  allocated_internal_port?: number | null;
  deploy_command_expires_at?: string | null;
  relay_last_seen_at?: string | null;
  worker_health_last_success_at?: string | null;
  last_disconnect_reason?: string | null;
  was_available?: boolean;
  unavailable_since?: string | null;
  unavailable_notified_at?: string | null;
  created_at?: string | null;
  metadata: Record<string, unknown>;
  last_seen_at?: string | null;
  updated_at: string;
  token?: string;
}

export interface BrowserTypeStatus {
  enabled?: boolean;
  installed?: boolean;
  available?: boolean;
  prewarmed?: boolean;
  pool_size?: number;
  status?: string;
  message?: string;
}

export interface ProbeRunnerPayload {
  runner_id?: string;
  name: string;
  address: string;
  network_region: string;
  enabled?: boolean;
  token?: string;
  allow_insecure_http?: boolean;
}

export interface RelayStatus {
  enabled: boolean;
  public_host: string;
  public_port: number;
  public_url: string;
  server_fingerprint: string;
  error: string;
}

export interface RunnerProvisionPayload {
  name: string;
  network_region: string;
  target_platform: "linux" | "powershell";
  compose_url: string;
  enabled?: boolean;
}

export interface RunnerProvisionResult extends ProbeRunner {
  deployment: {
    target_platform: "linux" | "powershell";
    compose_url: string;
    command: string;
    expires_at?: string | null;
    relay_url: string;
    server_fingerprint: string;
  };
}

export interface RunnerUpdateRequest {
  target_image?: string;
  update_id?: string;
  force?: boolean;
}

export interface RunnerUpdateStatus {
  status?: string;
  update_id?: string;
  current_image?: string;
  target_image?: string;
  previous_image?: string;
  update_available?: boolean;
  update_services?: string[];
  started_at?: string;
  finished_at?: string;
  updated_at?: string;
  message?: string;
}

export interface StatusPageSnapshot {
  generated_at: string;
  summary: {
    checks_total: number;
    checks_enabled: number;
    checks_failing: number;
    runs_today: number;
  };
  maintenance: {
    enabled: boolean;
    title: string;
    message: string;
    starts_at: string;
    ends_at: string;
  };
  checks: StatusPageCheck[];
  recent_incidents: StatusPageIncident[];
}

export interface StatusPageCheck {
  id: number;
  name: string;
  type: CheckType;
  enabled: boolean;
  tags: string;
  status: TaskSurfaceStatus | RunStatus | string;
  last_run_at?: string | null;
  last_error?: string | null;
}

export interface StatusPageIncident {
  id: number;
  check_id: number;
  check_name: string;
  check_type: CheckType;
  status: RunStatus;
  started_at: string;
  duration_ms?: number | null;
  failure_kind?: FailureKind | null;
  runner_name?: string | null;
  runner_region?: string | null;
  error_message?: string | null;
}

export interface Run {
  id: number;
  project_id: number;
  check_id: number;
  check_name: string;
  check_type: CheckType;
  status: RunStatus;
  started_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  error_message?: string | null;
  error_stack?: string | null;
  logs?: string | null;
  screenshot_path?: string | null;
  trace_path?: string | null;
  response_path?: string | null;
  deduplicated_count?: number;
  request_snapshot?: string | null;
  response_snapshot?: string | null;
  runner_id?: string | null;
  runner_name?: string | null;
  runner_address?: string | null;
  runner_region?: string | null;
  runner_browser_version?: string | null;
  browser_type?: BrowserType | string | null;
  failure_kind?: FailureKind | null;
  notification_status?: NotificationStatus | null;
  notification_channel?: "feishu" | "wecom" | "dingtalk" | string | null;
  notification_error?: string | null;
  notification_sent_at?: string | null;
  trigger: string;
  observation_kind: ObservationKind;
  affects_health: boolean;
  run_group_id?: string | null;
  created_at: string;
  consecutive_failures?: number;
  check?: RunCheckRequestInfo | null;
}

export interface RunComparison {
  available: boolean;
  message: string;
  current_run?: RunComparisonRun | null;
  baseline_run?: RunComparisonRun | null;
  fields: RunComparisonField[];
  assertions: RunComparisonAssertion[];
}

export interface RunFailureSummary {
  run_id: number;
  check_id: number;
  check_name: string;
  status: RunStatus | string;
  failure_kind: FailureKind;
  summary: string;
  signals: Array<{ label: string; value: unknown }>;
  next_steps: string[];
}

export interface RunComparisonRun {
  id: number;
  check_id: number;
  check_name: string;
  check_type: CheckType;
  status: RunStatus;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
}

export interface RunComparisonField {
  key: string;
  label: string;
  current: unknown;
  baseline: unknown;
  changed: boolean;
}

export interface RunComparisonAssertion {
  key: string;
  rule: string;
  path?: string | null;
  current_status?: string | null;
  baseline_status?: string | null;
  current_actual?: unknown;
  baseline_actual?: unknown;
  current_operator?: string | null;
  baseline_operator?: string | null;
  current_expected?: unknown;
  baseline_expected?: unknown;
  current_message?: string | null;
  baseline_message?: string | null;
  changed: boolean;
}

export interface Overview {
  ui_count: number;
  api_count: number;
  failing_count: number;
  suspected_failing_count: number;
  suspected_recovery_count: number;
  suspected_count: number;
  unknown_count: number;
  stale_count: number;
  healthy_count: number;
  today_runs: number;
  latest_run: Run | null;
  latest_recovered: { name: string; type: CheckType; last_success_at: string } | null;
  recent_failures: Run[];
  trends: OverviewTrend[];
}

export interface OverviewTrend {
  key: "24h" | "7d" | string;
  label: string;
  series: OverviewTrendSeries[];
}

export interface OverviewTrendSeries {
  check_type: CheckType;
  label: string;
  runs: number;
  success_count: number;
  success_rate?: number | null;
  failure_count: number;
  duration_p50_ms?: number | null;
  duration_p95_ms?: number | null;
}

export interface SettingsValues {
  default_interval_seconds: number;
  default_ui_timeout_ms: number;
  default_api_timeout_ms: number;
  max_concurrency: number;
  max_ui_concurrency: number;
  max_queue_size: number;
  max_task_runtime_seconds: number;
  alerts_enabled: boolean;
  system_alerts_enabled: boolean;
  alert_detail_base_url: string;
  notification_channels: NotificationChannel[];
  system_notification_channels: NotificationChannel[];
  execution_notification_channel_ids: string[];
  system_notification_channel_ids: string[];
  members: Member[];
  alert_tag_policies: AlertTagPolicy[];
  environment_variables: EnvironmentVariable[];
  alert_cooldown_minutes: number;
  alert_delivery_attempts: number;
  recovery_notification: boolean;
  api_failure_confirmation_count: number;
  ui_failure_confirmation_count: number;
  recovery_confirmation_count: number;
  api_retry_attempts: number;
  ui_retry_attempts: number;
  stale_after_intervals: number;
  browser_headless: boolean;
  browser_type: BrowserType;
  enabled_browser_types: BrowserType[];
  prewarmed_browser_types: BrowserType[];
  browser_pool_sizes: Record<BrowserType, number>;
  browser_recycle_after_runs: number;
  browser_proxy: string;
  browser_viewport: string;
  run_retention_days: number;
  screenshot_retention_days: number;
  trace_retention_days: number;
  response_retention_days: number;
  similar_failure_retention_count: number;
  trace_artifacts_enabled: boolean;
  success_response_artifacts_enabled: boolean;
  database_backup_retention: number;
  read_only_token?: string;
  read_only_token_set?: boolean;
  read_only_tokens?: ReadOnlyToken[];
  local_runner_name?: string;
  local_runner_address?: string;
  local_runner_region?: string;
  maintenance_enabled?: boolean;
  maintenance_title?: string;
  maintenance_message?: string;
  maintenance_starts_at?: string;
  maintenance_ends_at?: string;
}

export interface RuntimeStatus {
  queue: {
    queued: number;
    limit: number;
    available: number;
  };
  workers: {
    running: number;
    limit: number;
    available: number;
  };
  browser: {
    running: number;
    limit: number;
    available: number;
  };
  active_checks: number;
  closing: boolean;
  scheduler: {
    running: boolean;
    scheduled_checks: number;
    next_due_at?: string | null;
    overdue_jobs: number;
  };
  node_role?: "main" | "worker" | string;
}

export interface RunPage {
  items: Run[];
  total: number;
  page: number;
  page_size: number;
}

export interface DatabaseBackup {
  filename: string;
  size_bytes: number;
  created_at: string;
}

export interface DatabaseHealth {
  status: "normal" | "attention";
  file_size_bytes: number;
  wal_size_bytes: number;
  page_size_bytes: number;
  page_count: number;
  freelist_pages: number;
  estimated_used_bytes: number;
  reclaimable_bytes: number;
  reclaimable_ratio: number;
  journal_mode: string;
  maintenance_recommended: boolean;
}

export interface ReadOnlyToken {
  id: string;
  name: string;
  created_at: string;
  project_ids: number[];
}

export interface CreatedReadOnlyToken extends ReadOnlyToken {
  token: string;
}

export interface NotificationChannel {
  id: string;
  name: string;
  type: WebhookType;
  enabled: boolean;
  webhook_url: string;
  dingtalk_secret?: string;
  dingtalk_secret_set?: boolean;
  dingtalk_secret_clear?: boolean;
}

export interface ProjectAlertSettings {
  project_id: number;
  alerts_enabled: boolean;
  notification_channels: NotificationChannel[];
  execution_notification_channel_ids: string[];
  alert_tag_policies: AlertTagPolicy[];
  alert_cooldown_minutes: number;
  recovery_notification: boolean;
  updated_at: string;
}

export interface Member {
  id: string;
  name: string;
  feishu_open_id: string;
  wecom_user_id: string;
  wecom_mobile: string;
  dingtalk_user_id: string;
  dingtalk_mobile: string;
}

export interface EnvironmentVariable {
  id: string;
  name: string;
  value: string;
  secret: boolean;
  value_set?: boolean;
  value_clear?: boolean;
}

export interface AlertPreview {
  channels: AlertPreviewChannel[];
  message_text: string;
}

export interface AlertPreviewChannel {
  id: string;
  name: string;
  type: WebhookType;
  enabled: boolean;
  target: {
    valid: boolean;
    origin: string;
    path: string;
    query_keys: string[];
    issues: string[];
  };
  signing_enabled: boolean;
  payload: unknown;
}

export interface ConfigExportFile {
  blob: Blob;
  filename: string;
}

export interface ConfigImportIssue {
  message?: string;
  severity?: "error" | "warning" | "info" | string;
}

export interface ConfigImportPreview {
  valid?: boolean;
  ok?: boolean;
  summary?: ConfigImportSummary;
  counts?: ConfigImportSummary;
  errors?: Array<ConfigImportIssue | string>;
  warnings?: Array<ConfigImportIssue | string>;
  issues?: Array<ConfigImportIssue | string>;
  [key: string]: unknown;
}

export interface ConfigImportResult {
  ok?: boolean;
  message?: string;
  summary?: ConfigImportSummary;
  [key: string]: unknown;
}

export interface ConfigImportSummary {
  checks?: number;
  ui_checks?: number;
  api_checks?: number;
  settings?: number;
  runners?: number;
  conflicts?: number;
  notification_channels?: number;
  environment_variables?: number;
  variables?: number;
  [key: string]: unknown;
}

export type CheckPayload = Omit<
  Check,
  | "id"
  | "project_id"
  | "created_at"
  | "updated_at"
  | "current_status"
  | "monitor_status"
  | "consecutive_failures"
  | "consecutive_successes"
  | "last_success_at"
  | "last_failed_at"
  | "last_run_at"
  | "last_run_id"
  | "last_error"
  | "last_scheduled_at"
  | "last_scheduled_run_id"
  | "last_state_changed_at"
>;
