import { Alert, Button, Descriptions, Empty, Image as AntImage, Skeleton, Space, Table, Tabs, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Archive, Download, ExternalLink, FileText, Image as ImageIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import { RunnerExecutionTag, failureKindTag } from "./shared/businessTags";
import type { Run, RunCheckRequestInfo, RunComparison, RunComparisonAssertion, RunComparisonField, RunFailureSummary } from "../types";
import { artifactHref, formatDate, formatDuration, parseSnapshot, runnerExecutionMeta, runnerSummary, runStatusLabel, runStatusTagColor } from "../utils";
import { StructuredViewer } from "./StructuredViewer";

type RunResultMode = "detail" | "debug";

interface AssertionResult {
  rule?: string;
  path?: string;
  status?: "ok" | "failed" | string;
  actual?: unknown;
  operator?: string;
  expected?: unknown;
  message?: string;
}

interface StageTiming {
  label: string;
  value: number;
}

interface MetaItemData {
  label: string;
  value: string | number;
}

interface RunGroupSummary {
  total: number;
  ok: number;
  targetFailures: number;
  runnerFailures: number;
  running: number;
}

const detailDescriptionColumns = { xs: 1, sm: 1, md: 2 } as const;
const fullDescriptionSpan = { xs: 1, sm: 1, md: 2 } as const;

interface Props {
  run: Run | null;
  mode?: RunResultMode;
  showGroupResults?: boolean;
  showSummary?: boolean;
}

export function RunResultPanel({ run, mode = "detail", showGroupResults = true, showSummary = true }: Props) {
  const [activeTab, setActiveTab] = useState("overview");
  const [activeGroupRunId, setActiveGroupRunId] = useState<number | null>(null);
  const [comparison, setComparison] = useState<RunComparison | null>(null);
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [comparisonError, setComparisonError] = useState<string | null>(null);
  const [failureSummary, setFailureSummary] = useState<RunFailureSummary | null>(null);
  const [failureSummaryLoading, setFailureSummaryLoading] = useState(false);
  const [failureSummaryError, setFailureSummaryError] = useState<string | null>(null);
  const [groupRuns, setGroupRuns] = useState<Run[]>([]);
  const [groupLoading, setGroupLoading] = useState(false);
  const [groupError, setGroupError] = useState<string | null>(null);
  const requestSnapshot = useMemo(() => parseSnapshot(run?.request_snapshot), [run?.request_snapshot]);
  const responseSnapshot = useMemo(() => parseSnapshot(run?.response_snapshot), [run?.response_snapshot]);
  const responseBody = useMemo(() => extractResponseBody(responseSnapshot), [responseSnapshot]);
  const assertionResults = useMemo(() => extractAssertionResults(responseSnapshot), [responseSnapshot]);
  const stageTimings = useMemo(() => extractStageTimings(responseSnapshot, run?.check_type), [responseSnapshot, run?.check_type]);
  const groupSummary = useMemo(() => summarizeRunGroup(groupRuns), [groupRuns]);
  const groupMetaItems = useMemo(() => summarizeRunGroupMeta(run, groupRuns), [groupRuns, run]);
  const isApiSingleGroup = run?.check_type === "api" && !groupLoading && !groupError && groupRuns.length <= 1;
  const hasGroupResults = Boolean(showGroupResults && mode === "detail" && run?.run_group_id && !isApiSingleGroup);
  const showComparison = Boolean(run && mode === "detail" && !hasGroupResults && run.check_id > 0 && ["failed", "timeout"].includes(run.status));
  const showFailureSummary = Boolean(run && mode === "detail" && !hasGroupResults && run.check_id > 0 && ["failed", "timeout", "skipped"].includes(run.status));
  const showDiagnostics = mode === "debug";

  useEffect(() => {
    setActiveTab(defaultRunTab(run, mode, responseSnapshot, assertionResults.length));
  }, [assertionResults.length, mode, responseSnapshot, run]);

  useEffect(() => {
    if (!run || !showComparison) {
      setComparison(null);
      setComparisonError(null);
      setComparisonLoading(false);
      return;
    }
    let cancelled = false;
    setComparisonLoading(true);
    setComparisonError(null);
    api
      .runComparison(run.id)
      .then((value) => {
        if (!cancelled) setComparison(value);
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setComparison(null);
          setComparisonError(err.message);
        }
      })
      .finally(() => {
        if (!cancelled) setComparisonLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [run?.id, showComparison]);

  useEffect(() => {
    if (!run || !showFailureSummary) {
      setFailureSummary(null);
      setFailureSummaryError(null);
      setFailureSummaryLoading(false);
      return;
    }
    let cancelled = false;
    setFailureSummary(null);
    setFailureSummaryLoading(true);
    setFailureSummaryError(null);
    api
      .runFailureSummary(run.id)
      .then((value) => {
        if (!cancelled) setFailureSummary(value);
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setFailureSummary(null);
          setFailureSummaryError(err.message);
        }
      })
      .finally(() => {
        if (!cancelled) setFailureSummaryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [run?.id, showFailureSummary]);

  useEffect(() => {
    if (!showGroupResults || !run?.run_group_id || mode !== "detail") {
      setGroupRuns([]);
      setGroupError(null);
      setGroupLoading(false);
      setActiveGroupRunId(null);
      return;
    }
    let cancelled = false;
    setGroupLoading(true);
    setGroupError(null);
    api
      .runs({ run_group_id: run.run_group_id, limit: 500 })
      .then((runs) => {
        if (!cancelled) setGroupRuns(sortGroupRuns(runs, run.id));
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setGroupRuns([]);
          setGroupError(err.message);
        }
      })
      .finally(() => {
        if (!cancelled) setGroupLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, run?.id, run?.run_group_id, showGroupResults]);

  useEffect(() => {
    if (!groupRuns.length) {
      setActiveGroupRunId(null);
      return;
    }
    setActiveGroupRunId((current) => {
      if (current && groupRuns.some((item) => item.id === current)) return current;
      return groupRuns.find((item) => item.id === run?.id)?.id ?? groupRuns[0].id;
    });
  }, [groupRuns, run?.id]);

  if (!run) {
    return <Empty description={false} />;
  }

  const isDraftRun = run.check_id <= 0;
  const compactNodeDetail = mode === "detail" && !showSummary;
  const showMergedAssertions = mode === "detail" && assertionResults.length > 0 && !hasGroupResults;
  const showErrorTab = Boolean(!hasGroupResults && (run.error_message || showDiagnostics));
  const showApiRequestSnapshot = run.check_type === "api" && requestSnapshot !== null;
  const showApiResponseSnapshot = run.check_type === "api" && responseSnapshot !== null;
  const uiRequestInfo = run.check_type === "ui" ? run.check : null;
  const overviewPanel = (
    <Space orientation="vertical" size={14} className="drawer-stack">
      {showFailureSummary && <FailureSummaryPanel summary={failureSummary} error={failureSummaryError} loading={failureSummaryLoading} />}
      {uiRequestInfo && <CheckRequestSection check={uiRequestInfo} />}
      <DetailSection title={compactNodeDetail ? "节点信息" : "运行信息"}>
        <Descriptions bordered column={detailDescriptionColumns} size="small">
          <Descriptions.Item label="执行结果">
            <RunnerExecutionTag run={run} />
          </Descriptions.Item>
          {!compactNodeDetail && <Descriptions.Item label="任务 ID">{isDraftRun ? "草稿调试" : run.check_id}</Descriptions.Item>}
          <Descriptions.Item label="运行记录">#{run.id}</Descriptions.Item>
          {!compactNodeDetail && run.run_group_id && <Descriptions.Item label="运行分组">{run.run_group_id}</Descriptions.Item>}
          <Descriptions.Item label="开始时间">{formatDate(run.started_at)}</Descriptions.Item>
          <Descriptions.Item label="结束时间">{formatDate(run.finished_at)}</Descriptions.Item>
          <Descriptions.Item label="耗时">{formatDuration(run.duration_ms)}</Descriptions.Item>
          {stageTimings.map((item) => (
            <Descriptions.Item key={item.label} label={item.label}>
              {formatDuration(item.value)}
            </Descriptions.Item>
          ))}
          {!compactNodeDetail && Boolean(run.consecutive_failures) && <Descriptions.Item label="连续失败">{run.consecutive_failures}</Descriptions.Item>}
          {!compactNodeDetail && Boolean(run.deduplicated_count) && <Descriptions.Item label="同错压缩">{run.deduplicated_count} 条</Descriptions.Item>}
          {hasFailureKind(run.failure_kind) && <Descriptions.Item label="失败来源">{failureKindTag(run.failure_kind)}</Descriptions.Item>}
          <Descriptions.Item label="执行节点">{runnerSummary(run)}</Descriptions.Item>
          {!compactNodeDetail && <Descriptions.Item label="节点 ID">{run.runner_id || "local"}</Descriptions.Item>}
          {run.runner_address && <Descriptions.Item label="节点地址">{run.runner_address}</Descriptions.Item>}
          {run.runner_region && <Descriptions.Item label="网络区域">{run.runner_region}</Descriptions.Item>}
          {run.check_type === "ui" && run.browser_type && <Descriptions.Item label="Browser Type">{run.browser_type}</Descriptions.Item>}
          {run.runner_browser_version && <Descriptions.Item label="浏览器版本">{run.runner_browser_version}</Descriptions.Item>}
        </Descriptions>
      </DetailSection>
      {hasGroupResults && run.run_group_id && (
        <DetailSection title="多节点执行结果" extra={<RunGroupSummaryTag summary={groupSummary} />}>
          <RunGroupPanel
            activeRunId={activeGroupRunId}
            currentRunId={run.id}
            runs={groupRuns}
            loading={groupLoading}
            error={groupError}
            onSelectRun={setActiveGroupRunId}
          />
        </DetailSection>
      )}
      {showMergedAssertions && (
        <DetailSection title="校验结果" extra={<AssertionSummaryTag results={assertionResults} />}>
          <AssertionResultsPanel results={assertionResults} />
        </DetailSection>
      )}
    </Space>
  );
  const errorPanel = (
    <Space orientation="vertical" size={12} className="drawer-stack">
      {run.error_message ? (
        <StructuredViewer title="原始错误" value={run.error_message} defaultMode="text" />
      ) : (
        <Tag color="success">{mode === "debug" ? "调试无错误" : "运行无错误"}</Tag>
      )}
      {showDiagnostics && <StructuredViewer title="错误堆栈" value={run.error_stack} defaultMode="text" />}
    </Space>
  );
  const comparisonTab = showComparison
    ? [
        {
          key: "compare",
          label: "对比",
          children: <RunComparisonPanel comparison={comparison} error={comparisonError} loading={comparisonLoading} />
        }
      ]
    : [];
  const assertionTab = mode === "detail" || !assertionResults.length ? [] : [{ key: "assertions", label: "校验", children: <AssertionResultsPanel results={assertionResults} /> }];
  const errorTab = showErrorTab ? [{ key: "error", label: "错误", children: errorPanel }] : [];
  const apiTabs = [
    { key: "overview", label: "执行信息", children: overviewPanel },
    ...comparisonTab,
    ...assertionTab,
    ...(showApiRequestSnapshot
      ? [
          { key: "request", label: "请求", children: <StructuredViewer title="请求快照" value={requestSnapshot} defaultMode="json" /> }
        ]
      : []),
    ...(showApiResponseSnapshot
      ? [
          {
            key: "response",
            label: "响应",
            children: (
              <Space orientation="vertical" size={12} className="drawer-stack">
                <StructuredViewer title="响应快照" value={responseSnapshot} defaultMode="json" />
                <StructuredViewer title="响应体" value={responseBody} defaultMode="auto" />
              </Space>
            )
          }
        ]
      : []),
    ...(showDiagnostics
      ? [
          { key: "logs", label: "日志", children: <StructuredViewer title="日志" value={run.logs} defaultMode="text" /> }
        ]
      : []),
    ...errorTab
  ];
  const uiTabs = [
    { key: "overview", label: "执行信息", children: overviewPanel },
    ...comparisonTab,
    ...assertionTab,
    ...(!hasGroupResults && requestSnapshot !== null ? [{
      key: "request", label: "请求", children: <StructuredViewer title="本次请求" value={requestSnapshot} defaultMode="json" />
    }] : []),
    ...(!hasGroupResults ? [{ key: "evidence", label: "页面证据", children: <EvidencePanel run={run} diagnosticsEnabled={showDiagnostics} onOpenResponse={() => setActiveTab("response")} /> }] : []),
    ...(showDiagnostics ? [{ key: "logs", label: "日志", children: <StructuredViewer title="日志" value={run.logs} defaultMode="text" /> }] : []),
    ...errorTab
  ];
  const tabItems = run.check_type === "api" ? apiTabs : uiTabs;
  const groupContent = hasGroupResults ? (
    <Space orientation="vertical" size={16} className="drawer-stack">
      <RunDetailSummary
        run={run}
        isDraftRun={isDraftRun}
        metaItems={groupMetaItems}
        status={<RunGroupSummaryTag summary={groupSummary} />}
        subtitle={run.run_group_id ? `运行分组 ${run.run_group_id}` : `运行记录 #${run.id}`}
      />
      <RunGroupPanel
        activeRunId={activeGroupRunId}
        currentRunId={run.id}
        runs={groupRuns}
        loading={groupLoading}
        error={groupError}
        onSelectRun={setActiveGroupRunId}
      />
    </Space>
  ) : null;
  const content = groupContent || (
    <Space orientation="vertical" size={16} className="drawer-stack">
      {showSummary && (
        <RunDetailSummary
          run={run}
          isDraftRun={isDraftRun}
          metaItems={singleRunMetaItems(run)}
          status={<RunnerExecutionTag run={run} />}
          subtitle={`运行记录 #${run.id}`}
        />
      )}

      <Tabs
        activeKey={activeTab}
        className={`run-detail-tabs ${mode === "debug" ? "debug-result-tabs" : ""}`}
        moreIcon={<span className="tabs-more-label">更多</span>}
        onChange={setActiveTab}
        items={tabItems}
      />
    </Space>
  );

  if (mode === "debug") {
    return (
      <section className="run-result-panel">
        <div className="section-title">
          <h3>调试结果</h3>
          <RunStatusTag status={run.status} />
        </div>
        {content}
      </section>
    );
  }

  return content;
}

function FailureSummaryPanel({
  error,
  loading,
  summary
}: {
  error: string | null;
  loading: boolean;
  summary: RunFailureSummary | null;
}) {
  if (loading) return <Skeleton active paragraph={{ rows: 4 }} />;
  if (error) return <Alert type="error" title={error} showIcon />;
  if (!summary) return <Empty description="暂无失败摘要" />;
  return (
    <section className="run-detail-section failure-summary-panel">
      <div className="run-detail-section-header">
        <strong>校验结论</strong>
      </div>
      <p className="failure-summary-text">{summary.summary}</p>
      {summary.signals.length > 0 && (
        <div className="failure-summary-facts">
          {summary.signals.map((signal) => (
            <span key={signal.label}>
              <span>{signal.label}</span>
              <strong>{formatAssertionValue(signal.value)}</strong>
            </span>
          ))}
        </div>
      )}
      {summary.next_steps.length > 0 && (
        <div className="failure-summary-actions">
          <strong>下一步</strong>
          <ol>
            {summary.next_steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}

function CheckRequestSection({ check }: { check: RunCheckRequestInfo }) {
  const isApi = check.type === "api";
  return (
    <DetailSection title="基础请求信息">
      <Descriptions bordered column={detailDescriptionColumns} size="small">
        <Descriptions.Item label={isApi ? "接口 URL" : "页面 URL"} span={fullDescriptionSpan}>
          <code className="run-config-url">{check.entry_url || "-"}</code>
        </Descriptions.Item>
        {isApi && <Descriptions.Item label="Method">{check.method || "GET"}</Descriptions.Item>}
        {!isApi && <Descriptions.Item label="页面模式">{viewportModeLabel(check.viewport_mode)}</Descriptions.Item>}
        <Descriptions.Item label="超时时间">{formatDuration(check.timeout_ms)}</Descriptions.Item>
      </Descriptions>
      {isApi && (
        <div className="run-request-config-grid">
          <StructuredViewer title="Headers" value={check.headers || {}} defaultMode="json" />
          <StructuredViewer title="Body" value={check.body || ""} defaultMode="auto" />
        </div>
      )}
    </DetailSection>
  );
}

function RunComparisonPanel({
  comparison,
  error,
  loading
}: {
  comparison: RunComparison | null;
  error: string | null;
  loading: boolean;
}) {
  if (loading) {
    return <Skeleton active paragraph={{ rows: 6 }} />;
  }
  if (error) {
    return <Alert type="error" title="对比加载失败" description={error} showIcon />;
  }
  if (!comparison) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对比数据" />;
  }
  if (!comparison.available) {
    return <Alert type="info" title="暂无成功基线" description={comparison.message || "同一任务暂无可对比的成功运行记录"} showIcon />;
  }

  return (
    <Space orientation="vertical" size={14} className="drawer-stack">
      <Descriptions bordered column={detailDescriptionColumns} size="small">
        <Descriptions.Item label="当前运行">#{comparison.current_run?.id || "-"}</Descriptions.Item>
        <Descriptions.Item label="成功基线">#{comparison.baseline_run?.id || "-"}</Descriptions.Item>
        <Descriptions.Item label="当前时间">{formatDate(comparison.current_run?.finished_at || comparison.current_run?.started_at)}</Descriptions.Item>
        <Descriptions.Item label="基线时间">{formatDate(comparison.baseline_run?.finished_at || comparison.baseline_run?.started_at)}</Descriptions.Item>
        <Descriptions.Item label="当前耗时">{formatDuration(comparison.current_run?.duration_ms)}</Descriptions.Item>
        <Descriptions.Item label="基线耗时">{formatDuration(comparison.baseline_run?.duration_ms)}</Descriptions.Item>
      </Descriptions>
      <ComparisonFieldsTable fields={comparison.fields || []} />
      <ComparisonAssertionsTable assertions={comparison.assertions || []} />
    </Space>
  );
}

function ComparisonFieldsTable({ fields }: { fields: RunComparisonField[] }) {
  if (!fields.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无字段差异" />;
  }
  const columns: ColumnsType<RunComparisonField> = [
    {
      title: "字段",
      dataIndex: "label",
      width: 110,
      render: (value: string, item) => (
        <Space size={6}>
          <span>{value}</span>
          {item.changed && <Tag color="warning">变化</Tag>}
        </Space>
      )
    },
    {
      title: "失败运行",
      dataIndex: "current",
      render: (value: unknown) => <code className="comparison-value">{formatAssertionValue(value)}</code>
    },
    {
      title: "最近成功",
      dataIndex: "baseline",
      render: (value: unknown) => <code className="comparison-value">{formatAssertionValue(value)}</code>
    }
  ];
  return <Table rowKey="key" size="small" pagination={false} columns={columns} dataSource={fields} scroll={{ x: 720 }} />;
}

function ComparisonAssertionsTable({ assertions }: { assertions: RunComparisonAssertion[] }) {
  if (!assertions.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="关键断言无差异" />;
  }
  const columns: ColumnsType<RunComparisonAssertion> = [
    {
      title: "断言",
      dataIndex: "rule",
      width: 190,
      render: (value: string, item) => (
        <div className="assertion-result-rule">
          <strong>{value || "-"}</strong>
          {item.path && <span>{item.path}</span>}
        </div>
      )
    },
    {
      title: "失败运行",
      dataIndex: "current_status",
      render: (value: string | null | undefined, item) => (
        <ComparisonAssertionCell
          status={value}
          actual={item.current_actual}
          operator={item.current_operator}
          expected={item.current_expected}
          message={compactAssertionMessage(item.current_message, item.path, value)}
        />
      )
    },
    {
      title: "最近成功",
      dataIndex: "baseline_status",
      render: (value: string | null | undefined, item) => (
        <ComparisonAssertionCell
          status={value}
          actual={item.baseline_actual}
          operator={item.baseline_operator}
          expected={item.baseline_expected}
          message={compactAssertionMessage(item.baseline_message, item.path, value)}
        />
      )
    }
  ];
  return <Table rowKey="key" size="small" pagination={false} columns={columns} dataSource={assertions} scroll={{ x: 860 }} />;
}

function ComparisonAssertionCell({
  status,
  actual,
  operator,
  expected,
  message
}: {
  status?: string | null;
  actual?: unknown;
  operator?: string | null;
  expected?: unknown;
  message?: string | null;
}) {
  return (
    <div className="comparison-assertion-cell">
      <Tag color={assertionStatusColor(status)}>{assertionStatusLabel(status)}</Tag>
      <div className="comparison-assertion-values">
        <span className="comparison-assertion-field">
          <span className="comparison-assertion-label">实际</span>
          <code>{formatAssertionValue(actual)}</code>
        </span>
        <span className="comparison-assertion-field">
          <span className="comparison-assertion-label">关系</span>
          <code>{formatOperatorValue(operator)}</code>
        </span>
        <span className="comparison-assertion-field">
          <span className="comparison-assertion-label">预期</span>
          <code>{formatAssertionValue(expected)}</code>
        </span>
      </div>
      {message && <span className="comparison-assertion-message">{message}</span>}
    </div>
  );
}

function RunDetailSummary({
  isDraftRun,
  metaItems,
  run,
  status,
  subtitle
}: {
  isDraftRun: boolean;
  metaItems: MetaItemData[];
  run: Run;
  status: ReactNode;
  subtitle: string;
}) {
  return (
    <section className={`run-detail-summary run-detail-${run.status}`}>
      <div className="run-detail-title">
        {status}
        {isDraftRun && <Tag color="blue">草稿调试</Tag>}
        <div>
          <strong>{run.check_name}</strong>
          <span>{subtitle}</span>
        </div>
      </div>
      <div className="run-detail-meta">
        {metaItems.map((item) => (
          <MetaItem key={item.label} label={item.label} value={item.value} />
        ))}
      </div>
    </section>
  );
}

function MetaItem({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="meta-field">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function DetailSection({ children, extra, title }: { children: ReactNode; extra?: ReactNode; title: string }) {
  return (
    <section className="run-detail-section">
      <div className="run-detail-section-header">
        <strong>{title}</strong>
        {extra}
      </div>
      {children}
    </section>
  );
}

function AssertionSummaryTag({ results }: { results: AssertionResult[] }) {
  const failed = results.filter((item) => item.status !== "ok").length;
  return <Tag color={failed ? "error" : "success"}>{results.length - failed} 通过 / {failed} 失败</Tag>;
}

function RunGroupSummaryTag({ summary }: { summary: RunGroupSummary }) {
  if (!summary.total) return <Tag>加载中</Tag>;
  if (summary.running) return <Tag color="processing">{summary.running} 执行中 / {summary.total}</Tag>;
  if (summary.targetFailures) return <Tag color="error">{summary.targetFailures} 目标失败 / {summary.total}</Tag>;
  if (summary.runnerFailures) return <Tag color="warning">{summary.runnerFailures} 节点异常 / {summary.total}</Tag>;
  return <Tag color="success">{summary.ok} 正常 / {summary.total}</Tag>;
}

function RunGroupPanel({
  activeRunId,
  currentRunId,
  error,
  loading,
  onSelectRun,
  runs
}: {
  activeRunId: number | null;
  currentRunId: number;
  error: string | null;
  loading: boolean;
  onSelectRun: (runId: number) => void;
  runs: Run[];
}) {
  if (loading) return <Skeleton active paragraph={{ rows: 4 }} />;
  if (error) return <Alert type="error" title="节点执行结果加载失败" description={error} showIcon />;
  if (!runs.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无节点执行结果" />;

  const selectedRun = runs.find((item) => item.id === activeRunId) ?? runs.find((item) => item.id === currentRunId) ?? runs[0];
  const showBrowserType = runs.some((item) => item.check_type === "ui");
  const columns: ColumnsType<Run> = [
    {
      title: "执行节点",
      dataIndex: "runner_name",
      width: 190,
      render: (_, item) => (
        <div className="assertion-result-rule">
          <strong>{runnerSummary(item)}</strong>
          <span>{item.runner_id || "local"}</span>
        </div>
      )
    },
    ...(showBrowserType
      ? [
          {
            title: "Browser Type",
            dataIndex: "browser_type",
            width: 120,
            render: (value: Run["browser_type"]) => value || "-"
          }
        ]
      : []),
    {
      title: "执行结果",
      dataIndex: "failure_kind",
      width: 108,
      align: "center",
      render: (_, item) => <RunnerExecutionTag run={item} />
    },
    {
      title: "耗时",
      dataIndex: "duration_ms",
      width: 96,
      render: (value: number | null | undefined) => formatDuration(value)
    },
    {
      title: "结束时间",
      dataIndex: "finished_at",
      width: 156,
      render: (value: string | null | undefined) => formatDate(value)
    },
    {
      title: "结果摘要",
      dataIndex: "error_message",
      render: (_, item) => runnerResultMessage(item)
    },
    {
      title: "证据",
      key: "artifacts",
      width: 150,
      render: (_, item) => <RunnerEvidenceLinks run={item} />
    }
  ];

  return (
    <Space orientation="vertical" size={14} className="drawer-stack">
      <DetailSection title="节点列表">
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          columns={columns}
          dataSource={runs}
          scroll={{ x: 760 }}
        />
      </DetailSection>
      <DetailSection title="节点详情">
        <Tabs
          activeKey={String(selectedRun.id)}
          className="run-group-detail-tabs"
          destroyOnHidden
          moreIcon={<span className="tabs-more-label">更多</span>}
          onChange={(key) => onSelectRun(Number(key))}
          items={runs.map((item) => ({
            key: String(item.id),
            label: <RunGroupTabLabel run={item} />,
            children: item.id === selectedRun.id ? <RunResultPanel run={item} mode="detail" showGroupResults={false} showSummary={false} /> : null
          }))}
        />
      </DetailSection>
    </Space>
  );
}

function RunGroupTabLabel({ run }: { run: Run }) {
  return (
    <span className="run-group-node-tab">
      <span>{runnerSummary(run)}</span>
      {run.check_type === "ui" && run.browser_type && <span>{run.browser_type}</span>}
    </span>
  );
}

function RunnerEvidenceLinks({ run }: { run: Run }) {
  const links = [
    { key: "screenshot", label: "截图", href: artifactHref(run.screenshot_path) },
    { key: "trace", label: "Trace", href: artifactHref(run.trace_path) },
    { key: "response", label: "响应", href: artifactHref(run.response_path) }
  ].filter((item) => item.href);
  if (!links.length) return <Tag>无</Tag>;
  return (
    <Space size={4} wrap>
      {links.map((item) => (
        <Button key={item.key} size="small" href={item.href || undefined} target="_blank">
          {item.label}
        </Button>
      ))}
    </Space>
  );
}

function AssertionResultsPanel({ results }: { results: AssertionResult[] }) {
  if (!results.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={false} />;
  }

  const columns: ColumnsType<AssertionResult> = [
    {
      title: "结果",
      dataIndex: "status",
      width: 86,
      render: (value: AssertionResult["status"]) => <Tag color={value === "ok" ? "success" : "error"}>{value === "ok" ? "通过" : "失败"}</Tag>
    },
    {
      title: "校验项",
      dataIndex: "rule",
      width: 180,
      render: (value: string | undefined, item) => (
        <div className="assertion-result-rule">
          <strong>{value || "-"}</strong>
          {item.path && <span>{item.path}</span>}
        </div>
      )
    },
    {
      title: "实际值",
      dataIndex: "actual",
      render: (value: unknown) => <code className="assertion-result-value">{formatAssertionValue(value)}</code>
    },
    {
      title: "关系",
      dataIndex: "operator",
      width: 76,
      align: "center"
    },
    {
      title: "预期值",
      dataIndex: "expected",
      render: (value: unknown) => <code className="assertion-result-value">{formatAssertionValue(value)}</code>
    },
    {
      title: "原因",
      dataIndex: "message",
      render: (value: string | undefined, item) => compactAssertionMessage(value, item.path, item.status) || "-"
    }
  ];

  return (
    <Table
      rowKey={(record) => String(results.indexOf(record))}
      size="small"
      pagination={false}
      columns={columns}
      dataSource={results}
      className="assertion-results-table"
      scroll={{ x: 880 }}
    />
  );
}

function EvidencePanel({
  diagnosticsEnabled,
  onOpenResponse,
  run
}: {
  diagnosticsEnabled: boolean;
  onOpenResponse: () => void;
  run: Run;
}) {
  const showDiagnosticArtifacts = diagnosticsEnabled || shouldShowEvidence(run);
  const screenshotHref = artifactHref(run.screenshot_path);
  const traceHref = showDiagnosticArtifacts ? artifactHref(run.trace_path) : null;
  const responseHref = showDiagnosticArtifacts ? artifactHref(run.response_path) : null;
  const hasArtifacts = Boolean(screenshotHref || traceHref || responseHref);

  return (
    <section className="run-evidence-panel">
      <div className="run-evidence-header">
        <div>
          <strong>现场留证</strong>
          <span>{hasArtifacts ? "已归档" : "无产物"}</span>
        </div>
        {diagnosticsEnabled && responseHref && (
          <Button size="small" icon={<FileText size={15} />} onClick={onOpenResponse}>
            查看响应
          </Button>
        )}
      </div>

      <div className={`run-evidence-grid ${screenshotHref ? "" : "run-evidence-grid-empty"}`}>
        <div className="run-screenshot-preview">
          {screenshotHref ? (
            <>
              <AntImage
                alt={`${run.check_name} 失败截图`}
                className="run-screenshot-image"
                preview={{ mask: "查看大图" }}
                src={screenshotHref}
              />
              <div className="run-screenshot-caption">
                <ImageIcon size={15} />
                <span>失败截图</span>
              </div>
            </>
          ) : (
            <div className="run-evidence-empty">
              <ImageIcon size={18} />
              <span>暂无截图</span>
            </div>
          )}
        </div>

        <div className="run-evidence-actions">
          <EvidenceAction
            icon={<ImageIcon size={16} />}
            title="截图"
            description={screenshotHref ? "可预览" : "未生成"}
            href={screenshotHref}
            actionLabel="打开原图"
          />
          {showDiagnosticArtifacts && (
            <>
              <EvidenceAction
                icon={<Archive size={16} />}
                title="Trace"
                description={traceHref ? "可下载" : "未生成"}
                href={traceHref}
                actionLabel="下载 Trace"
                download
              />
              <EvidenceAction
                icon={<FileText size={16} />}
                title="Response"
                description={responseHref ? "已归档" : "未生成"}
                href={responseHref}
                actionLabel="下载文件"
                download
              />
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function EvidenceAction({
  actionLabel,
  description,
  download,
  href,
  icon,
  title
}: {
  actionLabel: string;
  description: string;
  download?: boolean;
  href: string | null;
  icon: ReactNode;
  title: string;
}) {
  return (
    <div className={`run-evidence-action ${href ? "" : "run-evidence-action-disabled"}`}>
      <span className="run-evidence-action-icon">{icon}</span>
      <div>
        <strong>{title}</strong>
        <span>{description}</span>
      </div>
      {href ? (
        <Button size="small" href={href} target="_blank" download={download || undefined} icon={download ? <Download size={14} /> : <ExternalLink size={14} />}>
          {actionLabel}
        </Button>
      ) : (
        <Tag>未生成</Tag>
      )}
    </div>
  );
}

function extractResponseBody(value: unknown): unknown {
  if (value && typeof value === "object" && "body" in value) {
    return (value as { body?: unknown }).body;
  }
  return null;
}

function extractAssertionResults(value: unknown): AssertionResult[] {
  if (value && typeof value === "object" && "assertions" in value) {
    const assertions = (value as { assertions?: unknown }).assertions;
    return Array.isArray(assertions) ? (assertions.filter((item) => item && typeof item === "object") as AssertionResult[]) : [];
  }
  return [];
}

function extractStageTimings(value: unknown, checkType?: Run["check_type"]): StageTiming[] {
  if (!value || typeof value !== "object") return [];
  const snapshot = value as Record<string, unknown>;
  const timings = objectRecord(snapshot.timings);
  const page = objectRecord(snapshot.page);
  if (checkType === "api") {
    const requestMs = numericMs(timings?.request_ms) ?? numericMs(snapshot.duration_ms);
    return requestMs === null ? [] : [{ label: "请求耗时", value: requestMs }];
  }
  const result: StageTiming[] = [];
  const pageLoadMs = numericMs(timings?.page_load_ms) ?? numericMs(page?.load_ms);
  const assertionsMs = numericMs(timings?.assertions_ms);
  if (pageLoadMs !== null) result.push({ label: "页面加载", value: pageLoadMs });
  if (assertionsMs !== null) result.push({ label: "校验耗时", value: assertionsMs });
  return result;
}

function singleRunMetaItems(run: Run): MetaItemData[] {
  return [
    { label: "类型", value: run.check_type === "ui" ? "UI" : "API" },
    ...(run.check_type === "ui" && run.browser_type ? [{ label: "Browser Type", value: run.browser_type }] : []),
    { label: "耗时", value: formatDuration(run.duration_ms) },
    { label: "开始", value: formatDate(run.started_at) },
    { label: "结束", value: formatDate(run.finished_at) }
  ];
}

function viewportModeLabel(value?: RunCheckRequestInfo["viewport_mode"]): string {
  return value === "h5" ? "H5" : "Web";
}

function summarizeRunGroupMeta(run: Run | null, runs: Run[]): MetaItemData[] {
  const source = runs.length ? runs : run ? [run] : [];
  const startedAt = earliestDate(source.map((item) => item.started_at));
  const finishedAt = latestDate(source.map((item) => item.finished_at).filter(Boolean) as string[]);
  const nodeCount = new Set(source.map((item) => item.runner_id || item.runner_name || "local")).size;
  const browserTypeCount = new Set(source.filter((item) => item.check_type === "ui").map((item) => item.browser_type).filter(Boolean)).size;
  return [
    { label: "类型", value: run?.check_type === "api" ? "API" : "UI" },
    { label: "节点", value: source.length ? `${nodeCount} 个` : "-" },
    ...(browserTypeCount ? [{ label: "Browser Type", value: `${browserTypeCount} 类 / ${source.length} 组合` }] : []),
    { label: "开始", value: formatDate(startedAt || run?.started_at) },
    { label: "结束", value: formatDate(finishedAt || run?.finished_at) }
  ];
}

function earliestDate(values: Array<string | null | undefined>): string | null {
  return values.reduce<string | null>((current, value) => {
    if (!value) return current;
    if (!current) return value;
    return new Date(value).getTime() < new Date(current).getTime() ? value : current;
  }, null);
}

function latestDate(values: Array<string | null | undefined>): string | null {
  return values.reduce<string | null>((current, value) => {
    if (!value) return current;
    if (!current) return value;
    return new Date(value).getTime() > new Date(current).getTime() ? value : current;
  }, null);
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function numericMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function formatAssertionValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function compactAssertionMessage(message?: string | null, path?: string | null, status?: string | null): string | null {
  if (!message || status === "ok") return null;
  let result = message.trim().replace(/^(校验失败|断言失败)[：:]\s*/, "");
  const target = path?.trim();
  if (target && result.endsWith(target)) {
    result = result.slice(0, -target.length).replace(/[：:]\s*$/, "").trim();
  }
  return result || null;
}

function RunStatusTag({ status }: { status: Run["status"] }) {
  return <Tag color={runStatusTagColor(status)}>{runStatusLabel(status)}</Tag>;
}

function formatOperatorValue(value?: string | null): string {
  if (!value) return "-";
  const labels: Record<string, string> = {
    eq: "=",
    ne: "!=",
    gt: ">",
    gte: ">=",
    lt: "<",
    lte: "<=",
    contains: "包含",
    exists: "存在"
  };
  if (labels[value]) return labels[value];
  if (/^[=!<>]+$/.test(value) || /[\u4e00-\u9fa5]/.test(value)) return value;
  return "自定义";
}

function assertionStatusLabel(value?: string | null): string {
  if (!value) return "缺失";
  const labels: Record<string, string> = {
    ok: "通过",
    failed: "失败",
    timeout: "超时",
    skipped: "跳过"
  };
  return labels[value] || "异常";
}

function assertionStatusColor(value?: string | null): string {
  if (!value) return "default";
  if (value === "ok") return "success";
  if (value === "skipped") return "default";
  return "error";
}

function sortGroupRuns(runs: Run[], currentRunId: number): Run[] {
  return [...runs].sort((left, right) => {
    if (left.id === currentRunId) return -1;
    if (right.id === currentRunId) return 1;
    return left.id - right.id;
  });
}

function summarizeRunGroup(runs: Run[]): RunGroupSummary {
  return runs.reduce<RunGroupSummary>(
    (summary, run) => {
      summary.total += 1;
      if (run.status === "ok") summary.ok += 1;
      if (["running", "pending"].includes(run.status)) summary.running += 1;
      if (run.failure_kind === "target" && ["failed", "timeout"].includes(run.status)) summary.targetFailures += 1;
      if (run.failure_kind === "runner" || (run.status === "skipped" && run.failure_kind !== "target")) summary.runnerFailures += 1;
      return summary;
    },
    { total: 0, ok: 0, targetFailures: 0, runnerFailures: 0, running: 0 }
  );
}

function runnerResultMessage(run: Run): string {
  if (run.error_message) return run.error_message;
  if (run.status === "ok") return "-";
  if (run.status === "running" || run.status === "pending") return "等待完成";
  if (run.logs) return run.logs.split("\n").find((line) => line.trim()) || "-";
  return "-";
}

function hasFailureKind(value?: string | null): boolean {
  return Boolean(value && value !== "none");
}

function runHasEvidence(run: Run): boolean {
  return Boolean(run.screenshot_path || run.trace_path || run.response_path);
}

function shouldShowEvidence(run: Run): boolean {
  return runHasEvidence(run) || ["failed", "timeout", "skipped"].includes(run.status);
}

function defaultRunTab(run: Run | null, mode: RunResultMode, responseSnapshot: unknown, assertionCount: number): string {
  if (!run) return "overview";
  if (mode === "detail") return "overview";
  if (mode === "debug" && assertionCount > 0) return "assertions";
  if (mode === "debug" && run.check_type === "api" && responseSnapshot) return "response";
  if (mode === "debug" && run.error_message) return "error";
  return "overview";
}
