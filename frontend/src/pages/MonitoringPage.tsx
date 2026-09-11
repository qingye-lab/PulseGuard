import {
  Alert,
  Badge,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Pagination,
  Popover,
  Segmented,
  Skeleton,
  Space,
  Tag,
  TimePicker,
  Tooltip,
  Typography
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { Activity, Calendar, Maximize2, RefreshCw, RotateCcw, SlidersHorizontal } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { InitialLoadState } from "../components/InitialLoadState";
import { palette } from "../designSystem";
import type {
  CheckTrend,
  CheckType,
  MonitoringTrendSummary,
  MonitoringTrendTask,
  MonitoringTrends,
  TrendPeriod
} from "../types";
import { formatDuration } from "../utils";

const { RangePicker } = DatePicker;
const { Text } = Typography;
const TASK_PAGE_SIZE = 12;
const loadLatencyChart = () => import("../components/LatencyChart");
const LatencyChart = lazy(loadLatencyChart);
const NUMBER_FORMATTER = new Intl.NumberFormat();
function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "0";
  return NUMBER_FORMATTER.format(value);
}

function formatPercent(numerator: number, denominator: number): string {
  if (!denominator) return "-";
  return `${((numerator / denominator) * 100).toFixed(1)}%`;
}

const SKELETON_TASK_KEYS = Array.from({ length: 6 }, (_, index) => `skeleton-${index}`);

type DateRange = [Dayjs | null, Dayjs | null] | null;
type HourRange = [Dayjs | null, Dayjs | null] | null;

interface Dimension {
  period: TrendPeriod;
  start?: string;
  end?: string;
  hourStart?: string;
  hourEnd?: string;
}

interface TrendQuery {
  period: TrendPeriod;
  start?: string;
  end?: string;
  hour_start?: string;
  hour_end?: string;
}

const PERIOD_OPTIONS = [
  { label: "24 小时", value: "24h" as const },
  { label: "7 天", value: "7d" as const },
  { label: "30 天", value: "30d" as const },
  { label: "自定义", value: "custom" as const }
];

const TYPE_OPTIONS = [
  { label: "全部", value: "" as const },
  { label: "UI", value: "ui" as const },
  { label: "API", value: "api" as const }
];

function dimensionToQuery(dim: Dimension): TrendQuery {
  return {
    period: dim.period,
    start: dim.period === "custom" ? dim.start : undefined,
    end: dim.period === "custom" ? dim.end : undefined,
    hour_start: dim.hourStart,
    hour_end: dim.hourEnd
  };
}

function isCompleteDateRange(value: DateRange): value is [Dayjs, Dayjs] {
  if (!value?.[0] || !value?.[1]) return false;
  return !value[0].isAfter(value[1], "day");
}

function isTrendDimensionReady(dim: Dimension): boolean {
  return dim.period !== "custom" || Boolean(dim.start && dim.end);
}

function dimensionDateRange(dim: Dimension): DateRange {
  if (dim.period !== "custom") return null;
  return [dim.start ? dayjs(dim.start) : null, dim.end ? dayjs(dim.end) : null];
}

function normalizeDateRange(value: DateRange): DateRange {
  return value ? [value[0] ?? null, value[1] ?? null] : null;
}

function customRangeButtonLabel(dim: Dimension): string {
  if (dim.period === "custom" && dim.start && dim.end) {
    return `${dayjs(dim.start).format("MM-DD")} - ${dayjs(dim.end).format("MM-DD")}`;
  }
  return "选择时间范围";
}

function dateRangeToDimensionRange(value: DateRange): Pick<Dimension, "start" | "end"> | null {
  if (!isCompleteDateRange(value)) return null;
  return {
    start: value[0].startOf("day").toISOString(),
    end: value[1].endOf("day").toISOString()
  };
}

function defaultDimension(): Dimension {
  return { period: "24h" };
}

function periodLabel(period: TrendPeriod): string {
  return PERIOD_OPTIONS.find((option) => option.value === period)?.label || String(period);
}

function dimensionSummary(dim: Dimension): string {
  const parts: string[] = [periodLabel(dim.period)];
  if (dim.period === "custom" && dim.start && dim.end) {
    const start = dayjs(dim.start).format("MM-DD HH:mm");
    const end = dayjs(dim.end).format("MM-DD HH:mm");
    parts.push(`${start} - ${end}`);
  }
  if (dim.hourStart && dim.hourEnd) {
    parts.push(`每日 ${dim.hourStart} - ${dim.hourEnd}`);
  }
  return parts.join("，");
}

