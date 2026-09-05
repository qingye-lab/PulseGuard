import { Alert, App, Button, Card, Checkbox, Dropdown, Empty, Input, Select, Skeleton, Space, Statistic, Switch, Table, Tag } from "antd";
import type { MenuProps, PaginationProps } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Copy, Edit3, FilterX, History, MoreHorizontal, Play, Plus, Power, PowerOff, RefreshCw, Search, Trash2 } from "lucide-react";
import { cloneElement, isValidElement, lazy, Suspense, useEffect, useMemo, useState } from "react";
import type { ReactElement } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { checkToCopyPayload } from "../checkPayload";
import { useProject } from "../ProjectContext";
import {
  BatchRunActions,
  BatchRunBreakdown,
  batchRunMessage,
  batchRunNotificationType,
  summarizeBatchRuns
} from "../components/BatchRunAlert";
import type { Check, CheckBatchAction, CheckType, ProbeRunner, Run, TaskSurfaceStatus } from "../types";
import { compactUrl, formatDate, formatDuration, intervalLabel, taskStatus, taskStatusLabel, taskStatusTagColor } from "../utils";

type EnabledFilter = "" | "enabled" | "disabled";
type BusyState = number | "batch" | null;

const CheckEditorDrawer = lazy(() => import("../components/CheckEditorDrawer").then((module) => ({ default: module.CheckEditorDrawer })));
const RunDetailDrawer = lazy(() => import("../components/RunDetailDrawer").then((module) => ({ default: module.RunDetailDrawer })));

function TaskStatusTag({ check }: { check: Check }) {
  const status = taskStatus(check);
  return <Tag color={taskStatusTagColor(status)}>{taskStatusLabel(status)}</Tag>;
}

function checkSecondaryActionItems(isDuplicating: boolean, readOnly: boolean): MenuProps["items"] {
  return [
    { key: "duplicate", icon: <Copy size={15} />, label: "复制", disabled: readOnly || isDuplicating },
    { key: "delete", icon: <Trash2 size={15} />, label: "删除", danger: true, disabled: readOnly }
  ];
}

const checkPaginationItemRender: PaginationProps["itemRender"] = (_, itemType, originalElement) => {
  const label = itemType === "prev" ? "上一页任务" : itemType === "next" ? "下一页任务" : "";
  if (!label || !isValidElement(originalElement)) return originalElement;
  return cloneElement(originalElement as ReactElement<Record<string, unknown>>, { "aria-label": label, title: label });
};

