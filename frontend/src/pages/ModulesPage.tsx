import { Alert, App as AntApp, Button, Drawer, Empty, Form, Input, Modal, Popconfirm, Space, Spin, Table, Tabs, Typography } from "antd";
import type { TableColumnsType } from "antd";
import { BookOpen, Copy, Plus, Save, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { useProject } from "../ProjectContext";
import { LazyCodeEditorPanel } from "../components/LazyCodeEditorPanel";
import type { ModuleLibrary, ProjectModule } from "../types";
import { copyTextToClipboard } from "../utils";
import "./ModulesPage.css";

const { Text } = Typography;

const MODULE_EXAMPLE = `def make_headers(token):
    return {"Authorization": f"Bearer {token}"}

async def request_with_token(ctx, token):
    return await ctx.request(headers=make_headers(token))
`;

const USAGE_EXAMPLE = `async def check(ctx):
    helpers = ctx.modules.use("auth_helpers")
    token = ctx.vars.get("TOKEN")
    await helpers.request_with_token(ctx, token)
`;

type ModuleForm = {
  name: string;
  description: string;
  source: string;
};

const EMPTY_MODULE: ModuleForm = {
  name: "",
  description: "",
  source: "def method(ctx):\n    return True\n"
};

function effectiveModules(library: ModuleLibrary | null): ProjectModule[] {
  return library?.effective_modules ?? [];
}

function effectiveRevision(library: ModuleLibrary | null): number {
  return library?.effective_revision ?? 0;
}

function moduleFormFrom(module: ProjectModule): ModuleForm {
  return {
    name: module.name,
    description: module.description || "",
    source: module.source
  };
}

export function ModulesPage() {
  const { message } = AntApp.useApp();
  const { currentProject } = useProject();
  const projectId = currentProject.id;
  const readOnly = currentProject.status === "archived";
  const [library, setLibrary] = useState<ModuleLibrary | null>(null);
  const [modules, setModules] = useState<ProjectModule[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [tutorialOpen, setTutorialOpen] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [moduleForm, setModuleForm] = useState<ModuleForm>(EMPTY_MODULE);
  const [editorDirty, setEditorDirty] = useState(false);
  const [deleteConfirmName, setDeleteConfirmName] = useState<string | null>(null);
  const requestGenerationRef = useRef(0);
  const activeProjectIdRef = useRef(projectId);

  const loadLibrary = useCallback(async () => {
    const generation = requestGenerationRef.current;
    const isCurrent = () => activeProjectIdRef.current === projectId && requestGenerationRef.current === generation;
    setLoading(true);
    setError(null);
    try {
      const nextLibrary = await api.library(projectId);
      if (!isCurrent()) return;
      if (!Array.isArray(nextLibrary.effective_modules) || typeof nextLibrary.effective_revision !== "number") {
        throw new Error("公共方法服务返回的生效库格式无效");
      }
      setLibrary(nextLibrary);
      setModules(effectiveModules(nextLibrary));
    } catch (reason) {
      if (!isCurrent()) return;
      setLibrary(null);
      setModules([]);
      setError((reason as Error).message || "公共方法加载失败");
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    activeProjectIdRef.current = projectId;
    requestGenerationRef.current += 1;
    setSaving(false);
    setLibrary(null);
    setModules([]);
    setError(null);
    setEditorOpen(false);
    setEditingName(null);
    setModuleForm({ ...EMPTY_MODULE });
    setEditorDirty(false);
    setDeleteConfirmName(null);
    void loadLibrary();
    return () => {
      requestGenerationRef.current += 1;
    };
  }, [loadLibrary, projectId]);

  const currentRevision = effectiveRevision(library);

  function openCreate() {
    if (readOnly) return;
    setError(null);
    setEditingName(null);
    setModuleForm({ ...EMPTY_MODULE });
    setEditorDirty(false);
    setEditorOpen(true);
  }

  function openEdit(module: ProjectModule) {
    if (readOnly) return;
    setError(null);
    setEditingName(module.name);
    setModuleForm(moduleFormFrom(module));
    setEditorDirty(false);
    setEditorOpen(true);
  }

  function closeEditor() {
    setEditorOpen(false);
    setEditingName(null);
    setModuleForm({ ...EMPTY_MODULE });
    setEditorDirty(false);
    setError(null);
  }

  function requestCloseEditor() {
    if (saving) return;
    if (!editorDirty) {
      closeEditor();
      return;
    }
    Modal.confirm({
      title: "放弃未保存内容？",
      content: "当前编辑内容还没有保存。",
      okText: "放弃编辑",
      cancelText: "继续编辑",
      onOk: closeEditor
    });
  }

  function showSaveError(reason: unknown) {
    if (reason instanceof ApiError && reason.status === 409) {
      setError("公共方法已被其他操作修改，请重新加载后再保存。");
      return;
    }
    setError((reason as Error).message || "公共方法保存失败");
  }

  async function saveModule() {
    if (readOnly || saving || !library) return;
    const name = moduleForm.name.trim();
    const source = moduleForm.source.trim();
    if (!name) {
      message.error("请输入方法名称");
      return;
    }
    if (!source) {
      message.error("请输入 Python 源码");
      return;
    }
    const duplicate = modules.some((module) => module.name === name && module.name !== editingName);
    if (duplicate) {
      message.error("方法名称已存在");
      return;
    }
    const nextModule: ProjectModule = {
      name,
      source: moduleForm.source,
      description: moduleForm.description.trim(),
      exports: []
    };
    const nextModules = editingName
      ? modules.map((module) => module.name === editingName ? nextModule : module)
      : [...modules, nextModule];
    const generation = requestGenerationRef.current;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.saveLibrary(projectId, {
        expected_revision: currentRevision,
        modules: nextModules.map(({ name: moduleName, source: moduleSource, description }) => ({
          name: moduleName,
          source: moduleSource,
          description: description || ""
        }))
      });
      if (requestGenerationRef.current !== generation || activeProjectIdRef.current !== projectId) return;
      setLibrary(saved);
      setModules(effectiveModules(saved));
      closeEditor();
      message.success(editingName ? "脚本已保存" : "脚本已创建");
    } catch (reason) {
      if (requestGenerationRef.current !== generation || activeProjectIdRef.current !== projectId) return;
      showSaveError(reason);
    } finally {
      if (requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setSaving(false);
    }
  }

  async function deleteModule(module: ProjectModule) {
    if (readOnly || saving || !library) return;
    const generation = requestGenerationRef.current;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.saveLibrary(projectId, {
        expected_revision: currentRevision,
        modules: modules
          .filter((item) => item.name !== module.name)
          .map(({ name, source, description }) => ({ name, source, description: description || "" }))
      });
      if (requestGenerationRef.current !== generation || activeProjectIdRef.current !== projectId) return;
      setLibrary(saved);
      setModules(effectiveModules(saved));
      message.success(`已删除脚本 ${module.name}`);
    } catch (reason) {
      if (requestGenerationRef.current !== generation || activeProjectIdRef.current !== projectId) return;
      showSaveError(reason);
    } finally {
      if (requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setSaving(false);
    }
  }

  async function copyReference(name: string) {
    const reference = `ctx.modules.use("${name}")`;
    try {
      await copyTextToClipboard(reference);
      message.success("引用已复制");
    } catch {
      Modal.info({
        title: "复制引用",
        content: <Input value={reference} readOnly autoFocus onFocus={(event) => event.currentTarget.select()} />,
        okText: "关闭"
      });
    }
  }

  const columns = useMemo<TableColumnsType<ProjectModule>>(() => [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      render: (name: string) => <Text code>{name}</Text>
    },
    {
      title: "说明",
      dataIndex: "description",
      key: "description",
      render: (description: string | null | undefined) => description ? <Text ellipsis={{ tooltip: description }}>{description}</Text> : <Text type="secondary">未填写</Text>
    },
    {
      title: "操作",
      key: "actions",
      width: 300,
      render: (_value, module) => (
        <Space size="small">
          <Button type="link" size="small" onClick={() => openEdit(module)} disabled={readOnly || saving}>编辑</Button>
          <Button type="link" size="small" icon={<Copy size={14} />} onClick={() => void copyReference(module.name)}>复制引用</Button>
          <Popconfirm
            title="删除公共方法？"
            description="如果任务仍在引用，保存会被拒绝。"
            okText="删除"
            cancelText="取消"
            open={deleteConfirmName === module.name}
            onOpenChange={(open) => setDeleteConfirmName(open ? module.name : null)}
            onConfirm={() => {
              setDeleteConfirmName(null);
              return deleteModule(module);
            }}
            disabled={readOnly || saving}
          >
            <Button type="link" danger size="small" icon={<Trash2 size={14} />} disabled={readOnly || saving}>删除</Button>
          </Popconfirm>
        </Space>
      )
    }
  ], [deleteConfirmName, modules, readOnly, saving, currentRevision, library, projectId]);

  return (
    <div className="page-content modules-page">
      <div className="modules-page-header">
        <Button icon={<BookOpen size={16} />} onClick={() => setTutorialOpen(true)}>
          使用教程
        </Button>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate} disabled={readOnly || loading || saving}>
          新建脚本
        </Button>
      </div>
      {error && (
        <div className="modules-page-error">
          <Text type="danger">{error}</Text>
          <Button type="link" size="small" onClick={() => void loadLibrary()} disabled={loading}>重新加载</Button>
        </div>
      )}
      {loading ? (
        <div className="modules-page-loading"><Spin /></div>
      ) : (
        <Table<ProjectModule>
          className="modules-page-table"
          rowKey="name"
          columns={columns}
          dataSource={modules}
          pagination={false}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无公共方法" /> }}
        />
      )}

      <Drawer
        title="公共方法使用教程"
        open={tutorialOpen}
        onClose={() => setTutorialOpen(false)}
        width={760}
        destroyOnHidden
      >
        <Tabs items={[
          {
            key: "write",
            label: "编写方法",
            children: (
              <div className="module-tutorial-content">
                <Typography.Paragraph>
                  点击“新建脚本”，填写脚本名称、说明和 Python 源码，再点击“保存”。一个脚本可以包含多个可复用函数。
                </Typography.Paragraph>
                <Typography.Paragraph>
                  例如，将脚本命名为 <Text code>auth_helpers</Text>，填入以下源码：
                </Typography.Paragraph>
                <Typography.Paragraph copyable={{ text: MODULE_EXAMPLE }}>复制编写示例</Typography.Paragraph>
                <pre className="code-block"><code>{MODULE_EXAMPLE}</code></pre>
                <Typography.Title level={5}>编写规则</Typography.Title>
                <ul>
                  <li>脚本名称用于引用，建议使用有意义的英文名称，如 auth_helpers。</li>
                  <li>顶层只写说明、字面量常量和函数定义；不要在顶层发请求或执行其他业务逻辑。</li>
                  <li>不支持 import、类或函数装饰器。需要请求、变量等能力时，通过函数参数传入 ctx。</li>
                  <li>默认可调用不以下划线开头的顶层函数，也可读取公开常量；内部辅助函数可用下划线开头。</li>
                  <li>公共方法内不能调用 ctx.modules.use() 引用其他脚本；需要组合多个脚本时，在任务入口中完成。</li>
                </ul>
                <Typography.Paragraph type="secondary">
                  保存后，同一项目的后续新运行会使用新源码，已经开始的运行不受影响。归档项目不可编辑。
                </Typography.Paragraph>
              </div>
            )
          },
          {
            key: "use",
            label: "使用方法",
            children: (
              <div className="module-tutorial-content">
                <Typography.Paragraph>
                  在同一项目的 API 或 UI 任务脚本中，使用 <Text code>ctx.modules.use("脚本名称")</Text> 获取公共方法，再通过“变量名.函数名”调用。列表中的“复制引用”可复制获取脚本的语句。
                </Typography.Paragraph>
                <Typography.Paragraph>
                  使用上一页的 auth_helpers 示例前，请先保存该脚本，并在项目变量中配置 TOKEN。下面的 API 任务示例会携带令牌请求任务配置的目标地址：
                </Typography.Paragraph>
                <Typography.Paragraph copyable={{ text: USAGE_EXAMPLE }}>复制调用示例</Typography.Paragraph>
                <pre className="code-block"><code>{USAGE_EXAMPLE}</code></pre>
                <Typography.Title level={5}>调用规则</Typography.Title>
                <ul>
                  <li>ctx.modules.use() 的脚本名称必须直接写成字符串，不能使用变量或拼接表达式。</li>
                  <li>按函数定义传入参数，ctx 也需要显式传入。同步函数直接调用，async def 定义的异步函数使用 await。</li>
                  <li>常量通过属性读取，不加括号，例如 helpers.DEFAULT_TIMEOUT（需先在公共脚本中定义该常量）。</li>
                  <li>UI 任务可在 check(ctx) 或前置脚本 setup(ctx) 中引用；公共方法仅在所属项目内可用。</li>
                </ul>
                <Typography.Paragraph type="secondary">
                  保存任务后，可通过调试或预览检查执行结果。若提示脚本或函数不存在，请核对当前项目、名称及公共方法是否已保存；删除或改名仍被任务引用的内容时，保存会被拒绝。
                </Typography.Paragraph>
              </div>
            )
          }
        ]} />
      </Drawer>

      <Drawer
        className="module-editor-drawer"
        title={editingName ? "编辑脚本" : "新建脚本"}
        open={editorOpen}
        onClose={requestCloseEditor}
        destroyOnHidden
        width={760}
        footer={(
          <Space>
            <Button onClick={requestCloseEditor} disabled={saving}>取消</Button>
            <Button type="primary" icon={<Save size={15} />} onClick={() => void saveModule()} loading={saving} disabled={readOnly}>
              保存
            </Button>
          </Space>
        )}
      >
        <Form layout="vertical" className="module-editor-form">
          {error && <Alert type="error" title={error} showIcon />}
          <Form.Item label="脚本名称" required>
            <Input
              value={moduleForm.name}
              autoFocus
              placeholder="例如 product_checks"
              onChange={(event) => {
                setModuleForm((current) => ({ ...current, name: event.target.value }));
                setEditorDirty(true);
              }}
            />
          </Form.Item>
          <Form.Item label="说明">
            <Input
              value={moduleForm.description}
              placeholder="简短说明这个方法的用途"
              onChange={(event) => {
                setModuleForm((current) => ({ ...current, description: event.target.value }));
                setEditorDirty(true);
              }}
            />
          </Form.Item>
          <div className="module-editor-source">
            <LazyCodeEditorPanel
              title="Python 源码"
              language="python"
              value={moduleForm.source}
              onChange={(source) => {
                setModuleForm((current) => ({ ...current, source }));
                setEditorDirty(true);
              }}
              height="calc(100vh - 330px)"
              completionScopeKey={`project:${projectId}:module-editor`}
            />
          </div>
        </Form>
      </Drawer>
    </div>
  );
}
