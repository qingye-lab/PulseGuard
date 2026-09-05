import { App as AntApp, Button, Card, Input, Modal, Popconfirm, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { Archive, ArchiveRestore, ArrowRightLeft, FolderKanban, PenLine, Plus, Trash2 } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
import { ProjectCreateModal } from "../components/ProjectCreateModal";
import { useProject } from "../ProjectContext";
import type { Project } from "../types";
import { formatDate } from "../utils";

const { Text } = Typography;

export function ProjectsPage() {
  const { message } = AntApp.useApp();
  const { projects, currentProject, selectProject, refreshProjects } = useProject();
  const [createOpen, setCreateOpen] = useState(false);
  const [busyProjectId, setBusyProjectId] = useState<number | null>(null);
  const [renamingProject, setRenamingProject] = useState<Project | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const counts = useMemo(
    () => ({
      active: projects.filter((project) => project.status === "active").length,
      archived: projects.filter((project) => project.status === "archived").length
    }),
    [projects]
  );

  async function mutateProject(projectId: number, action: () => Promise<unknown>, successMessage: string) {
    setBusyProjectId(projectId);
    try {
      await action();
      await refreshProjects(projectId);
      message.success(successMessage);
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setBusyProjectId(null);
    }
  }

  function startRename(project: Project) {
    setRenamingProject(project);
    setRenameValue(project.name);
  }

  async function renameProject() {
    const normalizedName = renameValue.trim();
    if (!renamingProject || !normalizedName) return;
    await mutateProject(
      renamingProject.id,
      () => api.renameProject(renamingProject.id, normalizedName),
      "项目名称已更新"
    );
    setRenamingProject(null);
  }

  function projectActions(project: Project): ReactNode {
    const busy = busyProjectId === project.id;
    return (
      <Space size={4} wrap className="project-row-actions">
        {project.id !== currentProject.id && (
          <Tooltip title="切换后所有项目数据将重新加载">
            <Button type="link" icon={<ArrowRightLeft size={15} />} onClick={() => selectProject(project.id)}>
              切换
            </Button>
          </Tooltip>
        )}
        <Button type="link" icon={<PenLine size={15} />} disabled={busy} onClick={() => startRename(project)}>
          重命名
        </Button>
        {project.status === "active" ? (
          <Button
            type="link"
            icon={<Archive size={15} />}
            loading={busy}
            onClick={() => void mutateProject(project.id, () => api.archiveProject(project.id), "项目已归档")}
          >
            归档
          </Button>
        ) : (
          <Button
            type="link"
            icon={<ArchiveRestore size={15} />}
            loading={busy}
            onClick={() => void mutateProject(project.id, () => api.restoreProject(project.id), "项目已恢复")}
          >
            恢复
          </Button>
        )}
        <Popconfirm
          title="删除这个项目？"
          description="只有没有任务、历史数据和只读令牌引用的项目可以删除。"
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={async () => {
            setBusyProjectId(project.id);
            try {
              await api.deleteProject(project.id);
              await refreshProjects();
              message.success("空项目已删除");
            } catch (error) {
              message.error((error as Error).message);
            } finally {
              setBusyProjectId(null);
            }
          }}
        >
          <Button type="link" danger icon={<Trash2 size={15} />} disabled={busy}>
            删除
          </Button>
        </Popconfirm>
      </Space>
    );
  }

  const columns: TableColumnsType<Project> = [
    {
      title: "项目",
      dataIndex: "name",
      key: "name",
      render: (_, project) => (
        <div className="project-name-cell">
          <span className="project-name-icon" aria-hidden="true"><FolderKanban size={16} /></span>
          <div>
            <Space size={6} wrap>
              <Text strong>{project.name}</Text>
              {project.id === currentProject.id && <Tag color="blue">当前</Tag>}
            </Space>
            <Text type="secondary">创建于 {formatDate(project.created_at)}</Text>
          </div>
        </div>
      )
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (status: Project["status"]) => status === "active" ? <Tag color="success">活跃</Tag> : <Tag>已归档</Tag>
    },
    {
      title: "最近更新",
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (value: string) => <span className="tabular-value">{formatDate(value)}</span>
    },
    {
      title: "操作",
      key: "actions",
      width: 360,
      render: (_, project) => projectActions(project)
    }
  ];

  return (
    <div className="page-content project-management-page">
      <Card
        className="project-management-card"
        title={
          <div className="project-management-summary">
            <strong>{projects.length} 个项目</strong>
            <span>{counts.active} 个活跃 · {counts.archived} 个已归档</span>
          </div>
        }
        extra={
          <Button type="primary" icon={<Plus size={16} />} onClick={() => setCreateOpen(true)}>
            新建项目
          </Button>
        }
      >
        <Table<Project>
          rowKey="id"
          size="middle"
          pagination={false}
          columns={columns}
          dataSource={projects}
          scroll={{ x: 820 }}
        />
      </Card>

      <ProjectCreateModal open={createOpen} onClose={() => setCreateOpen(false)} />
      <Modal
        title="重命名项目"
        open={Boolean(renamingProject)}
        okText="保存名称"
        cancelText="取消"
        confirmLoading={renamingProject ? busyProjectId === renamingProject.id : false}
        okButtonProps={{ disabled: !renameValue.trim() }}
        onCancel={() => setRenamingProject(null)}
        onOk={() => void renameProject()}
      >
        <Typography.Paragraph type="secondary">项目内的任务和历史数据不会改变。</Typography.Paragraph>
        <Typography.Text strong>项目名称</Typography.Text>
        <Input
          className="project-rename-input"
          value={renameValue}
          maxLength={80}
          showCount
          onChange={(event) => setRenameValue(event.target.value)}
          onPressEnter={() => void renameProject()}
        />
      </Modal>
    </div>
  );
}