function compactDimensionSummary(dim: Dimension): string[] {
  const period =
    dim.period === "24h" ? "24h" : dim.period === "7d" ? "7d" : dim.period === "30d" ? "30d" : "自定义";
  const isAllDay =
    !dim.hourStart ||
    !dim.hourEnd ||
    (dim.hourStart === "00:00" && (dim.hourEnd === "23:59" || dim.hourEnd === "23:59:59"));
  return isAllDay ? [period] : [period, `${dim.hourStart}-${dim.hourEnd}`];
}

function dimensionsEqual(a: Dimension, b: Dimension): boolean {
  return (
    a.period === b.period &&
    (a.start || "") === (b.start || "") &&
    (a.end || "") === (b.end || "") &&
    (a.hourStart || "") === (b.hourStart || "") &&
    (a.hourEnd || "") === (b.hourEnd || "")
  );
}

const VALID_PERIODS: TrendPeriod[] = ["24h", "7d", "30d", "custom"];

function parsePeriodParam(value: string | null): TrendPeriod {
  return VALID_PERIODS.includes(value as TrendPeriod) ? (value as TrendPeriod) : "24h";
}

function parseTypeParam(value: string | null): CheckType | "" {
  return value === "ui" || value === "api" ? value : "";
}

function parseHourParam(value: string | null): string | undefined {
  if (!value) return undefined;
  return /^\d{2}:\d{2}$/.test(value) ? value : undefined;
}

function parseDateParam(value: string | null): string | undefined {
  if (!value) return undefined;
  return dayjs(value).isValid() ? value : undefined;
}

function dimensionFromParams(params: URLSearchParams): Dimension {
  const period = parsePeriodParam(params.get("period"));
  const start = parseDateParam(params.get("start"));
  const end = parseDateParam(params.get("end"));
  return {
    period,
    start: period === "custom" ? start : undefined,
    end: period === "custom" ? end : undefined,
    hourStart: parseHourParam(params.get("hour_start")),
    hourEnd: parseHourParam(params.get("hour_end"))
  };
}

function dimensionToParams(target: URLSearchParams, dim: Dimension): void {
  if (dim.period && dim.period !== "24h") target.set("period", dim.period);
  if (dim.period === "custom" && dim.start) target.set("start", dim.start);
  if (dim.period === "custom" && dim.end) target.set("end", dim.end);
  if (dim.hourStart) target.set("hour_start", dim.hourStart);
  if (dim.hourEnd) target.set("hour_end", dim.hourEnd);
}

function paramsEqual(a: URLSearchParams, b: URLSearchParams): boolean {
  const aSorted = new URLSearchParams(a);
  const bSorted = new URLSearchParams(b);
  aSorted.sort();
  bSorted.sort();
  return aSorted.toString() === bSorted.toString();
}

