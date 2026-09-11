import { Button, Tag } from "antd";
import { Eye, History } from "lucide-react";
import type { ReactNode } from "react";
import type { CheckType, Run, RunStatus } from "../types";
import { formatDate, runStatusLabel, runStatusTagColor } from "../utils";

export type BatchRunNotice = {
  type: CheckType;
  total: number;
  counts: Record<RunStatus, number>;
  latestRunId: number | null;
  issueRunId: number | null;
  finishedAt: string;
};

const RUN_STATUS_ORDER: RunStatus[] = ["ok", "failed", "timeout", "preparation_failed", "skipped", "running", "pending"];

export function summarizeBatchRuns(type: CheckType, runs: Run[]): BatchRunNotice {
  const counts: Record<RunStatus, number> = {
    pending: 0,
    running: 0,
    ok: 0,
    failed: 0,
    timeout: 0,
    skipped: 0,
    preparation_failed: 0
  };
  runs.forEach((run) => {
    counts[run.status] += 1;
  });

  const orderedRuns = [...runs].sort(compareRunsByRecency);
  const latestRun = orderedRuns.length ? orderedRuns[orderedRuns.length - 1] : null;
  const issueRun = [...orderedRuns]
    .reverse()
    .find((run) => run.status === "failed" || run.status === "timeout" || run.status === "skipped" || run.status === "preparation_failed") || null;

  return {
    type,
    total: runs.length,
    counts,
    latestRunId: latestRun?.id ?? null,
    issueRunId: issueRun?.id ?? null,
    finishedAt: latestRun?.finished_at || latestRun?.started_at || new Date().toISOString()
  };
}

export function BatchRunActions({
  notice,
  emptyAction,
  onOpenHistory,
  onOpenRun
}: {
  notice: BatchRunNotice;
  emptyAction?: ReactNode;
  onOpenHistory: (path: string) => void;
  onOpenRun: (runId: number) => void;
}) {
  return notice.total > 0 ? (
    <div className="batch-run-actions">
      {notice.issueRunId && (
        <Button size="small" icon={<Eye size={14} />} onClick={() => onOpenRun(notice.issueRunId!)}>
          查看问题详情
        </Button>
      )}
      {notice.latestRunId && notice.latestRunId !== notice.issueRunId && (
        <Button size="small" icon={<Eye size={14} />} onClick={() => onOpenRun(notice.latestRunId!)}>
          查看最近结果
        </Button>
      )}
      <Button size="small" icon={<History size={14} />} onClick={() => onOpenHistory(batchRunHistoryPath(notice))}>
        运行记录
      </Button>
    </div>
  ) : (
    emptyAction
  );
}

export function batchRunNotificationType(notice: BatchRunNotice): "success" | "info" | "warning" {
  if (!notice.total) return "info";
  if (notice.counts.failed || notice.counts.timeout || notice.counts.preparation_failed) return "warning";
  if (notice.counts.skipped) return "info";
  return "success";
}

export function batchRunMessage(notice: BatchRunNotice): string {
  const typeLabel = notice.type === "ui" ? " UI" : "接口";
  return notice.total ? `批量执行选中${typeLabel}任务完成` : `选中任务里没有可执行的${typeLabel}任务`;
}

export function batchRunHistoryPath(notice: BatchRunNotice): string {
  const params = new URLSearchParams({ type: notice.type });
  if (notice.counts.failed || notice.counts.timeout) {
    params.set("status", "failed");
  }
  return `/runs?${params.toString()}`;
}

export function BatchRunBreakdown({ notice }: { notice: BatchRunNotice }) {
  if (!notice.total) {
    return `选中任务里没有启用的${notice.type === "ui" ? " UI" : "接口"}任务。`;
  }

  return (
    <div className="batch-run-breakdown">
      <span>共 {notice.total} 条</span>
      {RUN_STATUS_ORDER.filter((status) => notice.counts[status] > 0).map((status) => (
        <Tag color={runStatusTagColor(status)} key={status}>
          {runStatusLabel(status)} {notice.counts[status]}
        </Tag>
      ))}
      <span className="batch-run-time">完成于 {formatDate(notice.finishedAt)}</span>
    </div>
  );
}

function compareRunsByRecency(a: Run, b: Run): number {
  const aTime = runTimeValue(a);
  const bTime = runTimeValue(b);
  if (aTime !== bTime) return aTime - bTime;
  return a.id - b.id;
}

function runTimeValue(run: Run): number {
  const finishedAt = run.finished_at ? Date.parse(run.finished_at) : Number.NaN;
  if (!Number.isNaN(finishedAt)) return finishedAt;
  const startedAt = Date.parse(run.started_at);
  return Number.isNaN(startedAt) ? 0 : startedAt;
}
