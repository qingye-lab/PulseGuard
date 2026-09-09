import type { Check, CheckType, FailureKind, NotificationStatus, Run, RunStatus, TaskSurfaceStatus } from "./types";

export type SemanticTagColor = "default" | "blue" | "success" | "warning" | "error" | "processing";

export function taskStatus(check: Check): TaskSurfaceStatus {
  if (!check.enabled) return "disabled";
  return normalizedTaskStatus(check.monitor_status || check.current_status || "unknown");
}

export function taskStatusLabel(status: TaskSurfaceStatus): string {
  return {
    healthy: "健康",
    suspected_failing: "疑似故障",
    failing: "故障",
    suspected_recovery: "疑似恢复",
    unknown: "无有效观测",
    stale: "观测过期",
    disabled: "已停用"
  }[status];
}

export function runStatusLabel(status: RunStatus): string {
  return {
    pending: "等待中",
    running: "运行中",
    ok: "正常",
    failed: "失败",
    timeout: "超时",
    skipped: "已跳过"
  }[status];
}

export function runStatusTone(status: RunStatus): "ok" | "failed" | "neutral" | "running" {
  if (status === "ok") return "ok";
  if (status === "failed" || status === "timeout") return "failed";
  if (status === "running" || status === "pending") return "running";
  return "neutral";
}

export function taskStatusTagColor(status: TaskSurfaceStatus): SemanticTagColor {
  if (status === "healthy") return "success";
  if (status === "failing") return "error";
  if (status === "suspected_failing" || status === "suspected_recovery" || status === "stale") return "warning";
  if (status === "unknown") return "processing";
  return "default";
}

function normalizedTaskStatus(status: string): TaskSurfaceStatus {
  if (
    status === "healthy" ||
    status === "suspected_failing" ||
    status === "failing" ||
    status === "suspected_recovery" ||
    status === "unknown" ||
    status === "stale" ||
    status === "disabled"
  ) {
    return status;
  }
  return "unknown";
}

export function runStatusTagColor(status: RunStatus): SemanticTagColor {
  if (status === "ok") return "success";
  if (status === "failed" || status === "timeout") return "error";
  if (status === "running" || status === "pending") return "processing";
  if (status === "skipped") return "warning";
  return "default";
}

export function runnerExecutionMeta(status: RunStatus, failureKind?: FailureKind | null): { label: string; color: SemanticTagColor } {
  if (status === "pending") return { label: "等待中", color: "processing" };
  if (status === "running") return { label: "执行中", color: "processing" };
  if (failureKind === "runner" || (status === "skipped" && failureKind !== "target")) {
    return { label: "节点异常", color: "warning" };
  }
  if ((status === "failed" || status === "timeout") && failureKind === "target") {
    return { label: "目标失败", color: "error" };
  }
  if (status === "ok") return { label: "正常", color: "success" };
  if (status === "skipped") return { label: "已跳过", color: "warning" };
  if (status === "failed" || status === "timeout") return { label: runStatusLabel(status), color: "error" };
  return { label: runStatusLabel(status), color: runStatusTagColor(status) };
}

export function runnerSummary(run: Run): string {
  const name = (run.runner_name || "local").trim();
  const region = (run.runner_region || "").trim();
  return region && region !== name ? `${name} · ${region}` : name;
}

export function notificationStatusTagColor(status?: NotificationStatus | null): SemanticTagColor {
  return notificationStatusMeta(status).color as SemanticTagColor;
}

export function dirtyTagColor(isDirty: boolean): SemanticTagColor {
  return isDirty ? "warning" : "success";
}

export async function copyTextToClipboard(value: string): Promise<void> {
  if (copyTextWithTextarea(value)) return;

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Some local browser contexts expose Clipboard API but deny writes.
    }
  }

  if (!copyTextWithTextarea(value)) throw new Error("copy command rejected");
}

function copyTextWithTextarea(value: string): boolean {
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  try {
    return document.execCommand("copy");
  } finally {
    document.body.removeChild(textarea);
  }
}

export function enabledTagColor(enabled: boolean): SemanticTagColor {
  return enabled ? "success" : "default";
}

export function notificationStatusMeta(status?: NotificationStatus | null): { label: string; color: string } {
  if (!status) {
    return { label: "未记录", color: "default" };
  }
  return (
    {
      disabled: { label: "未启用", color: "default" },
      not_required: { label: "无需告警", color: "default" },
      suppressed: { label: "未触发", color: "processing" },
      sent: { label: "已发送", color: "success" },
      failed: { label: "发送失败", color: "error" }
    } satisfies Record<NotificationStatus, { label: string; color: string }>
  )[status];
}

export function notificationChannelLabel(channel?: string | null, status?: NotificationStatus | null): string {
  if (status === "disabled" || status === "not_required") return "未使用";
  if (!channel) return "-";
  return (
    {
      feishu: "飞书",
      wecom: "企业微信",
      dingtalk: "钉钉"
    }[channel] || channel
  );
}

export function formatDate(value?: string | null): string {
  if (!value) return "未执行";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatDuration(value?: number | null): string {
  if (value === null || value === undefined) return "-";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

export function intervalLabel(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

export function artifactHref(path?: string | null): string | null {
  return path ? `/artifacts/${path.replace(/\\/g, "/")}` : null;
}

export function checkListPath(type: CheckType, checkId?: number | string | null): string {
  const basePath = type === "ui" ? "/ui-checks" : "/api-checks";
  return checkId ? `${basePath}?check_id=${encodeURIComponent(String(checkId))}` : basePath;
}

export function parseSnapshot(value?: string | null): unknown {
  if (!value) return null;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

export function compactUrl(value: string): string {
  if (value.length <= 72) return value;
  return `${value.slice(0, 42)}...${value.slice(-24)}`;
}
