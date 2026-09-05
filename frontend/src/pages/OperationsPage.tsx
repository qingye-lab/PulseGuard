import { App, Button, Card, Descriptions, Drawer, Empty, Select, Skeleton, Space, Table, Tabs, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Eye, RefreshCw, RotateCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useProject } from "../ProjectContext";
import { StructuredViewer } from "../components/StructuredViewer";
import type { AuditEvent, Check, CheckVersion, DatabaseHealth, ProbeRunner, RunArchive } from "../types";
import { compactUrl, formatDate, formatDuration, runStatusLabel, runStatusTagColor } from "../utils";

const { Text } = Typography;

type DetailDrawer = { title: string; value: unknown } | null;

export function OperationsPage() {
  const { message, modal } = App.useApp();
  const { currentProject } = useProject();
  const readOnly = currentProject.status === "archived";
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [archives, setArchives] = useState<RunArchive[]>([]);
  const [runners, setRunners] = useState<ProbeRunner[]>([]);
  const [checks, setChecks] = useState<Check[]>([]);
  const [databaseHealth, setDatabaseHealth] = useState<DatabaseHealth | null>(null);
  const [selectedCheckId, setSelectedCheckId] = useState<number | null>(null);
  const [versions, setVersions] = useState<CheckVersion[]>([]);
  const [loading, setLoading] = useState(true);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [restoringId, setRestoringId] = useState<number | null>(null);
  const [drawer, setDrawer] = useState<DetailDrawer>(null);

  const selectedCheck = useMemo(
    () => checks.find((check) => check.id === selectedCheckId) || null,
    [checks, selectedCheckId]
  );

  async function loadOperations() {
    setLoading(true);
    try {
      const [events, nextArchives, nextRunners, uiChecks, apiChecks, nextDatabaseHealth] = await Promise.all([
        api.auditEvents(),
        api.runArchives(),
        api.runners(),
        api.checks("ui"),
        api.checks("api"),
        api.databaseHealth()
      ]);
      const nextChecks = [...uiChecks, ...apiChecks].sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
      setAuditEvents(events);
      setArchives(nextArchives);
      setRunners(nextRunners);
      setChecks(nextChecks);
      setDatabaseHealth(nextDatabaseHealth);
      setSelectedCheckId((current) => {
        if (current && nextChecks.some((check) => check.id === current)) return current;
        return nextChecks[0]?.id ?? null;
      });
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function loadVersions(checkId = selectedCheckId) {
    if (!checkId) {
      setVersions([]);
      return;
    }
    setVersionsLoading(true);
    try {
      setVersions(await api.checkVersions(checkId));
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setVersionsLoading(false);
    }
  }

  useEffect(() => {
    void loadOperations();
  }, []);

  useEffect(() => {
    void loadVersions();
  }, [selectedCheckId]);

  function restoreVersion(version: CheckVersion) {
    modal.confirm({
      title: "恢复任务版本",
      content: `将 ${selectedCheck?.name || `#${version.check_id}`} 恢复到 ${formatDate(version.created_at)} 的版本。`,
      okText: "恢复",
      cancelText: "取消",
      onOk: async () => {
        setRestoringId(version.id);
        try {
          const restored = await api.restoreCheckVersion(version.id);
          message.success(`已恢复 ${restored.name}`);
          await loadOperations();
          await loadVersions(restored.id);
          setSelectedCheckId(restored.id);
        } catch (err) {
          message.error((err as Error).message);
        } finally {
          setRestoringId(null);
        }
      }
    });
  }

  const auditColumns: ColumnsType<AuditEvent> = [
    {
      title: "时间",
      dataIndex: "created_at",
      width: 180,
      render: (value: string) => formatDate(value)
    },
    {
      title: "动作",
      dataIndex: "action",
      width: 120,
      render: (value: string) => <Tag color={auditActionColor(value)}>{auditActionLabel(value)}</Tag>
    },
    {
      title: "对象",
      render: (_, event) => (
        <Space size={6} wrap>
          <Tag>{entityTypeLabel(event.entity_type)}</Tag>
          <Text>{event.entity_name || event.entity_id || "-"}</Text>
        </Space>
      )
    },
    {
      title: "摘要",
      dataIndex: "summary",
      render: (value: string) => value || "-"
    },
    {
      title: "详情",
      width: 96,
      align: "right",
      render: (_, event) => (
        <Tooltip title="查看审计载荷">
          <Button
            icon={<Eye size={16} />}
            onClick={() => setDrawer({ title: `审计事件 #${event.id}`, value: auditEventDetail(event) })}
            aria-label={`查看审计事件 ${event.id}`}
          />
        </Tooltip>
      )
    }
  ];

  const versionColumns: ColumnsType<CheckVersion> = [
    {
      title: "时间",
      dataIndex: "created_at",
      width: 180,
      render: (value: string) => formatDate(value)
    },
    {
      title: "动作",
      dataIndex: "action",
      width: 140,
      render: (value: string) => <Tag color={versionActionColor(value)}>{versionActionLabel(value)}</Tag>
    },
    {
      title: "任务快照",
      render: (_, version) => (
        <div className="version-snapshot-cell">
          <Text strong>{String(version.snapshot.name || `#${version.check_id}`)}</Text>
          <Text type="secondary">{snapshotSubtitle(version.snapshot)}</Text>
        </div>
      )
    },
    {
      title: "操作",
      width: 170,
      align: "right",
      render: (_, version) => (
        <Space size={8}>
          <Tooltip title="查看版本快照">
            <Button
              icon={<Eye size={16} />}
              onClick={() => setDrawer({ title: `版本 #${version.id}`, value: checkVersionDetail(version) })}
              aria-label={`查看版本 ${version.id}`}
            />
          </Tooltip>
          <Tooltip title="恢复这个版本">
            <Button
              icon={<RotateCcw size={16} />}
              loading={restoringId === version.id}
              disabled={readOnly}
              onClick={() => restoreVersion(version)}
              aria-label={`恢复版本 ${version.id}`}
            />
          </Tooltip>
        </Space>
      )
    }
  ];

  const archiveColumns: ColumnsType<RunArchive> = [
    {
      title: "日期",
      dataIndex: "archive_date",
      width: 130
    },
    {
      title: "类型",
      dataIndex: "check_type",
      width: 100,
      render: (value: string) => <Tag>{value === "ui" ? "UI" : "API"}</Tag>
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 120,
      render: (value: RunArchive["status"]) => <Tag color={runStatusTagColor(value)}>{runStatusLabel(value)}</Tag>
    },
    {
      title: "运行数",
      dataIndex: "run_count",
      width: 110
    },
    {
      title: "平均耗时",
      render: (_, archive) => formatDuration(averageDuration(archive))
    },
    {
      title: "最后运行",
      dataIndex: "last_run_at",
      render: (value: string | null) => formatDate(value)
    }
  ];

  const runnerColumns: ColumnsType<ProbeRunner> = [
    {
      title: "执行节点",
      dataIndex: "name",
      render: (value: string, runner) => (
        <div className="runner-cell">
          <Text strong>{value || runner.runner_id}</Text>
          <Text type="secondary">{runner.address || "-"}</Text>
        </div>
      )
    },
    {
      title: "网络区域",
      dataIndex: "network_region",
      width: 140,
      render: (value: string) => <Tag>{value || "local"}</Tag>
    },
    {
      title: "启用",
      dataIndex: "enabled",
      width: 92,
      render: (value: boolean) => <Tag color={value ? "success" : "default"}>{value ? "已启用" : "已停用"}</Tag>
    },
    {
      title: "可用",
      dataIndex: "available",
      width: 92,
      render: (value: boolean) => <Tag color={value ? "success" : "error"}>{value ? "可用" : "不可用"}</Tag>
    },
    {
      title: "关联任务",
      width: 100,
      render: (_, runner) => assignedRunnerCount(runner, checks)
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value: ProbeRunner["status"]) => <Tag color={runnerStatusColor(value)}>{runnerStatusLabel(value)}</Tag>
    },
    {
      title: "Browser Type",
      dataIndex: "installed_browser_types",
      width: 180,
      render: (value: string[] | undefined, runner) => {
        const types = value && value.length ? value : runner.browser_version ? [runner.browser_version] : [];
        return types.length ? (
          <Space size={4} wrap>
            {types.map((item) => (
              <Tag key={item}>{item}</Tag>
            ))}
          </Space>
        ) : (
          "-"
        );
      }
    },
    {
      title: "最后心跳",
      dataIndex: "last_seen_at",
      width: 180,
      render: (value: string) => formatDate(value)
    },
    {
      title: "详情",
      width: 96,
      align: "right",
      render: (_, runner) => (
        <Tooltip title="查看执行节点元数据">
          <Button
            icon={<Eye size={16} />}
            onClick={(event) => {
              event.stopPropagation();
              openRunnerDetail(runner);
            }}
            aria-label={`查看执行节点 ${runner.runner_id}`}
          />
        </Tooltip>
      )
    }
  ];

  function openRunnerDetail(runner: ProbeRunner) {
    setDrawer({ title: `执行节点 ${runner.runner_id}`, value: runnerDetail(runner) });
  }

  if (loading) {
    return (
      <div className="page-content operations-page">
        <Skeleton active paragraph={{ rows: 10 }} />
      </div>
    );
  }

  return (
    <div className="page-content operations-page">
      <Card
        title="运行与审计"
        className="project-tab-card operations-workspace-card"
        extra={
          <Button icon={<RefreshCw size={16} />} onClick={loadOperations}>
            刷新
          </Button>
        }
      >
        <Tabs
          className="operations-tabs"
          items={[
          {
            key: "runners",
            label: "执行节点状态",
            children:
              runners.length === 0 ? (
                <Empty description="暂无执行节点心跳" />
              ) : (
                <Table
                  rowKey="runner_id"
                  columns={runnerColumns}
                  dataSource={runners}
                  pagination={{ pageSize: 12 }}
                  rowClassName="operations-runner-row"
                  onRow={(runner) => ({
                    onClick: () => openRunnerDetail(runner)
                  })}
                />
              )
          },
          {
            key: "audit",
            label: "操作审计",
            children:
              auditEvents.length === 0 ? (
                <Empty description="暂无审计事件" />
              ) : (
                <Table rowKey="id" columns={auditColumns} dataSource={auditEvents} pagination={{ pageSize: 12 }} />
              )
          },
          {
            key: "archives",
            label: "归档摘要",
            children:
              archives.length === 0 ? (
                <Empty description="暂无归档摘要" />
              ) : (
                <Table rowKey="id" columns={archiveColumns} dataSource={archives} pagination={{ pageSize: 12 }} />
              )
          },
          {
            key: "database",
            label: "数据库状态",
            children: databaseHealth ? (
              <div className="database-health-panel">
                <Descriptions
                  className="database-health-descriptions"
                  bordered
                  size="small"
                  column={{ xs: 1, sm: 2, lg: 3 }}
                  items={[
                    {
                      key: "status",
                      label: "维护状态",
                      children: (
                        <Tag color={databaseHealth.maintenance_recommended ? "warning" : "success"}>
                          {databaseHealth.maintenance_recommended ? "建议安排维护" : "状态正常"}
                        </Tag>
                      )
                    },
                    {
                      key: "file-size",
                      label: "数据库文件",
                      children: formatStorageSize(databaseHealth.file_size_bytes)
                    },
                    {
                      key: "used-size",
                      label: "有效数据估算",
                      children: formatStorageSize(databaseHealth.estimated_used_bytes)
                    },
                    {
                      key: "reclaimable",
                      label: "可回收空间",
                      children: `${formatStorageSize(databaseHealth.reclaimable_bytes)} · ${(databaseHealth.reclaimable_ratio * 100).toFixed(1)}%`
                    },
                    {
                      key: "wal-size",
                      label: "WAL 日志",
                      children: formatStorageSize(databaseHealth.wal_size_bytes)
                    },
                    {
                      key: "journal-mode",
                      label: "日志模式",
                      children: databaseHealth.journal_mode.toUpperCase()
                    }
                  ]}
                />
                <Text type={databaseHealth.maintenance_recommended ? "warning" : "secondary"}>
                  {databaseHealth.maintenance_recommended
                    ? "空闲页占比较高。压缩前请先创建数据库备份，并在停止写入的维护窗口执行。"
                    : "当前空闲页占比无需安排数据库压缩。"}
                </Text>
              </div>
            ) : (
              <Empty description="暂无数据库状态" />
            )
          },
          {
            key: "versions",
            label: "任务版本",
            children: (
              <div className="versions-panel">
                <Space wrap className="versions-toolbar">
                  <Select
                    className="version-check-select"
                    placeholder="选择任务"
                    value={selectedCheckId ?? undefined}
                    onChange={(value) => setSelectedCheckId(value)}
                    options={checks.map((check) => ({
                      label: `${check.name} · ${check.type === "ui" ? "UI" : "API"} · ${compactUrl(check.entry_url || "-")}`,
                      value: check.id
                    }))}
                  />
                  <Button icon={<RefreshCw size={16} />} onClick={() => loadVersions()}>
                    刷新版本
                  </Button>
                </Space>
                {selectedCheckId ? (
                  <Table
                    rowKey="id"
                    columns={versionColumns}
                    dataSource={versions}
                    loading={versionsLoading}
                    pagination={{ pageSize: 10 }}
                  />
                ) : (
                  <Empty description="暂无任务" />
                )}
              </div>
            )
          }
          ]}
        />
      </Card>

      <Drawer title={drawer?.title} open={Boolean(drawer)} onClose={() => setDrawer(null)} size="large">
        <StructuredViewer value={drawer?.value} defaultMode="json" />
      </Drawer>
    </div>
  );
}

function auditActionLabel(value: string): string {
  return (
    {
      created: "创建",
      updated: "更新",
      deleted: "删除",
      enabled: "启用",
      disabled: "禁用",
      imported: "导入",
      restored: "恢复",
      enable: "批量启用",
      disable: "批量停用",
      update_interval: "批量改频"
    }[value] || "其他"
  );
}

function formatStorageSize(value: number): string {
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && size >= 1024; index += 1) {
    size /= 1024;
    unit = units[index];
  }
  return `${size.toFixed(size >= 100 ? 0 : size >= 10 ? 1 : 2)} ${unit}`;
}

