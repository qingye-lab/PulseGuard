import type { ReactNode } from "react";
import { Tag } from "antd";
import type { Run } from "../../types";
import { runnerExecutionMeta } from "../../utils";

export function failureKindTag(
  value?: string | null,
  emptyNode: ReactNode = <Tag>无</Tag>
): ReactNode {
  if (value === "target") return <Tag color="error">目标页面/API</Tag>;
  if (value === "runner") return <Tag color="warning">执行环境</Tag>;
  if (value === "preparation") return <Tag color="warning">前置准备</Tag>;
  return <>{emptyNode}</>;
}

export function RunnerExecutionTag({ run }: { run: Run }) {
  const meta = runnerExecutionMeta(run.status, run.failure_kind);
  return <Tag color={meta.color}>{meta.label}</Tag>;
}