export function MonitoringPage() {
  // Fetch the primary chart runtime alongside the trend request, not after it.
  useEffect(() => {
    // React.lazy remains the error owner when the component is rendered.
    void loadLatencyChart().catch(() => undefined);
  }, []);
  const [searchParams, setSearchParams] = useSearchParams();
  const [globalDimension, setGlobalDimension] = useState<Dimension>(() => dimensionFromParams(searchParams));
  const [type, setType] = useState<CheckType | "">(() => parseTypeParam(searchParams.get("type")));
  const [q, setQ] = useState(() => searchParams.get("q") || "");
  const [debouncedQ, setDebouncedQ] = useState(() => (searchParams.get("q") || "").trim());
  const [page, setPage] = useState(() => {
    const raw = Number(searchParams.get("page"));
    return Number.isFinite(raw) && raw >= 1 ? Math.floor(raw) : 1;
  });
  const [data, setData] = useState<MonitoringTrends | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<MonitoringTrendTask | null>(null);
  const [overrides, setOverrides] = useState<Record<number, Dimension>>({});
  const [customRangeOpen, setCustomRangeOpen] = useState(() => {
    const initialDimension = dimensionFromParams(searchParams);
    return initialDimension.period === "custom" && !isTrendDimensionReady(initialDimension);
  });
  const [customRangeDraft, setCustomRangeDraft] = useState<DateRange>(() => {
    const initialDimension = dimensionFromParams(searchParams);
    return dimensionDateRange(initialDimension);
  });
  const didMountFilterReset = useRef(false);

  useEffect(() => {
    if (!isTrendDimensionReady(globalDimension)) {
      setCustomRangeDraft(dimensionDateRange(globalDimension));
      setCustomRangeOpen(true);
    }
  }, [globalDimension]);

  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedQ(q.trim()), 280);
    return () => window.clearTimeout(handle);
  }, [q]);

  // 把分页之外的筛选变化重置为第 1 页（避免空页）
  useEffect(() => {
    if (!didMountFilterReset.current) {
      didMountFilterReset.current = true;
      return;
    }
    setPage(1);
  }, [globalDimension, type, debouncedQ]);

  // URL 同步：把当前筛选写入 query string
  useEffect(() => {
    const next = new URLSearchParams();
    dimensionToParams(next, globalDimension);
    if (type) next.set("type", type);
    if (debouncedQ) next.set("q", debouncedQ);
    if (page > 1) next.set("page", String(page));
    if (!paramsEqual(next, searchParams)) {
      setSearchParams(next, { replace: true });
    }
  }, [globalDimension, type, debouncedQ, page, searchParams, setSearchParams]);

  const fetchKey = useMemo(
    () => JSON.stringify({ globalDimension, type, debouncedQ, page }),
    [debouncedQ, globalDimension, page, type]
  );

  const loadList = useCallback(
    (signal: AbortSignal) => {
      if (!isTrendDimensionReady(globalDimension)) {
        setData(null);
        setError(null);
        setLoading(false);
        return Promise.resolve();
      }
      setLoading(true);
      const query = dimensionToQuery(globalDimension);
      return api
        .monitoringTrends({
          ...query,
          type,
          q: debouncedQ,
          page,
          page_size: TASK_PAGE_SIZE
        })
        .then((payload) => {
          if (signal.aborted) return;
          setData(payload);
          setError(null);
        })
        .catch((err) => {
          if (signal.aborted) return;
          setError((err as Error).message);
        })
        .finally(() => {
          if (signal.aborted) return;
          setLoading(false);
        });
    },
    [debouncedQ, globalDimension, page, type]
  );

  useEffect(() => {
    const controller = new AbortController();
    loadList(controller.signal);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchKey]);

  const refresh = useCallback(() => {
    const controller = new AbortController();
    loadList(controller.signal);
  }, [loadList]);

  function handleGlobalDimensionChange(next: Dimension) {
    if (dimensionsEqual(next, globalDimension)) return;
    setOverrides({});
    setGlobalDimension(next);
  }

  function openCustomRangeDialog() {
    setCustomRangeDraft(dimensionDateRange({ ...globalDimension, period: "custom" }));
    setCustomRangeOpen(true);
  }

  function confirmCustomRange() {
    const range = dateRangeToDimensionRange(customRangeDraft);
    if (!range) return;
    handleGlobalDimensionChange({
      ...globalDimension,
      period: "custom",
      ...range
    });
    setCustomRangeOpen(false);
  }

  function cancelCustomRange() {
    setCustomRangeOpen(false);
    if (!isTrendDimensionReady(globalDimension)) {
      handleGlobalDimensionChange(defaultDimension());
    }
  }

  function handleOverrideChange(checkId: number, next: Dimension | null) {
    setOverrides((prev) => {
      const draft = { ...prev };
      if (next === null) {
        delete draft[checkId];
      } else {
        draft[checkId] = next;
      }
      return draft;
    });
  }

  const summaries = data?.summaries || [];
  const tasks = data?.tasks.items || [];
  const overrideCount = Object.keys(overrides).length;
  const totalTasks = data?.tasks.total ?? 0;
  const totalObservations = summaries.reduce((sum, summary) => sum + summary.success_count + summary.failure_count, 0);
  const successfulObservations = summaries.reduce((sum, summary) => sum + summary.success_count, 0);
  const overallP95 = summaries.reduce<number | null>((max, summary) => {
    if (typeof summary.p95_duration_ms !== "number") return max;
    return max == null ? summary.p95_duration_ms : Math.max(max, summary.p95_duration_ms);
  }, null);
  const activeWindow = dimensionSummary(globalDimension);
  const lastUpdated = data?.end ? dayjs(data.end).format("MM-DD HH:mm") : "-";
  const waitingForCustomRange = !isTrendDimensionReady(globalDimension);

  if (!data && !waitingForCustomRange) return <div className="page-content"><InitialLoadState loading={loading} error={error} onRetry={refresh} /></div>;

  return (
    <div className="page-content monitoring-page">
      {error && (
        <Alert
          type="error"
          title={error}
          showIcon
          closable
          onClose={() => setError(null)}
          role="alert"
        />
      )}

      <Card
        title="趋势范围"
        className="project-summary-card monitoring-range-card"
        extra={
          <Space size={8}>
            {waitingForCustomRange && <Tag color="warning">待选择时间</Tag>}
            {overrideCount > 0 && <Tag color="processing">{overrideCount} 个独立维度</Tag>}
            <Button
              icon={<RefreshCw size={15} aria-hidden="true" />}
              loading={loading}
              disabled={loading}
              onClick={refresh}
            >
              刷新
            </Button>
          </Space>
        }
      >
        <GlobalFilterBar
          dimension={globalDimension}
          onDimensionChange={handleGlobalDimensionChange}
          onCustomPeriodRequest={openCustomRangeDialog}
          type={type}
          onTypeChange={setType}
          q={q}
          onQueryChange={setQ}
        />
        <Descriptions
          className="project-state-descriptions monitoring-range-summary"
          bordered
          size="small"
          column={{ xs: 1, sm: 2, md: 3, lg: 3, xl: 5, xxl: 5 }}
          items={[
            { key: "window", label: "数据范围", children: activeWindow },
            { key: "tasks", label: "任务数", children: formatCount(totalTasks) },
            { key: "observations", label: "观测量", children: formatCount(totalObservations) },
            { key: "success", label: "达标率", children: formatPercent(successfulObservations, totalObservations) },
            { key: "p95", label: "P95", children: formatDuration(overallP95) }
          ]}
        />
        <Text type="secondary" className="monitoring-last-updated" aria-live="polite">更新于 {lastUpdated}</Text>
      </Card>

      <Modal
        title="选择自定义时间范围"
        open={customRangeOpen}
        onOk={confirmCustomRange}
        onCancel={cancelCustomRange}
        okText="确定"
        cancelText="取消"
        destroyOnHidden
        width={624}
        className="monitoring-custom-range-modal"
        okButtonProps={{ disabled: !isCompleteDateRange(customRangeDraft) }}
      >
        <RangePicker
          value={customRangeDraft}
          allowClear
          onCalendarChange={(next) => setCustomRangeDraft(normalizeDateRange(next as DateRange))}
          onChange={(next) => setCustomRangeDraft(normalizeDateRange(next as DateRange))}
          className="monitoring-custom-range-picker"
          popupClassName="monitoring-custom-range-dropdown"
          style={{ width: "100%" }}
        />
      </Modal>

      <section className="monitoring-summary-grid" aria-label="整体趋势">
        {loading && !data ? (
          <>
            <Card><Skeleton active paragraph={{ rows: 4 }} /></Card>
            <Card><Skeleton active paragraph={{ rows: 4 }} /></Card>
          </>
        ) : summaries.length ? (
          summaries.map((summary) => <SummaryCard key={summary.check_type} summary={summary} />)
        ) : (
          <Card className="monitoring-summary-empty"><Empty description="暂无整体趋势数据" /></Card>
        )}
      </section>

      <Card title="任务趋势" extra={<Tag>{formatCount(totalTasks)} 个任务</Tag>} className="project-work-card monitoring-task-card">
        {loading && !data ? (
          <div className="monitoring-task-list">
            {SKELETON_TASK_KEYS.map((key) => (
              <div className="monitoring-task-row is-loading" key={key}><Skeleton active paragraph={{ rows: 3 }} /></div>
            ))}
          </div>
        ) : tasks.length ? (
          <>
            <div className="monitoring-task-list">
              {tasks.map((task) => (
                <TaskTrendRow
                  key={task.check_id}
                  task={task}
                  globalDimension={globalDimension}
                  override={overrides[task.check_id]}
                  onOverrideChange={(next) => handleOverrideChange(task.check_id, next)}
                  onOpen={() => setActiveTask(task)}
                />
              ))}
            </div>
            <Pagination
              className="monitoring-pagination"
              current={data?.tasks.page}
              pageSize={data?.tasks.page_size}
              total={data?.tasks.total}
              showSizeChanger={false}
              onChange={setPage}
            />
          </>
        ) : (
          <Empty description="没有符合筛选条件的监控任务" />
        )}
      </Card>

      <TaskTrendDrawer
        key={activeTask?.check_id ?? "closed"}
        task={activeTask}
        defaultDimension={activeTask ? overrides[activeTask.check_id] || globalDimension : globalDimension}
        resetDimension={globalDimension}
        onClose={() => setActiveTask(null)}
        onPersist={(checkId, dim) => handleOverrideChange(checkId, dim)}
      />
    </div>
  );
}