function auditActionColor(value: string): string {
  if (value === "deleted" || value === "disabled" || value === "disable") return "warning";
  if (value === "created" || value === "enabled" || value === "enable") return "success";
  if (value === "imported" || value === "restored") return "blue";
  return "processing";
}

function versionActionLabel(value: string): string {
  return (
    {
      created: "创建",
      updated: "更新前",
      deleted: "删除前",
      "restored-from": "恢复前"
    }[value] || "其他"
  );
}

function entityTypeLabel(value: string): string {
  return (
    {
      check: "任务",
      config: "配置",
      settings: "设置",
      batch: "批量操作",
      runner: "执行节点"
    }[value] || "其他"
  );
}

function auditEventDetail(event: AuditEvent): Record<string, unknown> {
  return {
    时间: formatDate(event.created_at),
    动作: auditActionLabel(event.action),
    对象: entityTypeLabel(event.entity_type),
    对象名称: event.entity_name || event.entity_id || "-",
    摘要: event.summary || "-",
    载荷摘要: auditPayloadSummary(event.payload)
  };
}

function auditPayloadSummary(payload: unknown): Record<string, unknown> | string {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return "无";
  const value = payload as Record<string, unknown>;
  const summary: Record<string, unknown> = {};
  for (const key of ["matched_count", "updated_count", "created_count", "deleted_count", "skipped_count"]) {
    if (typeof value[key] === "number") summary[countLabel(key)] = value[key];
  }
  if (Array.isArray(value.check_ids)) summary["任务数量"] = value.check_ids.length;
  if (Array.isArray(value.errors)) summary["错误数量"] = value.errors.length;
  if (typeof value.replace_existing === "boolean") summary["替换同名任务"] = value.replace_existing ? "是" : "否";
  return Object.keys(summary).length ? summary : "已记录";
}

