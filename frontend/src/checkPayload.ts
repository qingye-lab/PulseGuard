import { hasEnabledApiAssertions, parseApiAssertions, serializeApiAssertions } from "./apiAssertions";
import { hasEnabledUiAssertions, parseUiAssertions, serializeUiAssertions } from "./uiAssertions";
import type { ApiInspectPayload, BrowserType, Check, CheckPayload, CheckType } from "./types";

const BROWSER_TYPES: BrowserType[] = ["chromium", "firefox", "webkit"];

export type BodyEditorMode = "json" | "text";

export function checkToPayload(check: Check): CheckPayload {
  return {
    name: check.name,
    type: check.type as CheckType,
    enabled: check.enabled,
    interval_seconds: check.interval_seconds,
    timeout_ms: check.timeout_ms,
    entry_url: check.entry_url,
    viewport_mode: check.viewport_mode || "web",
    method: check.method || (check.type === "api" ? "GET" : ""),
    headers_json: check.headers_json || "{}",
    body: check.body || "",
    assertions_json: check.assertions_json || "[]",
    setup_script: check.setup_script || "",
    script: check.script,
    tags: check.tags || "",
    alert_policy_json: check.alert_policy_json || "{}",
    runner_selection_mode: check.runner_selection_mode || "selected_parallel",
    runner_ids: normalizeRunnerIds(check.runner_ids),
    browser_selection_mode: check.type === "ui" ? check.browser_selection_mode || "selected_parallel" : "selected_parallel",
    browser_types: check.type === "ui" ? normalizeBrowserTypes(check.browser_types) : []
  };
}

export function checkToCopyPayload(check: Check): CheckPayload {
  return {
    ...checkToPayload(check),
    name: copyName(check.name),
    enabled: false
  };
}

function copyName(name: string): string {
  const suffix = " 副本";
  const normalized = name.trim() || "未命名任务";
  if (normalized.length + suffix.length <= 120) return `${normalized}${suffix}`;
  return `${normalized.slice(0, 120 - suffix.length)}${suffix}`;
}

interface PrepareCheckPayloadOptions {
  bodyMode?: BodyEditorMode;
}

export function prepareCheckPayload(value: CheckPayload, options: PrepareCheckPayloadOptions = {}): CheckPayload {
  const payload = normalizeCheckPayload(value);
  if (!payload.name) throw new Error("请填写任务名称");
  if (!payload.entry_url) throw new Error("请填写目标 URL");
  const usesApiAssertions = payload.type === "api" && hasEnabledApiAssertions(payload.assertions_json);
  const usesUiAssertions = payload.type === "ui" && hasEnabledUiAssertions(payload.assertions_json);
  if (payload.type === "ui" && payload.setup_script.trim() && !payload.setup_script.includes("async def setup")) {
    throw new Error("前置脚本必须定义 async def setup(ctx, page)");
  }
  if (!usesApiAssertions && !usesUiAssertions && !payload.script.includes("async def check")) {
    throw new Error(payload.type === "api" ? "请至少添加一个校验项，或填写高级脚本" : "请至少添加一个 UI 校验项，或填写高级脚本");
  }

  if (payload.type === "api") {
    try {
      const parsed = JSON.parse(payload.headers_json || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Headers 必须是 JSON Object");
      }
      payload.headers_json = JSON.stringify(parsed, null, 2);
    } catch (err) {
      if (err instanceof SyntaxError) throw new Error("Headers 必须是合法 JSON");
      throw err;
    }
    if (options.bodyMode === "json" && payload.body.trim()) {
      try {
        payload.body = JSON.stringify(JSON.parse(payload.body), null, 2);
      } catch {
        throw new Error("Body 当前为 JSON 模式，必须填写合法 JSON");
      }
    }
    payload.assertions_json = serializeApiAssertions(parseApiAssertions(payload.assertions_json));
  } else {
    payload.assertions_json = serializeUiAssertions(parseUiAssertions(payload.assertions_json));
  }
  return payload;
}

export function prepareApiInspectPayload(value: CheckPayload, options: PrepareCheckPayloadOptions = {}): ApiInspectPayload {
  const payload = normalizeCheckPayload(value);
  if (payload.type !== "api") throw new Error("只有接口任务支持发起请求预览");
  if (!payload.entry_url) throw new Error("请填写接口 URL");
  try {
    const parsed = JSON.parse(payload.headers_json || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Headers 必须是 JSON Object");
    }
    payload.headers_json = JSON.stringify(parsed, null, 2);
  } catch (err) {
    if (err instanceof SyntaxError) throw new Error("Headers 必须是合法 JSON");
    throw err;
  }
  if (options.bodyMode === "json" && payload.body.trim()) {
    try {
      payload.body = JSON.stringify(JSON.parse(payload.body), null, 2);
    } catch {
      throw new Error("Body 当前为 JSON 模式，必须填写合法 JSON");
    }
  }
  return {
    type: "api",
    entry_url: payload.entry_url,
    method: payload.method || "GET",
    headers_json: payload.headers_json,
    body: payload.body,
    script: payload.script,
    timeout_ms: payload.timeout_ms
  };
}