export function ChecksPage({ type }: { type: CheckType }) {
  const { message, modal } = App.useApp();
  const { currentProject } = useProject();
  const readOnly = currentProject.status === "archived";
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [checks, setChecks] = useState<Check[]>([]);
  const [editing, setEditing] = useState<Check | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [detailRunId, setDetailRunId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<BusyState>(null);
  const [duplicatingId, setDuplicatingId] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskSurfaceStatus | "">("");
  const [enabledFilter, setEnabledFilter] = useState<EnabledFilter>("");
  const [tagFilter, setTagFilter] = useState("");
  const [runnerFilter, setRunnerFilter] = useState("");
  const [runners, setRunners] = useState<ProbeRunner[]>([]);
  const [selectedCheckIds, setSelectedCheckIds] = useState<number[]>([]);
  const [isCompactList, setIsCompactList] = useState(() => window.matchMedia("(max-width: 720px)").matches);
  const navigate = useNavigate();
  const focusedCheckId = searchParams.get("check_id");

  async function load() {
    setLoading(true);
    try {
      setChecks(await api.checks(type));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    setTagFilter("");
  }, [type]);

  useEffect(() => {
    api.runners().then(setRunners).catch(() => setRunners([]));
  }, []);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 720px)");
    const sync = () => setIsCompactList(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    const runnerFilterNode = runnerFilter ? runners.find((runner) => runner.runner_id === runnerFilter) : undefined;
    return checks.filter((check) => {
      const matchesKeyword =
        !keyword || [check.name, check.entry_url, check.tags].some((value) => (value || "").toLowerCase().includes(keyword));
      const surfaceStatus = taskStatus(check);
      const matchesStatus = !statusFilter || surfaceStatus === statusFilter;
      const matchesEnabled =
        !enabledFilter || (enabledFilter === "enabled" ? check.enabled : !check.enabled);
      const matchesTag = !tagFilter || checkTagTokens(check.tags).includes(tagFilter);
      const matchesRunner =
        !runnerFilter ||
        (check.runner_selection_mode === "round_robin_all" && Boolean(runnerFilterNode?.enabled)) ||
        (check.runner_ids?.length ? check.runner_ids : ["local"]).includes(runnerFilter);
      const matchesFocused = !focusedCheckId || String(check.id) === focusedCheckId;
      return matchesKeyword && matchesStatus && matchesEnabled && matchesTag && matchesRunner && matchesFocused;
    });
  }, [checks, enabledFilter, focusedCheckId, query, runnerFilter, runners, statusFilter, tagFilter]);

  useEffect(() => {
    const visibleIds = new Set(filtered.map((check) => check.id));
    setSelectedCheckIds((current) => {
      const next = current.filter((id) => visibleIds.has(id));
      return next.length === current.length ? current : next;
    });
  }, [filtered]);

  const tagOptions = useMemo(() => {
    const tags = new Set<string>();
    checks.forEach((check) => checkTagTokens(check.tags).forEach((tag) => tags.add(tag)));
    return Array.from(tags)
      .sort((left, right) => left.localeCompare(right))
      .map((tag) => ({ label: tag, value: tag }));
  }, [checks]);

  const selectedChecks = useMemo(() => {
    const selected = new Set(selectedCheckIds);
    return filtered.filter((check) => selected.has(check.id));
  }, [filtered, selectedCheckIds]);
  const selectedIds = useMemo(() => selectedChecks.map((check) => check.id), [selectedChecks]);
  const selectedEnabledIds = useMemo(() => selectedChecks.filter((check) => check.enabled).map((check) => check.id), [selectedChecks]);

  const summary = useMemo(() => {
    const counts: Record<TaskSurfaceStatus, number> = {
      healthy: 0,
      suspected_failing: 0,
      failing: 0,
      suspected_recovery: 0,
      unknown: 0,
      stale: 0,
      disabled: 0
    };
    checks.forEach((check) => {
      counts[taskStatus(check)] += 1;
    });
    return {
      total: checks.length,
      visible: filtered.length,
      enabled: checks.filter((check) => check.enabled).length,
      ...counts
    };
  }, [checks, filtered.length]);

  const hasFilters = Boolean(query || statusFilter || enabledFilter || tagFilter || runnerFilter || focusedCheckId);
  const selectedCount = selectedChecks.length;
  const selectedStateActionItems: MenuProps["items"] = [
    { key: "enable", icon: <Power size={15} />, label: `启用选中任务（${selectedCount}）`, disabled: readOnly || selectedCount === 0 },
    { key: "disable", icon: <PowerOff size={15} />, label: `禁用选中任务（${selectedCount}）`, disabled: readOnly || selectedCount === 0 }
  ];

  async function runCheck(check: Check) {
    setBusy(check.id);
    try {
      const run = await api.runCheck(check.id);
      setDetailRunId(run.id);
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function confirmRecovery(check: Check) {
    setBusy(check.id);
    try {
      const run = await api.confirmRecovery(check.id);
      setDetailRunId(run.id);
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function toggle(check: Check) {
    setBusy(check.id);
    try {
      check.enabled ? await api.disableCheck(check.id) : await api.enableCheck(check.id);
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function remove(check: Check) {
    setBusy(check.id);
    try {
      await api.deleteCheck(check.id);
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function duplicate(check: Check) {
    setDuplicatingId(check.id);
    setError(null);
    try {
      const copied = await api.createCheck(checkToCopyPayload(check));
      await load();
      setEditing(copied);
      setDrawerOpen(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setDuplicatingId(null);
    }
  }

  function confirmRemove(check: Check) {
    modal.confirm({
      title: "删除任务",
      content: `删除“${check.name}”？`,
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => remove(check)
    });
  }

  function notifyBatchRun(runs: Run[]) {
    const notice = summarizeBatchRuns(type, runs);
    message.open({
      key: `batch-run-${type}`,
      type: batchRunNotificationType(notice),
      duration: 8,
      content: (
        <div className="batch-run-toast">
          <strong>{batchRunMessage(notice)}</strong>
          <BatchRunBreakdown notice={notice} />
          <BatchRunActions
            notice={notice}
            onOpenHistory={(path) => navigate(path)}
            onOpenRun={setDetailRunId}
            emptyAction={
              <Button size="small" icon={<Plus size={14} />} onClick={openCreateDrawer}>
                新增任务
              </Button>
            }
          />
        </div>
      )
    });
  }

  async function executeBatchAction(action: CheckBatchAction, ids: number[]) {
    setBusy("batch");
    setError(null);
    try {
      const result = await api.batchChecks({
        action,
        type,
        ids,
        expected_count: ids.length
      });
      if (action === "run") {
        notifyBatchRun(result.runs);
      } else {
        const actionLabel = action === "enable" ? "启用" : "禁用";
        message.success(`${actionLabel} ${result.changed} 条任务，匹配 ${result.matched} 条`);
      }
      setSelectedCheckIds([]);
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }

  function requestBatchAction(action: CheckBatchAction) {
    const ids = action === "run" ? selectedEnabledIds : selectedIds;
    if (!ids.length) {
      message.warning(action === "run" ? "选中任务里没有可运行任务" : "请先选择任务");
      return;
    }

    if (action === "run") {
      void executeBatchAction(action, ids);
      return;
    }

    const title = action === "enable" ? "批量启用任务" : "批量禁用任务";
    const content = `将${action === "enable" ? "启用" : "禁用"}已选中的 ${ids.length} 条任务。`;

    modal.confirm({
      title,
      content,
      okText: action === "enable" ? "启用" : "禁用",
      cancelText: "取消",
      onOk: () => executeBatchAction(action, ids)
    });
  }

  function openCreateDrawer() {
    setEditing(null);
    setDrawerOpen(true);
  }

  function resetFilters() {
    setQuery("");
    setStatusFilter("");
    setEnabledFilter("");
    setTagFilter("");
    setRunnerFilter("");
    clearTaskFocus();
  }

  function clearTaskFocus() {
    if (!focusedCheckId) return;
    const next = new URLSearchParams(searchParams);
    next.delete("check_id");
    setSearchParams(next, { replace: true });
  }

  const columns: ColumnsType<Check> = [
    {
      title: "名称",
      dataIndex: "name",
      render: (value: string, check) => (
        <div>
          <Button type="link" className="table-link" onClick={() => check.last_run_id && setDetailRunId(check.last_run_id)}>
            {value}
          </Button>
          {check.tags && <div className="tag-line">{check.tags}</div>}
        </div>
      ),
      fixed: "left",
      width: 230
    },
    ...(type === "api" ? [{ title: "Method", dataIndex: "method", width: 92 }] : []),
    {
      title: "URL",
      dataIndex: "entry_url",
      ellipsis: true,
      width: 260,
      render: (value: string) => <span title={value}>{compactUrl(value)}</span>
    },
    {
      title: "执行节点",
      width: 130,
      render: (_, check) => <Tag>{runnerStrategyLabel(check, runners)}</Tag>
    },
    { title: "状态", className: "check-status-cell", render: (_, check) => <TaskStatusTag check={check} />, width: 105 },
    {
      title: "启用",
      render: (_, check) => (
        <Switch
          aria-label={`${check.name} 启用状态`}
          checked={check.enabled}
          loading={busy === check.id}
          disabled={readOnly}
          onChange={() => toggle(check)}
        />
      ),
      width: 92
    },
    { title: "定时", dataIndex: "interval_seconds", render: (value: number) => intervalLabel(value), width: 105 },
    { title: "最近执行", dataIndex: "last_run_at", render: (value?: string | null) => formatDate(value), width: 180 },
    { title: "耗时", dataIndex: "last_duration_ms", render: (value?: number | null) => formatDuration(value), width: 105 },
    { title: "连续失败", dataIndex: "consecutive_failures", render: (value: number) => value || "-", width: 105 },
    {
      title: "操作",
      className: "check-actions-cell",
      align: "right",
      fixed: "right",
      width: 168,
      render: (_, check) => {
        const isDuplicating = duplicatingId === check.id;
        return (
          <Space className="check-row-actions" size={6}>
            <Button
              size="small"
              title="执行"
              aria-label={`${check.name} 执行`}
              icon={<Play size={15} />}
              loading={busy === check.id}
              disabled={readOnly}
              onClick={() => runCheck(check)}
            />
            {["failing", "suspected_failing", "suspected_recovery", "stale", "unknown"].includes(taskStatus(check)) && (
              <Button
                size="small"
                title="确认恢复"
                aria-label={`${check.name} 确认恢复`}
                icon={<RefreshCw size={15} />}
                loading={busy === check.id}
                disabled={readOnly}
                onClick={() => confirmRecovery(check)}
              />
            )}
            <Button
              size="small"
              title="编辑"
              aria-label={`${check.name} 编辑`}
              icon={<Edit3 size={15} />}
              disabled={readOnly}
              onClick={() => {
                setEditing(check);
                setDrawerOpen(true);
              }}
            />
            <Button
              size="small"
              title="历史"
              aria-label={`${check.name} 历史`}
              icon={<History size={15} />}
              onClick={() => navigate(`/runs?check_id=${check.id}`)}
            />
            <Dropdown
              trigger={["click"]}
              placement="bottomRight"
              menu={{
                items: checkSecondaryActionItems(isDuplicating, readOnly),
                onClick: ({ key }) => {
                  if (key === "duplicate") duplicate(check);
                  if (key === "delete") confirmRemove(check);
                }
              }}
            >
              <Button size="small" title="更多操作" aria-label={`${check.name} 更多操作`} icon={<MoreHorizontal size={15} />} />
            </Dropdown>
          </Space>
        );
      }
    }
  ];

  return (
    <div className="page-content">
      {error && <Alert type="error" title={error} showIcon />}

      <Card title="任务状态" className="project-summary-card">
        <div className="project-stat-grid project-stat-grid-five">
          <Statistic title="全部任务" value={summary.total} />
          <Statistic className="project-stat-success" title="健康" value={summary.healthy} />
          <Statistic className={summary.failing ? "project-stat-danger" : undefined} title="故障" value={summary.failing} />
          <Statistic title="确认中/待观测" value={summary.suspected_failing + summary.suspected_recovery + summary.unknown + summary.stale} />
          <Statistic title="已启用" value={summary.enabled} />
        </div>
      </Card>

      <Card
        title={type === "ui" ? "页面监控任务" : "接口监控任务"}
        className="project-data-card"
        extra={
          <Button type="primary" icon={<Plus size={16} />} onClick={openCreateDrawer} disabled={readOnly}>
            新增任务
          </Button>
        }
      >
        <div className="project-data-toolbar">
          <div className="checks-filter-row">
            <Input
              className="checks-search"
              name="checks-search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="搜索任务"
              prefix={<Search size={15} />}
              placeholder="搜索名称、URL 或标签…"
              allowClear
              autoComplete="off"
            />
            <Select
              value={statusFilter}
              className="checks-filter-control"
              onChange={(value) => setStatusFilter(value)}
              options={[
                { label: "全部状态", value: "" },
                { label: "健康", value: "healthy" },
                { label: "疑似故障", value: "suspected_failing" },
                { label: "故障", value: "failing" },
                { label: "疑似恢复", value: "suspected_recovery" },
                { label: "无有效观测", value: "unknown" },
                { label: "观测过期", value: "stale" },
                { label: "已禁用", value: "disabled" }
              ]}
            />
            <Select
              value={enabledFilter}
              className="checks-filter-control"
              onChange={(value) => setEnabledFilter(value)}
              options={[
                { label: "全部启用状态", value: "" },
                { label: "已启用", value: "enabled" },
                { label: "已禁用", value: "disabled" }
              ]}
            />
            <Select
              showSearch
              value={tagFilter}
              className="checks-filter-control"
              onChange={(value) => setTagFilter(value)}
              options={[{ label: "全部标签", value: "" }, ...tagOptions]}
              aria-label="按标签筛选任务"
            />
            <Select
              value={runnerFilter}
              className="checks-filter-control"
              onChange={(value) => setRunnerFilter(value)}
              options={[
                { label: "全部节点", value: "" },
                ...runners.map((runner) => ({ label: runner.name || runner.runner_id, value: runner.runner_id }))
              ]}
              aria-label="按执行节点筛选任务"
            />
          </div>
          <Space wrap>
            <Button icon={<FilterX size={16} />} onClick={resetFilters} disabled={!hasFilters}>
              清空筛选
            </Button>
            <Button icon={<RefreshCw size={16} />} onClick={load} loading={loading}>
              刷新
            </Button>
          </Space>
        </div>

        <div className="project-result-strip">
          <span>当前结果</span>
          <strong>{summary.visible}</strong>
          <span>条</span>
          {statusFilter && <Tag>{taskStatusLabel(statusFilter)}</Tag>}
          {enabledFilter && <Tag>{enabledFilter === "enabled" ? "已启用" : "已禁用"}</Tag>}
          {tagFilter && <Tag>标签：{tagFilter}</Tag>}
          {runnerFilter && <Tag>执行节点：{runnerName(runnerFilter, runners)}</Tag>}
          {focusedCheckId && (
            <Tag
              closable
              onClose={(event) => {
                event.preventDefault();
                clearTaskFocus();
              }}
            >
              任务 #{focusedCheckId}
            </Tag>
          )}
          {query && <Tag>关键词：{query}</Tag>}
        </div>

        {selectedCount > 0 && (
          <div className="project-selection-strip" aria-label="已选任务操作">
            <span>
              已选择 <strong>{selectedCount}</strong> 条任务
            </span>
            <Space wrap>
              <Button icon={<Play size={16} />} loading={busy === "batch"} disabled={readOnly || selectedEnabledIds.length === 0} onClick={() => requestBatchAction("run")}>
                批量执行
              </Button>
              <Dropdown
                trigger={["click"]}
                placement="bottomRight"
                menu={{
                  items: selectedStateActionItems,
                  onClick: ({ key }) => requestBatchAction(key as CheckBatchAction)
                }}
              >
                <Button icon={<MoreHorizontal size={16} />} loading={busy === "batch"}>
                  启用/禁用
                </Button>
              </Dropdown>
              <Button onClick={() => setSelectedCheckIds([])}>取消选择</Button>
            </Space>
          </div>
        )}

        {isCompactList ? (
          <CompactCheckList
            busy={busy}
            checks={filtered}
            hasFilters={hasFilters}
            loading={loading}
            readOnly={readOnly}
            runners={runners}
            type={type}
            onCreate={openCreateDrawer}
            onDelete={confirmRemove}
            onDuplicate={duplicate}
            onEdit={(check) => {
              setEditing(check);
              setDrawerOpen(true);
            }}
            onHistory={(check) => navigate(`/runs?check_id=${check.id}`)}
            onOpenLastRun={(check) => check.last_run_id && setDetailRunId(check.last_run_id)}
            onRun={runCheck}
            onConfirmRecovery={confirmRecovery}
            onToggle={toggle}
            duplicatingId={duplicatingId}
            selectedIds={selectedCheckIds}
            onToggleSelection={(checkId, selected) =>
              setSelectedCheckIds((current) => (selected ? Array.from(new Set([...current, checkId])) : current.filter((id) => id !== checkId)))
            }
          />
        ) : (
          <Table
            rowKey="id"
            columns={columns}
            dataSource={filtered}
            loading={loading}
            rowSelection={{
              selectedRowKeys: selectedCheckIds,
              onChange: (keys) => setSelectedCheckIds(keys.map((key) => Number(key)).filter((key) => Number.isFinite(key)))
            }}
            pagination={{ pageSize: 10, showSizeChanger: false, itemRender: checkPaginationItemRender }}
            locale={{
              emptyText: (
                <Empty description={hasFilters ? "没有符合筛选条件的任务" : "暂无任务"}>
                  {!hasFilters && <Button type="primary" icon={<Plus size={16} />} onClick={openCreateDrawer} disabled={readOnly}>新增任务</Button>}
                </Empty>
              )
            }}
            className="checks-table project-data-table"
            scroll={{ x: type === "api" ? 1580 : 1480 }}
          />
        )}
      </Card>

      {drawerOpen && (
        <Suspense fallback={null}>
          <CheckEditorDrawer open={drawerOpen} type={type} check={editing} onClose={() => setDrawerOpen(false)} onSaved={load} />
        </Suspense>
      )}
      {detailRunId && (
        <Suspense fallback={null}>
          <RunDetailDrawer
            runId={detailRunId}
            onClose={() => setDetailRunId(null)}
            returnTo={`${location.pathname}${location.search}`}
            onRerun={readOnly ? undefined : async (run) => {
              const latest = await api.rerun(run.id);
              setDetailRunId(latest.id);
              await load();
            }}
          />
        </Suspense>
      )}
    </div>
  );
}

interface CompactCheckListProps {
  busy: BusyState;
  checks: Check[];
  duplicatingId: number | null;
  hasFilters: boolean;
  loading: boolean;
  readOnly: boolean;
  runners: ProbeRunner[];
  type: CheckType;
  onCreate: () => void;
  onDelete: (check: Check) => void;
  onDuplicate: (check: Check) => void;
  onEdit: (check: Check) => void;
  onHistory: (check: Check) => void;
  onOpenLastRun: (check: Check) => void;
  onRun: (check: Check) => void;
  onConfirmRecovery: (check: Check) => void;
  onToggle: (check: Check) => void;
  selectedIds: number[];
  onToggleSelection: (checkId: number, selected: boolean) => void;
}

function CompactCheckList({
  busy,
  checks,
  duplicatingId,
  hasFilters,
  loading,
  readOnly,
  runners,
  type,
  onCreate,
  onDelete,
  onDuplicate,
  onEdit,
  onHistory,
  onOpenLastRun,
  onRun,
  onConfirmRecovery,
  onToggle,
  selectedIds,
  onToggleSelection
}: CompactCheckListProps) {
  if (loading) {
    return (
      <section className="check-card-list" aria-label="任务列表加载中">
        {[0, 1, 2].map((item) => (
          <article className="check-card check-card-loading" key={item}>
            <Skeleton active paragraph={{ rows: 4 }} title={{ width: "72%" }} />
          </article>
        ))}
      </section>
    );
  }

  if (!checks.length) {
    return (
      <section className="check-card-empty">
        <Empty description={hasFilters ? "没有符合筛选条件的任务" : "暂无任务"}>
          {!hasFilters && (
            <Button type="primary" icon={<Plus size={16} />} onClick={onCreate} disabled={readOnly}>
              新增任务
            </Button>
          )}
        </Empty>
      </section>
    );
  }

  return (
    <section className="check-card-list" aria-label="任务列表">
      {checks.map((check) => {
        const status = taskStatus(check);
        const selected = selectedIds.includes(check.id);
        return (
          <article className={`check-card check-card-${status}`} key={check.id}>
            <header className="check-card-header">
              <div className="check-card-main">
                <div className="check-card-context">
                  <Checkbox checked={selected} aria-label={`选择 ${check.name}`} onChange={(event) => onToggleSelection(check.id, event.target.checked)} />
                  <span>{type === "ui" ? "UI 任务" : "接口任务"}</span>
                  {type === "api" && check.method && <Tag>{check.method}</Tag>}
                </div>
                {check.last_run_id ? (
                  <Button type="link" className="check-card-title" onClick={() => onOpenLastRun(check)}>
                    {check.name}
                  </Button>
                ) : (
                  <span className="check-card-title-static">{check.name}</span>
                )}
                <div className="check-card-url" title={check.entry_url}>
                  {compactUrl(check.entry_url)}
                </div>
                {check.tags && <TagLine tags={check.tags} />}
              </div>
              <div className="check-card-state">
                <TaskStatusTag check={check} />
                <label className="check-card-enable">
                  <span>{check.enabled ? "启用" : "禁用"}</span>
                  <Switch
                    aria-label={`${check.name} 启用状态`}
                    checked={check.enabled}
                    loading={busy === check.id}
                    disabled={readOnly}
                    onChange={() => onToggle(check)}
                  />
                </label>
              </div>
            </header>

            <div className="check-card-meta">
              <CheckMeta label="定时" value={intervalLabel(check.interval_seconds)} />
              <CheckMeta label="执行节点" value={runnerStrategyLabel(check, runners)} />
              <CheckMeta label="最近执行" value={formatDate(check.last_run_at)} />
              <CheckMeta label="耗时" value={formatDuration(check.last_duration_ms)} />
              <CheckMeta label="连续失败" value={check.consecutive_failures || "-"} />
            </div>

            <Space className="check-card-actions">
              <Button icon={<Play size={15} />} loading={busy === check.id} disabled={readOnly} onClick={() => onRun(check)}>
                人工验证
              </Button>
              {["failing", "suspected_failing", "suspected_recovery", "stale", "unknown"].includes(status) && (
                <Button icon={<RefreshCw size={15} />} loading={busy === check.id} disabled={readOnly} onClick={() => onConfirmRecovery(check)}>
                  确认恢复
                </Button>
              )}
              <Button icon={<Edit3 size={15} />} disabled={readOnly} onClick={() => onEdit(check)}>
                编辑
              </Button>
              <Button icon={<History size={15} />} onClick={() => onHistory(check)}>
                历史
              </Button>
              <Dropdown
                trigger={["click"]}
                placement="bottomRight"
                menu={{
                  items: checkSecondaryActionItems(duplicatingId === check.id, readOnly),
                  onClick: ({ key }) => {
                    if (key === "duplicate") onDuplicate(check);
                    if (key === "delete") onDelete(check);
                  }
                }}
              >
                <Button icon={<MoreHorizontal size={15} />} aria-label={`${check.name} 更多操作`}>
                  更多
                </Button>
              </Dropdown>
            </Space>
          </article>
        );
      })}
    </section>
  );
}

function CheckMeta({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="meta-field meta-field-card">
      <span>{label}</span>
      <span>{value}</span>
    </div>
  );
}

function TagLine({ tags }: { tags: string }) {
  const items = checkTagTokens(tags, false);

  if (!items.length) return null;

  return (
    <div className="check-card-tags">
      {items.map((tag) => (
        <Tag key={tag}>{tag}</Tag>
      ))}
    </div>
  );
}

function runnerStrategyLabel(check: Check, runners: ProbeRunner[]): string {
  if (check.runner_selection_mode === "round_robin_all") return "轮询所有启用节点";
  const ids = check.runner_ids?.length ? check.runner_ids : ["local"];
  const labels = ids.map((id) => runnerName(id, runners));
  if (labels.length <= 2) return labels.join("、");
  return `${labels.slice(0, 2).join("、")} 等 ${labels.length} 个`;
}

function runnerName(runnerId: string, runners: ProbeRunner[]): string {
  return runners.find((runner) => runner.runner_id === runnerId)?.name || runnerId;
}

function checkTagTokens(tags: string | null | undefined, normalize = true): string[] {
  return (tags || "")
    .split(/[,\s]+/)
    .map((tag) => {
      const text = tag.trim();
      return normalize ? text.toLowerCase() : text;
    })
    .filter(Boolean);
}