function countLabel(key: string): string {
  return (
    {
      matched_count: "命中数量",
      updated_count: "更新数量",
      created_count: "创建数量",
      deleted_count: "删除数量",
      skipped_count: "跳过数量"
    }[key] || "数量"
  );
}

function checkVersionDetail(version: CheckVersion): Record<string, unknown> {
  const snapshot = version.snapshot || {};
  return {
    任务名称: String(snapshot.name || `#${version.check_id}`),
    类型: snapshot.type === "ui" ? "UI" : snapshot.type === "api" ? "API" : "-",
    动作: versionActionLabel(version.action),
    记录时间: formatDate(version.created_at),
    启用状态: snapshot.enabled === false ? "停用" : "启用",
    入口: String(snapshot.entry_url || "-"),
    方法: String(snapshot.method || "-"),
    标签: String(snapshot.tags || "-"),
    执行频率: snapshot.interval_seconds || "-",
    超时时间: snapshot.timeout_ms || "-"
  };
}

function assignedRunnerCount(runner: ProbeRunner, checks: Check[]): number {
  return checks.filter((check) => {
    if (check.runner_selection_mode === "round_robin_all") return runner.enabled;
    const ids = check.runner_ids?.length ? check.runner_ids : ["local"];
    return ids.includes(runner.runner_id);
  }).length;
}

