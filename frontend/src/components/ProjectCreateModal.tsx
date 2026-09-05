import { App as AntApp, Form, Input, Modal } from "antd";
import { useEffect, useState } from "react";
import { api } from "../api";
import { useProject } from "../ProjectContext";

type ProjectCreateModalProps = {
  open: boolean;
  onClose: () => void;
};

export function ProjectCreateModal({ open, onClose }: ProjectCreateModalProps) {
  const { message } = AntApp.useApp();
  const { refreshProjects } = useProject();
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) setName("");
  }, [open]);

  async function createProject() {
    const normalizedName = name.trim();
    if (!normalizedName || submitting) return;
    setSubmitting(true);
    try {
      const created = await api.createProject(normalizedName);
      await refreshProjects(created.id);
      message.success(`项目“${created.name}”已创建`);
      onClose();
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title="新建项目"
      open={open}
      okText="创建项目"
      cancelText="取消"
      confirmLoading={submitting}
      okButtonProps={{ disabled: !name.trim() }}
      onCancel={onClose}
      onOk={() => void createProject()}
      destroyOnHidden
    >
      <Form layout="vertical" requiredMark={false} className="project-create-form">
        <Form.Item label="项目名称" extra="创建后将立即切换到新项目。">
          <Input
            autoFocus
            value={name}
            maxLength={80}
            showCount
            placeholder="例如：商城核心链路"
            onChange={(event) => setName(event.target.value)}
            onPressEnter={() => void createProject()}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