function GlobalFilterBar({
  dimension,
  onDimensionChange,
  onCustomPeriodRequest,
  type,
  onTypeChange,
  q,
  onQueryChange
}: {
  dimension: Dimension;
  onDimensionChange: (next: Dimension) => void;
  onCustomPeriodRequest: () => void;
  type: CheckType | "";
  onTypeChange: (next: CheckType | "") => void;
  q: string;
  onQueryChange: (next: string) => void;
}) {
  const hourRange: HourRange = useMemo(() => {
    if (!dimension.hourStart || !dimension.hourEnd) return null;
    return [dayjs(dimension.hourStart, "HH:mm"), dayjs(dimension.hourEnd, "HH:mm")];
  }, [dimension.hourEnd, dimension.hourStart]);

  const hasHourWindow = Boolean(dimension.hourStart && dimension.hourEnd);

  return (
    <div className="monitoring-global-filter" aria-label="监控筛选">
      <div className="monitoring-filter-cluster">
        <div className="monitoring-filter-block">
          <span className="monitoring-filter-label">周期</span>
          <Segmented
            value={dimension.period}
            onChange={(value) => {
              const period = value as TrendPeriod;
              if (period === "custom" && !dimension.start && !dimension.end) {
                onCustomPeriodRequest();
                return;
              }
              const next: Dimension = { ...dimension, period };
              if (period !== "custom") {
                next.start = undefined;
                next.end = undefined;
              }
              onDimensionChange(next);
            }}
            options={PERIOD_OPTIONS}
          />
        </div>
        {dimension.period === "custom" && (
          <div className="monitoring-filter-block monitoring-date-filter-block">
            <span className="monitoring-filter-label">时间</span>
            <Button
              className="monitoring-filter-range"
              icon={<Calendar size={14} aria-hidden="true" />}
              onClick={onCustomPeriodRequest}
            >
              {customRangeButtonLabel(dimension)}
            </Button>
          </div>
        )}
        <div className="monitoring-filter-block">
          <span className="monitoring-filter-label">类型</span>
          <Segmented
            value={type}
            onChange={(value) => onTypeChange(value as CheckType | "")}
            options={TYPE_OPTIONS}
          />
        </div>
        <Popover
          trigger="click"
          placement="bottomLeft"
          destroyOnHidden
          content={
            <div className="monitoring-hour-popover">
              <span className="monitoring-filter-label">每日时段</span>
              <TimePicker.RangePicker
                value={hourRange}
                format="HH:mm"
                minuteStep={15}
                allowClear
                onChange={(value) => {
                  if (!value || !value[0] || !value[1]) {
                    onDimensionChange({ ...dimension, hourStart: undefined, hourEnd: undefined });
                    return;
                  }
                  onDimensionChange({
                    ...dimension,
                    hourStart: value[0]?.format("HH:mm"),
                    hourEnd: value[1]?.format("HH:mm")
                  });
                }}
                style={{ width: "100%" }}
              />
              <Text type="secondary" className="monitoring-helper-text">
                例如 02:00-04:00 仅汇总每日该时段；支持跨午夜，如 22:00-02:00。
              </Text>
            </div>
          }
        >
          <Tooltip title={hasHourWindow ? `每日 ${dimension.hourStart} - ${dimension.hourEnd}` : "限制到每日固定时段"}>
            <Button
              icon={<SlidersHorizontal size={14} />}
              type={hasHourWindow ? "primary" : "default"}
              ghost={hasHourWindow}
            >
              {hasHourWindow ? `每日 ${dimension.hourStart} - ${dimension.hourEnd}` : "每日时段"}
            </Button>
          </Tooltip>
        </Popover>
      </div>
      <div className="monitoring-filter-search">
        <Input.Search
          value={q}
          onChange={(event) => onQueryChange(event.target.value)}
          onSearch={onQueryChange}
          allowClear
          autoComplete="off"
          enterButton="搜索"
          placeholder="搜索任务名称"
        />
      </div>
    </div>
  );
}