function runnerDetail(runner: ProbeRunner): Record<string, unknown> {
  return {
    执行节点: runner.name || runner.runner_id,
    "节点 ID": runner.runner_id,
    地址: runner.address || "-",
    网络区域: runner.network_region || "local",
    角色: runner.role === "local" ? "本机" : "子节点",
    启用状态: runner.enabled ? "已启用" : "已停用",
    可用状态: runner.available ? "可用" : "不可用",
    状态: runnerStatusLabel(runner.status),
    浏览器: runner.browser_version || "-",
    "Browser Type": (runner.installed_browser_types || []).join(", ") || "-",
    "Available Browser Type": (runner.available_browser_types || []).join(", ") || "-",
    "Browser Type Status": runner.browser_type_status || {},
    最后心跳: formatDate(runner.last_seen_at),
    更新时间: formatDate(runner.updated_at),
    元数据字段数: Object.keys(runner.metadata || {}).length
  };
}

function versionActionColor(value: string): string {
  if (value === "deleted") return "warning";
  if (value === "created") return "success";
  if (value === "restored-from") return "blue";
  return "processing";
}

function snapshotSubtitle(snapshot: Record<string, unknown>): string {
  const method = String(snapshot.method || "").trim();
  const entryUrl = String(snapshot.entry_url || "").trim();
  return [method, entryUrl ? compactUrl(entryUrl) : ""].filter(Boolean).join(" · ") || "-";
}

function runnerStatusLabel(value: string): string {
  return (
    {
      pending_deployment: "待部署",
      expired: "已过期",
      connecting: "连接中",
      available: "可用",
      unhealthy: "异常",
      unavailable: "异常",
      auth_failed: "认证失败",
      ok: "正常",
      warning: "告警",
      offline: "离线",
      disabled: "已停用"
    }[value] || "未知"
  );
}

function runnerStatusColor(value: string): string {
  if (value === "ok" || value === "available") return "success";
  if (value === "warning" || value === "expired" || value === "auth_failed") return "warning";
  if (value === "pending_deployment" || value === "connecting") return "processing";
  if (value === "unhealthy" || value === "unavailable") return "error";
  if (value === "offline" || value === "disabled") return "default";
  return "default";
}

function averageDuration(archive: RunArchive): number | null {
  if (!archive.duration_sample_count) return null;
  return Math.round(archive.duration_sum_ms / archive.duration_sample_count);
}
