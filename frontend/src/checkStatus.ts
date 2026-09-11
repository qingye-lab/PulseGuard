import type { Overview } from "./types";
import type { SemanticTagColor } from "./utils";

/**
 * Monitoring readiness answers "is this project actually being watched?".
 * Event health answers "is anything broken right now?".
 *
 * These are deliberately separate: a project whose checks are all disabled has
 * no incidents, but it is not "healthy" — nothing is watching it. The overview
 * used to collapse both questions into one green tag, which reported an
 * unwatched project as healthy.
 */
export type MonitoringReadiness = "disabled" | "unobserved" | "stale" | "ready";

export type EventHealth = "healthy" | "acknowledging" | "failing" | "recovering";

export const monitoringReadinessLabel: Record<MonitoringReadiness, string> = {
  disabled: "监控未启用",
  unobserved: "尚未观测",
  stale: "观测已过期",
  ready: "监控中"
};

export const eventHealthLabel: Record<EventHealth, string> = {
  healthy: "无故障",
  acknowledging: "故障确认中",
  failing: "存在故障",
  recovering: "恢复确认中"
};

export const monitoringReadinessTagColor: Record<MonitoringReadiness, SemanticTagColor> = {
  disabled: "default",
  unobserved: "processing",
  stale: "warning",
  ready: "success"
};

export const eventHealthTagColor: Record<EventHealth, SemanticTagColor> = {
  healthy: "success",
  acknowledging: "warning",
  failing: "error",
  recovering: "processing"
};

export const monitoringReadinessHint: Record<MonitoringReadiness, string> = {
  disabled: "当前项目没有启用中的监控任务，以下数字不代表系统已被有效监控。",
  unobserved: "已有启用中的任务，但还没有产生任何有效观测结果，尚不能判断健康与否。",
  stale: "最近一次观测已超过预期周期，当前状态可能已经过时。",
  ready: "有启用中的任务且观测结果有效。"
};

export const eventHealthHint: Record<EventHealth, string> = {
  healthy: "已观测到的任务中没有故障。",
  acknowledging: "有任务被判定为故障或恢复，仍在等待确认，尚未计入当前故障数。",
  failing: "存在已确认的故障，需要处理。",
  recovering: "有故障任务正在恢复观察期内。"
};

export interface ProjectStatus {
  readiness: MonitoringReadiness;
  health: EventHealth;
  /** Number of enabled checks being monitored. */
  monitoredCount: number;
  /** Checks reporting no valid observation, or whose observation is stale. */
  staleOrUnknownCount: number;
  /** Confirmed failures. */
  failingCount: number;
  /** Failures and recoveries still inside the confirmation window. */
  confirmingCount: number;
  /** True only when monitoring is ready and nothing is failing. */
  isVerifiedHealthy: boolean;
}

export function deriveProjectStatus(overview: Overview): ProjectStatus {
  const monitoredCount = (overview?.ui_count || 0) + (overview?.api_count || 0);
  const staleOrUnknownCount = (overview?.unknown_count || 0) + (overview?.stale_count || 0);
  const failingCount = overview?.failing_count || 0;
  const confirmingCount = (overview?.suspected_failing_count || 0) + (overview?.suspected_recovery_count || 0);
  const hasObservation = monitoredCount > 0 && ((overview?.today_runs || 0) > 0 || Boolean(overview?.latest_run));
  const readiness = deriveReadiness({ monitoredCount, hasObservation, staleOrUnknownCount });
  const health = deriveEventHealth({ failingCount, confirmingCount, suspectedRecoveryCount: overview?.suspected_recovery_count || 0 });

  return {
    readiness,
    health,
    monitoredCount,
    staleOrUnknownCount,
    failingCount,
    confirmingCount,
    isVerifiedHealthy: readiness === "ready" && health === "healthy"
  };
}

function deriveReadiness({
  monitoredCount,
  hasObservation,
  staleOrUnknownCount
}: {
  monitoredCount: number;
  hasObservation: boolean;
  staleOrUnknownCount: number;
}): MonitoringReadiness {
  if (monitoredCount === 0) return "disabled";
  if (!hasObservation) return "unobserved";
  if (staleOrUnknownCount > 0) return "stale";
  return "ready";
}

function deriveEventHealth({
  failingCount,
  confirmingCount,
  suspectedRecoveryCount
}: {
  failingCount: number;
  confirmingCount: number;
  suspectedRecoveryCount: number;
}): EventHealth {
  if (failingCount > 0) return "failing";
  if (suspectedRecoveryCount > 0) return "recovering";
  if (confirmingCount > 0) return "acknowledging";
  return "healthy";
}