function SummaryCard({ summary }: { summary: MonitoringTrendSummary }) {
  const total = summary.success_count + summary.failure_count;
  return (
    <Card className={`monitoring-summary-card monitoring-summary-${summary.check_type}`}>
      <div className="monitoring-summary-head">
        <span className="monitoring-title-icon" aria-hidden="true"><Activity size={16} /></span>
        <div className="monitoring-summary-title">
          <strong>整体 {summary.label}</strong>
          <Text type="secondary">{formatCount(total)} 次观测</Text>
        </div>
        <div className="monitoring-summary-stats">
          <StatChip label="平均" value={formatDuration(summary.avg_duration_ms)} />
          <StatChip label="P95" value={formatDuration(summary.p95_duration_ms)} />
          {summary.p99_duration_ms != null && (
            <StatChip label="P99" value={formatDuration(summary.p99_duration_ms)} />
          )}
        </div>
      </div>
      <Suspense fallback={<Skeleton active paragraph={{ rows: 3 }} />}><LatencyChart points={summary.points} variant="summary" /></Suspense>
    </Card>
  );
}

function TaskTrendRow({
  task,
  globalDimension,
  override,
  onOverrideChange,
  onOpen
}: {
  task: MonitoringTrendTask;
  globalDimension: Dimension;
  override?: Dimension;
  onOverrideChange: (next: Dimension | null) => void;
  onOpen: () => void;
}) {
  const activeDimension = override || globalDimension;
  const isOverridden = Boolean(override);
  const [dimensionData, setDimensionData] = useState<CheckTrend | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [popoverOpen, setPopoverOpen] = useState(false);
  const lastQueryRef = useRef<string>("");

  useEffect(() => {
    if (!override) {
      setDimensionData(null);
      setLoadError(null);
      lastQueryRef.current = "";
      return;
    }
    if (!isTrendDimensionReady(override)) {
      setDimensionData(null);
      setLoadError(null);
      setLoading(false);
      lastQueryRef.current = "";
      return;
    }
    const query = dimensionToQuery(override);
    const key = JSON.stringify(query);
    if (lastQueryRef.current === key && dimensionData) return;
    lastQueryRef.current = key;
    setLoading(true);
    setLoadError(null);
    let disposed = false;
    api
      .checkTrend(task.check_id, query)
      .then((payload) => {
        if (!disposed) setDimensionData(payload);
      })
      .catch((err) => {
        if (disposed) return;
        setDimensionData(null);
        setLoadError((err as Error).message);
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [override, task.check_id]);

  const points = override ? dimensionData?.points || [] : task.points;
  const [periodLabel, hourRangeLabel] = compactDimensionSummary(activeDimension);
  const metrics = override
    ? {
        avg: dimensionData?.avg_duration_ms,
        p95: dimensionData?.p95_duration_ms,
        p99: dimensionData?.p99_duration_ms
      }
    : {
        avg: task.avg_duration_ms,
        p95: task.p95_duration_ms,
        p99: task.p99_duration_ms
  };

  return (
    <article className={`monitoring-task-row${isOverridden ? " is-overridden" : ""}`}>
      <Button
        type="text"
        htmlType="button"
        className="monitoring-task-row-main"
        onClick={onOpen}
        aria-label={`查看「${task.name}」趋势详情`}
      >
        <div className="monitoring-task-title">
          <div className="monitoring-task-title-bar">
            <div className="monitoring-task-title-line">
              <Tag>{checkTypeLabel(task.check_type)}</Tag>
              <h4 className="monitoring-task-name">{task.name}</h4>
            </div>
          </div>
        </div>
        <div className="monitoring-task-metrics">
          <StatChip label="平均" value={formatDuration(metrics.avg)} />
          <StatChip label="P95" value={formatDuration(metrics.p95)} />
          {metrics.p99 != null && <StatChip label="P99" value={formatDuration(metrics.p99)} />}
        </div>
        <div className="monitoring-chart-wrap">
          {loading && (
            <div className="monitoring-chart-loading">
              <Skeleton.Node active style={{ width: "100%", height: 168 }} />
            </div>
          )}
          {loadError && !loading && (
            <div className="monitoring-chart-loading">
              <Text type="danger">独立维度加载失败</Text>
            </div>
          )}
          <Suspense fallback={<Skeleton active paragraph={{ rows: 3 }} />}><LatencyChart points={points} variant="sparkline" /></Suspense>
        </div>
      </Button>
      <div className="monitoring-task-sidecar">
        <div className="monitoring-task-meta" title={dimensionSummary(activeDimension)}>
          <span className="monitoring-task-meta-time">{periodLabel}</span>
          {hourRangeLabel && <span className="monitoring-task-meta-range">{hourRangeLabel}</span>}
        </div>
        <div className="monitoring-task-actions">
          <Popover
            trigger="click"
            placement="bottomRight"
            open={popoverOpen}
            onOpenChange={setPopoverOpen}
            destroyOnHidden
            content={
              <DimensionEditor
                value={activeDimension}
                isOverridden={isOverridden}
                onChange={(next) => {
                  if (dimensionsEqual(next, globalDimension)) {
                    onOverrideChange(null);
                  } else {
                    onOverrideChange(next);
                  }
                }}
                onReset={() => {
                  onOverrideChange(null);
                  setPopoverOpen(false);
                }}
                onClose={() => setPopoverOpen(false)}
              />
            }
          >
            <Tooltip title="调整观察维度">
              <Badge dot={isOverridden} color={palette.primary} offset={[-4, 4]} aria-label={isOverridden ? "已使用独立维度" : undefined}>
                <Button size="small" icon={<SlidersHorizontal size={14} aria-hidden="true" />} aria-label="调整观察维度" />
              </Badge>
            </Tooltip>
          </Popover>
          <Button
            size="small"
            icon={<Maximize2 size={14} aria-hidden="true" />}
            aria-label={`放大查看「${task.name}」趋势详情`}
            title="放大查看"
            onClick={onOpen}
          />
        </div>
      </div>
    </article>
  );
}

function DimensionEditor({
  value,
  isOverridden,
  onChange,
  onReset,
  onClose
}: {
  value: Dimension;
  isOverridden: boolean;
  onChange: (next: Dimension) => void;
  onReset: () => void;
  onClose?: () => void;
}) {
  const dateRange: DateRange = useMemo(() => {
    if (value.period !== "custom") return null;
    return [value.start ? dayjs(value.start) : null, value.end ? dayjs(value.end) : null];
  }, [value.end, value.period, value.start]);

  const hourRange: HourRange = useMemo(() => {
    if (!value.hourStart || !value.hourEnd) return null;
    return [dayjs(value.hourStart, "HH:mm"), dayjs(value.hourEnd, "HH:mm")];
  }, [value.hourEnd, value.hourStart]);

  return (
    <div className="monitoring-dim-popover" onClick={(event) => event.stopPropagation()}>
      <div className="monitoring-advanced-row">
        <span className="monitoring-filter-label">周期</span>
        <Segmented
          value={value.period}
          onChange={(next) => {
            const period = next as TrendPeriod;
            onChange({
              ...value,
              period,
              start: period === "custom" ? value.start : undefined,
              end: period === "custom" ? value.end : undefined
            });
          }}
          options={PERIOD_OPTIONS}
          block
        />
      </div>
      {value.period === "custom" && (
        <div className="monitoring-advanced-row">
          <span className="monitoring-filter-label">自定义时间段</span>
          <RangePicker
            value={dateRange}
            allowClear
            onChange={(next) => {
              const range = dateRangeToDimensionRange(normalizeDateRange(next as DateRange));
              if (!range) {
                onChange({ ...value, start: undefined, end: undefined });
                return;
              }
              onChange({
                ...value,
                ...range
              });
            }}
            style={{ width: "100%" }}
          />
        </div>
      )}
      <div className="monitoring-advanced-row">
        <span className="monitoring-filter-label">每日时间段</span>
        <TimePicker.RangePicker
          value={hourRange}
          format="HH:mm"
          minuteStep={5}
          allowClear
          onChange={(next) => {
            if (!next || !next[0] || !next[1]) {
              onChange({ ...value, hourStart: undefined, hourEnd: undefined });
              return;
            }
            onChange({
              ...value,
              hourStart: next[0]?.format("HH:mm"),
              hourEnd: next[1]?.format("HH:mm")
            });
          }}
          style={{ width: "100%" }}
        />
      </div>
      <div className="monitoring-dim-actions">
        <Button
          icon={<RotateCcw size={13} aria-hidden="true" />}
          size="small"
          disabled={!isOverridden}
          onClick={onReset}
        >
          重置为全局
        </Button>
        {onClose && (
          <Button type="primary" size="small" onClick={onClose}>
            完成
          </Button>
        )}
      </div>
    </div>
  );
}

function TaskTrendDrawer({
  task,
  defaultDimension,
  resetDimension,
  onClose,
  onPersist
}: {
  task: MonitoringTrendTask | null;
  defaultDimension: Dimension;
  resetDimension: Dimension;
  onClose: () => void;
  onPersist: (checkId: number, dim: Dimension | null) => void;
}) {
  const [dimension, setDimension] = useState<Dimension>(defaultDimension);
  const [trend, setTrend] = useState<CheckTrend | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);

  useEffect(() => {
    if (task) setDimension(defaultDimension);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultDimension, task?.check_id]);

  useEffect(() => {
    if (!task) {
      setTrend(null);
      return;
    }
    if (!isTrendDimensionReady(dimension)) {
      setTrend(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    api
      .checkTrend(task.check_id, dimensionToQuery(dimension))
      .then((payload) => {
        if (controller.signal.aborted) return;
        setTrend(payload);
        setError(null);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setError((err as Error).message);
      })
      .finally(() => {
        if (controller.signal.aborted) return;
        setLoading(false);
      });
    return () => controller.abort();
  }, [dimension, task, loadAttempt]);

  return (
    <Drawer
      title={task ? `${task.name} 趋势` : "任务趋势"}
      open={Boolean(task)}
      size={920}
      onClose={onClose}
      destroyOnHidden
      classNames={{ body: "monitoring-drawer-body" }}
      extra={
        task && (
          <Space>
            <Popover
              trigger="click"
              placement="bottomRight"
              open={editorOpen}
              onOpenChange={setEditorOpen}
              destroyOnHidden
              content={
                <DimensionEditor
                  value={dimension}
                  isOverridden
                  onChange={(next) => {
                    setDimension(next);
                    onPersist(task.check_id, next);
                  }}
                  onReset={() => {
                    setDimension(resetDimension);
                    onPersist(task.check_id, null);
                    setEditorOpen(false);
                  }}
                  onClose={() => setEditorOpen(false)}
                />
              }
            >
              <Button size="small" icon={<SlidersHorizontal size={14} aria-hidden="true" />}>
                调整维度
              </Button>
            </Popover>
          </Space>
        )
      }
    >
      {error && trend && <Alert type="error" title={error} showIcon role="alert" />}
      <div className="monitoring-drawer-summary">
        <Text type="secondary">{dimensionSummary(dimension)}</Text>
      </div>
      {!isTrendDimensionReady(dimension) ? (
        <Empty description="请选择完整日期范围" />
      ) : !trend ? (
        <InitialLoadState loading={loading} error={error} onRetry={() => setLoadAttempt((attempt) => attempt + 1)} />
      ) : (
        <div className="monitoring-drawer-content">
          <div className="monitoring-drawer-metrics">
            <StatChip label="样本" value={formatCount(trend.success_count)} />
            <StatChip label="平均" value={formatDuration(trend.avg_duration_ms)} />
            <StatChip label="P95" value={formatDuration(trend.p95_duration_ms)} />
            {trend.p99_duration_ms != null && <StatChip label="P99" value={formatDuration(trend.p99_duration_ms)} />}
          </div>
          <Suspense fallback={<Skeleton active paragraph={{ rows: 3 }} />}><LatencyChart points={trend.points} variant="drawer" /></Suspense>
        </div>
      )}
    </Drawer>
  );
}

function StatChip({ label, value, tone = "neutral" }: { label: string; value: string | number; tone?: "neutral" | "danger" }) {
  const seriesClass = label === "平均" ? " series avg" : label === "P95" ? " series p95" : label === "P99" ? " series p99" : "";
  return (
    <span className={`monitoring-stat-chip ${tone}${seriesClass}`}>
      <span className="monitoring-stat-chip-label">{label}</span>
      <strong className="monitoring-stat-chip-value">{value}</strong>
    </span>
  );
}

function checkTypeLabel(type: CheckType): string {
  return type === "ui" ? "UI" : "API";
}