export function apiInspectSignature(value: CheckPayload, projectId?: number | null): string {
  const payload = normalizeCheckPayload(value);
  return JSON.stringify({
    project_id: Number.isInteger(projectId) ? Number(projectId) : null,
    entry_url: payload.entry_url,
    method: payload.method || "GET",
    headers_json: payload.headers_json,
    body: payload.body,
    script: payload.script,
    timeout_ms: payload.timeout_ms
  });
}

export function normalizeCheckPayload(value: CheckPayload): CheckPayload {
  return {
    ...value,
    name: value.name.trim(),
    entry_url: value.entry_url.trim(),
    viewport_mode: value.type === "ui" ? value.viewport_mode || "web" : "web",
    tags: value.tags?.trim() || "",
    method: value.type === "api" ? (value.method || "GET").toUpperCase() : "",
    headers_json: value.type === "api" ? value.headers_json || "{}" : "{}",
    body: value.type === "api" ? value.body || "" : "",
    assertions_json: value.assertions_json || "[]",
    interval_seconds: Math.max(5, Number(value.interval_seconds || 5)),
    timeout_ms: Math.max(500, Number(value.timeout_ms || 500)),
    setup_script: value.type === "ui" ? value.setup_script || "" : "",
    script: value.script || "",
    alert_policy_json: normalizeAlertPolicyJson(value.alert_policy_json),
    runner_selection_mode: value.runner_selection_mode === "round_robin_all" ? "round_robin_all" : "selected_parallel",
    runner_ids: normalizeRunnerIds(value.runner_ids),
    browser_selection_mode: value.type === "ui" && value.browser_selection_mode === "round_robin_all" ? "round_robin_all" : "selected_parallel",
    browser_types: value.type === "ui" ? normalizeBrowserTypes(value.browser_types) : []
  };
}

export function sameCheckPayload(left: CheckPayload, right: CheckPayload): boolean {
  function comparable(value: CheckPayload): string {
    const normalized: Record<string, unknown> = { ...normalizeCheckPayload(value) };
    for (const field of ["assertions_json", "headers_json", "alert_policy_json"]) {
      try { normalized[field] = { json: JSON.parse(String(normalized[field])) }; }
      catch { /* Invalid drafts remain distinguishable by their original text. */ }
    }
    return JSON.stringify(sortObjectKeys(normalized));
  }
  return comparable(left) === comparable(right);
}

function sortObjectKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortObjectKeys);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => [key, sortObjectKeys(item)]));
  }
  return value;
}

export function detectBodyMode(value?: string | null): BodyEditorMode {
  const text = value?.trim();
  if (!text) return "json";
  try {
    JSON.parse(text);
    return "json";
  } catch {
    return "text";
  }
}

function normalizeAlertPolicyJson(value?: string | null): string {
  if (!value?.trim()) return "{}";
  try {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return "{}";
    const policy = parsed as {
      alert_cooldown_minutes?: unknown;
      recovery_notification?: unknown;
      notification_channel_ids?: unknown;
      member_ids?: unknown;
    };
    const normalized: Record<string, unknown> = {};
    if (typeof policy.alert_cooldown_minutes === "number" && Number.isFinite(policy.alert_cooldown_minutes)) {
      normalized.alert_cooldown_minutes = Math.max(1, Math.min(1440, Math.round(policy.alert_cooldown_minutes)));
    }
    if (typeof policy.recovery_notification === "boolean") {
      normalized.recovery_notification = policy.recovery_notification;
    }
    if (Array.isArray(policy.notification_channel_ids)) {
      normalized.notification_channel_ids = Array.from(
        new Set(policy.notification_channel_ids.map((item) => String(item || "").trim()).filter(Boolean))
      );
    }
    if (Array.isArray(policy.member_ids)) {
      normalized.member_ids = Array.from(new Set(policy.member_ids.map((item) => String(item || "").trim()).filter(Boolean)));
    }
    return JSON.stringify(normalized);
  } catch {
    return "{}";
  }
}

function normalizeRunnerIds(value?: string[] | null): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const item of value || []) {
    const runnerId = String(item || "").trim();
    if (!runnerId || seen.has(runnerId)) continue;
    seen.add(runnerId);
    result.push(runnerId);
  }
  return result.length ? result : ["local"];
}

function normalizeBrowserTypes(value?: string[] | null): BrowserType[] {
  const seen = new Set<string>();
  const result: BrowserType[] = [];
  for (const item of value || []) {
    const browserType = String(item || "").trim() as BrowserType;
    if (!BROWSER_TYPES.includes(browserType) || seen.has(browserType)) continue;
    seen.add(browserType);
    result.push(browserType);
  }
  return result.length ? result : ["chromium"];
}
