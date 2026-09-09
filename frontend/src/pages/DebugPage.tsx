import { Alert, Button, Card, Collapse, Empty, Form, Input, InputNumber, Segmented, Select, Skeleton, Space, Switch, Tag, Tooltip } from "antd";
import { ArrowLeft, Play, Save } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useProject } from "../ProjectContext";
import {
  checkToPayload,
  detectBodyMode,
  normalizeCheckPayload,
  prepareCheckPayload,
  sameCheckPayload,
  type BodyEditorMode
} from "../checkPayload";
import { apiScriptTemplate } from "../defaults";
import { ApiAssertionsBuilder } from "../components/ApiAssertionsBuilder";
import { LazyCodeEditorPanel as CodeEditorPanel } from "../components/LazyCodeEditorPanel";
import { RunResultPanel } from "../components/RunResultPanel";
import { UiAssertionsBuilder } from "../components/UiAssertionsBuilder";
import { UiScriptSections } from "../components/UiScriptSections";
import { ViewportModeControl } from "../components/ViewportModeControl";
import { moduleCompletionItems } from "../moduleLibrary";
import type { Check, CheckPayload, ModuleLibrary, Run } from "../types";
import { dirtyTagColor } from "../utils";

export function DebugPage() {
  const { currentProject } = useProject();
  const projectId = currentProject.id;
  const readOnly = currentProject.status === "archived";
  const { type, checkId } = useParams();
  const navigate = useNavigate();
  const checkType = type === "api" ? "api" : "ui";
  const id = Number(checkId);
  const [check, setCheck] = useState<Check | null>(null);
  const [form, setForm] = useState<CheckPayload | null>(null);
  const [baseline, setBaseline] = useState<CheckPayload | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"saved-run" | "draft-run" | "save" | null>(null);
  const [bodyMode, setBodyMode] = useState<BodyEditorMode>("json");
  const [moduleLibrary, setModuleLibrary] = useState<ModuleLibrary | null>(null);
  const requestGenerationRef = useRef(0);
  const activeProjectIdRef = useRef(projectId);

  const isDirty = useMemo(() => Boolean(form && baseline && !sameCheckPayload(form, baseline)), [baseline, form]);
  const moduleCompletions = useMemo(() => moduleCompletionItems(moduleLibrary), [moduleLibrary]);

  useEffect(() => {
    activeProjectIdRef.current = projectId;
    const generation = ++requestGenerationRef.current;
    let cancelled = false;
    setBusy(null);
    setCheck(null);
    setForm(null);
    setBaseline(null);
    setRun(null);
    setError(null);
    api
      .check(id)
      .then((data) => {
        if (cancelled || requestGenerationRef.current !== generation || activeProjectIdRef.current !== projectId) return;
        const payload = checkToPayload(data);
        setCheck(data);
        setForm(payload);
        setBaseline(normalizeCheckPayload(payload));
        setBodyMode(detectBodyMode(payload.body));
        setError(null);
      })
      .catch((err: Error) => {
        if (!cancelled && requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [id, projectId]);

  useEffect(() => {
    let cancelled = false;
    setModuleLibrary(null);
    api.library(projectId)
      .then((value) => {
        if (!cancelled) setModuleLibrary(value);
      })
      .catch(() => {
        if (!cancelled) setModuleLibrary(null);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, id]);

  async function save(): Promise<Check | null> {
    if (!form) return null;
    const generation = requestGenerationRef.current;
    try {
      const payload = prepareCheckPayload(form, { bodyMode });
      const saved = await api.updateCheck(id, payload);
      if (requestGenerationRef.current !== generation || activeProjectIdRef.current !== projectId) return null;
      const savedPayload = checkToPayload(saved);
      setCheck(saved);
      setForm(savedPayload);
      setBaseline(normalizeCheckPayload(savedPayload));
      setError(null);
      return saved;
    } catch (err) {
      if (requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setError((err as Error).message);
      return null;
    }
  }

  async function saveOnly() {
    const generation = requestGenerationRef.current;
    setBusy("save");
    try {
      await save();
    } finally {
      if (requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setBusy(null);
    }
  }

  async function runSaved() {
    const generation = requestGenerationRef.current;
    setBusy("saved-run");
    try {
      const result = await api.runCheck(id);
      if (requestGenerationRef.current !== generation || activeProjectIdRef.current !== projectId) return;
      setRun(result);
      setError(null);
    } catch (err) {
      if (requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setError((err as Error).message);
    } finally {
      if (requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setBusy(null);
    }
  }

  async function runDraft() {
    if (!form) return;
    const generation = requestGenerationRef.current;
    setBusy("draft-run");
    try {
      const payload = prepareCheckPayload(form, { bodyMode });
      const result = await api.debugCheck(payload);
      if (requestGenerationRef.current !== generation || activeProjectIdRef.current !== projectId) return;
      setRun(result);
      setError(null);
    } catch (err) {
      if (requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setError((err as Error).message);
    } finally {
      if (requestGenerationRef.current === generation && activeProjectIdRef.current === projectId) setBusy(null);
    }
  }

  function patchForm(values: Partial<CheckPayload>) {
    setForm((current) => (current ? { ...current, ...values } : current));
  }

  function formatJsonField(field: "headers_json" | "body", label: string) {
    if (!form) return;
    try {
      const text = field === "headers_json" ? form.headers_json : form.body;
      if (field === "body" && !text.trim()) {
        patchForm({ body: "" });
        setError(null);
        return;
      }
      const parsed = JSON.parse(text || "{}");
      if (field === "headers_json" && (!parsed || typeof parsed !== "object" || Array.isArray(parsed))) {
        throw new Error("Headers 必须是 JSON Object");
      }
      patchForm({ [field]: JSON.stringify(parsed, null, 2) } as Partial<CheckPayload>);
      setError(null);
    } catch (err) {
      setError(`${label} 不是合法 JSON：${(err as Error).message}`);
    }
  }

  if (!form || !check) {
    return (
      <div className="page-content">
        <Card>
          <Skeleton active paragraph={{ rows: 4 }} />
        </Card>
      </div>
    );
  }

  return (
    <div className="debug-page">
      <Card>
        <div className="debug-header">
          <Button icon={<ArrowLeft size={16} />} onClick={() => navigate(checkType === "ui" ? "/ui-checks" : "/api-checks")}>
            返回
          </Button>
          <div>
            <h2>{check.name}</h2>
          </div>
          <Space wrap>
            <Tooltip title={isDirty ? "运行草稿只验证当前编辑内容，不会保存配置或触发告警" : "当前内容与已保存配置一致"}>
              <Tag color={dirtyTagColor(isDirty)}>{isDirty ? "未保存变更" : "已保存"}</Tag>
            </Tooltip>
            <Button icon={<Play size={16} />} onClick={runSaved} loading={busy === "saved-run"} disabled={readOnly || Boolean(busy)}>
              人工验证已保存配置
            </Button>
            <Button icon={<Play size={16} />} onClick={runDraft} loading={busy === "draft-run"} disabled={readOnly || Boolean(busy)}>
              试运行当前配置
            </Button>
            <Button type="primary" icon={<Save size={16} />} onClick={saveOnly} loading={busy === "save"} disabled={readOnly || Boolean(busy)}>
              保存
            </Button>
          </Space>
        </div>
      </Card>

      {error && <Alert type="error" title={error} showIcon />}

      <div className="debug-layout">
        <Card className="debug-form" title="任务配置">
          <Form layout="vertical">
            <Form.Item label="名称" required>
              <Input name="debug-check-name" value={form.name} onChange={(event) => patchForm({ name: event.target.value })} autoComplete="off" />
            </Form.Item>
            <Form.Item label={checkType === "ui" ? "页面 URL" : "接口 URL"} required>
              <Input name="debug-entry-url" value={form.entry_url} onChange={(event) => patchForm({ entry_url: event.target.value })} autoComplete="off" />
            </Form.Item>
            {checkType === "api" && (
              <Form.Item label="Method" required>
                <Select
                  value={form.method}
                  onChange={(value) => patchForm({ method: value })}
                  options={["GET", "POST", "PUT", "DELETE", "PATCH"].map((method) => ({ label: method, value: method }))}
                />
              </Form.Item>
            )}
            {checkType === "ui" && (
              <Form.Item label="页面模式" required>
                <ViewportModeControl value={form.viewport_mode} onChange={(viewport_mode) => patchForm({ viewport_mode })} />
              </Form.Item>
            )}
            <div className="field-grid two">
              <Form.Item label="执行频率">
                <InputNumber min={5} value={form.interval_seconds} suffix="秒" onChange={(value) => patchForm({ interval_seconds: Number(value || 5) })} />
              </Form.Item>
              <Form.Item label="超时时间">
                <InputNumber min={500} value={form.timeout_ms} suffix="ms" onChange={(value) => patchForm({ timeout_ms: Number(value || 500) })} />
              </Form.Item>
            </div>
            <Form.Item label="启用">
              <Space className="switch-line">
                <span>{form.enabled ? "已启用" : "已禁用"}</span>
                <Switch aria-label="调试任务启用状态" checked={form.enabled} onChange={(value) => patchForm({ enabled: value })} />
              </Space>
            </Form.Item>
          </Form>
        </Card>

        <Card className="debug-editor">
          <Space orientation="vertical" size={14} className="drawer-stack">
            {checkType === "api" && (
              <>
                <div className="api-request-editors">
                  <CodeEditorPanel
                    title="Headers"
                    language="json"
                    value={form.headers_json}
                    height="220px"
                    compact
                    onChange={(value) => patchForm({ headers_json: value })}
                    extra={
                      <Button size="small" onClick={() => formatJsonField("headers_json", "Headers")}>
                        格式化
                      </Button>
                    }
                  />
                  <CodeEditorPanel
                    title="Body"
                    language={bodyMode === "json" ? "json" : "plaintext"}
                    value={form.body}
                    height="220px"
                    compact
                    onChange={(value) => patchForm({ body: value })}
                    extra={
                      <Space size={8} wrap>
                        <Segmented
                          size="small"
                          value={bodyMode}
                          onChange={(value) => setBodyMode(value as BodyEditorMode)}
                          options={[
                            { label: "JSON", value: "json" },
                            { label: "文本", value: "text" }
                          ]}
                        />
                        {bodyMode === "json" && (
                          <Button size="small" onClick={() => formatJsonField("body", "Body")}>
                            格式化
                          </Button>
                        )}
                      </Space>
                    }
                  />
                </div>
              </>
            )}
            {checkType === "ui" ? (
              <>
                <UiAssertionsBuilder check={form} value={form.assertions_json} onChange={(assertions_json) => patchForm({ assertions_json })} />
                <UiScriptSections
                  setupScript={form.setup_script}
                  script={form.script}
                  moduleCompletions={moduleCompletions}
                  completionScopeKey={`project:${currentProject.id}:library:${moduleLibrary?.effective_revision ?? "none"}`}
                  setupHeight="320px"
                  scriptHeight="360px"
                  onSetupScriptChange={(value) => patchForm({ setup_script: value })}
                  onScriptChange={(value) => patchForm({ script: value })}
                />
              </>
            ) : (
              <>
                <Collapse
                  className="advanced-script-collapse"
                  items={[
                    {
                      key: "script",
                      label: form.script.trim() ? (
                        <Tooltip title="脚本可准备并发请求，规则检查最后一次响应">
                          <span>高级探测脚本（可发起请求）</span>
                        </Tooltip>
                      ) : "高级探测脚本（可发起请求）",
                      children: (
                        <CodeEditorPanel
                          title="Python 脚本"
                          language="python"
                          value={form.script}
                          height="360px"
                          onChange={(value) => patchForm({ script: value })}
                          moduleCompletions={moduleCompletions}
                          completionScopeKey={`project:${currentProject.id}:library:${moduleLibrary?.effective_revision ?? "none"}`}
                          extra={
                            !form.script.trim() && (
                              <Button size="small" onClick={() => patchForm({ script: apiScriptTemplate })}>
                                使用模板
                              </Button>
                            )
                          }
                        />
                      )
                    }
                  ]}
                />
                <ApiAssertionsBuilder
                  bodyMode={bodyMode}
                  check={form}
                  projectId={currentProject.id}
                  value={form.assertions_json}
                  onChange={(assertions_json) => patchForm({ assertions_json })}
                />
              </>
            )}
          </Space>
        </Card>

        <div className="debug-result">
          {!run ? (
            <Card>
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={false} />
            </Card>
          ) : (
            <RunResultPanel run={run} mode="debug" />
          )}
        </div>
      </div>
    </div>
  );
}
