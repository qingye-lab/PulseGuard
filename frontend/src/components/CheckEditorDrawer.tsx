import { Alert, Button, Collapse, Drawer, Form, Input, InputNumber, Modal, Segmented, Select, Space, Switch, Tag, Tooltip } from "antd";
import { Play, Save } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { parseApiAssertions } from "../apiAssertions";
import { checkToPayload, detectBodyMode, normalizeCheckPayload, prepareCheckPayload, sameCheckPayload, type BodyEditorMode } from "../checkPayload";
import { apiScriptTemplate, blankCheck, checkFromTemplate, checkTemplatesForType } from "../defaults";
import { useProject } from "../ProjectContext";
import type {
  AlertPolicy,
  BrowserSelectionMode,
  BrowserType,
  Check,
  CheckPayload,
  CheckType,
  Member,
  ModuleLibrary,
  NotificationChannel,
  ProbeRunner,
  Run,
  RunnerSelectionMode
} from "../types";
import { dirtyTagColor } from "../utils";
import { ApiAssertionsBuilder } from "./ApiAssertionsBuilder";
import { LazyCodeEditorPanel as CodeEditorPanel } from "./LazyCodeEditorPanel";
import { RunResultPanel } from "./RunResultPanel";
import { UiAssertionsBuilder } from "./UiAssertionsBuilder";
import { UiScriptSections } from "./UiScriptSections";
import { ViewportModeControl } from "./ViewportModeControl";
import { moduleCompletionItems } from "../moduleLibrary";

const BROWSER_TYPES: BrowserType[] = ["chromium", "firefox", "webkit"];

interface Props {
  open: boolean;
  type: CheckType;
  check: Check | null;
  onClose: () => void;
  onSaved: () => void;
}

export function CheckEditorDrawer({ open, type, check, onClose, onSaved }: Props) {
  const { currentProject } = useProject();
  const [form, setForm] = useState<CheckPayload>(() => blankCheck(type));
  const [activeCheck, setActiveCheck] = useState<Check | null>(check);
  const [baseline, setBaseline] = useState<CheckPayload>(() => normalizeCheckPayload(blankCheck(type)));
  const [saving, setSaving] = useState(false);
  const [runningMode, setRunningMode] = useState<"saved" | "draft" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [debugRun, setDebugRun] = useState<Run | null>(null);
  const [debugSnapshot, setDebugSnapshot] = useState<CheckPayload | null>(null);
  const [bodyMode, setBodyMode] = useState<BodyEditorMode>("json");
  const [notificationChannels, setNotificationChannels] = useState<NotificationChannel[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [runners, setRunners] = useState<ProbeRunner[]>([]);
  const [enabledBrowserTypes, setEnabledBrowserTypes] = useState<BrowserType[]>(["chromium"]);
  const [selectedTemplateId, setSelectedTemplateId] = useState(() => checkTemplatesForType(type)[0]?.id || "");
  const [moduleLibrary, setModuleLibrary] = useState<ModuleLibrary | null>(null);

  const templates = useMemo(() => checkTemplatesForType(type), [type]);
  const enabledApiAssertionCount = useMemo(
    () => type === "api" ? parseApiAssertions(form.assertions_json).filter((item) => item.enabled).length : 0,
    [form.assertions_json, type]
  );
  const isDirty = useMemo(() => !sameCheckPayload(form, baseline), [baseline, form]);
  const debugStale = useMemo(() => Boolean(debugRun && debugSnapshot && !sameCheckPayload(form, debugSnapshot)), [debugRun, debugSnapshot, form]);
  const alertPolicy = useMemo(() => parseCheckAlertPolicy(form.alert_policy_json), [form.alert_policy_json]);
  const alertPolicyMode = useMemo(() => (hasAlertPolicyOverrides(alertPolicy) ? "custom" : "inherit"), [alertPolicy]);
  const selectedRunnerIds = useMemo(() => form.runner_ids ?? ["local"], [form.runner_ids]);
  const selectedBrowserTypes = useMemo(() => normalizeBrowserTypes(form.browser_types), [form.browser_types]);
  const runnerList = useMemo(() => {
    return runners.length
      ? runners
      : [
          {
            runner_id: "local",
            name: "local",
            address: "127.0.0.1",
            network_region: "local",
            browser_version: "",
            installed_browser_types: ["chromium"],
            available_browser_types: ["chromium"],
            status: "ok",
            enabled: true,
            role: "local",
            available: true,
            metadata: {},
            updated_at: ""
          } as ProbeRunner
        ];
  }, [runners]);
  const availableRunnerIds = useMemo(() => runnerList.filter(isSelectableRunner).map((runner) => runner.runner_id), [runnerList]);
  const runnerOptions = useMemo(() => {
    const selected = new Set(selectedRunnerIds);
    const visible = runnerList.filter((runner) => isSelectableRunner(runner) || (Boolean(activeCheck) && selected.has(runner.runner_id)));
    const seen = new Set<string>();
    return visible
      .filter((runner) => {
        if (seen.has(runner.runner_id)) return false;
        seen.add(runner.runner_id);
        return true;
      })
      .map((runner) => ({
        label: runnerOptionLabel(runner),
        value: runner.runner_id,
        disabled: !isSelectableRunner(runner)
      }));
  }, [activeCheck, runnerList, selectedRunnerIds]);
  const hiddenUnavailableRunnerCount = useMemo(() => {
    const selected = new Set(selectedRunnerIds);
    return runnerList.filter((runner) => !selected.has(runner.runner_id) && !isSelectableRunner(runner)).length;
  }, [runnerList, selectedRunnerIds]);
  const browserOptions = useMemo(() => {
    const enabled = new Set(enabledBrowserTypes);
    const selected = new Set(selectedBrowserTypes);
    return BROWSER_TYPES.filter((browserType) => enabled.has(browserType) || selected.has(browserType)).map((browserType) => ({
      label: browserTypeLabel(browserType, runnerList),
      value: browserType,
      disabled: !enabled.has(browserType)
    }));
  }, [enabledBrowserTypes, runnerList, selectedBrowserTypes]);
  const running = Boolean(runningMode);
  const moduleCompletions = useMemo(() => moduleCompletionItems(moduleLibrary), [moduleLibrary]);

  useEffect(() => {
    if (!open) return;
    const next = check ? checkToPayload(check) : blankCheck(type);
    setError(null);
    setDebugRun(null);
    setDebugSnapshot(null);
    setActiveCheck(check);
    setForm(next);
    setBaseline(normalizeCheckPayload(next));
    setBodyMode(detectBodyMode(next.body));
    setSelectedTemplateId(checkTemplatesForType(type)[0]?.id || "");
  }, [check, open, type]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setModuleLibrary(null);
    api.library(currentProject.id)
      .then((value) => {
        if (!cancelled) setModuleLibrary(value);
      })
      .catch(() => {
        if (!cancelled) setModuleLibrary(null);
      });
    return () => {
      cancelled = true;
    };
  }, [currentProject.id, open]);

  useEffect(() => {
    if (!open) return;
    Promise.all([api.settings(), api.runners()])
      .then(([values, nextRunners]) => {
        setNotificationChannels(values.notification_channels || []);
        setMembers(values.members || []);
        setRunners(nextRunners);
        const nextEnabledBrowserTypes = normalizeBrowserTypes(values.enabled_browser_types);
        setEnabledBrowserTypes(nextEnabledBrowserTypes);
        const nextAvailableRunnerIds = nextRunners.filter(isSelectableRunner).map((runner) => runner.runner_id);
        if (!check) {
          setForm((current) => {
            const currentIds = current.runner_ids?.filter((runnerId) => nextAvailableRunnerIds.includes(runnerId)) ?? [];
            const currentBrowserTypes = normalizeBrowserTypes(current.browser_types).filter((browserType) => nextEnabledBrowserTypes.includes(browserType));
            return {
              ...current,
              runner_ids: currentIds.length ? currentIds : nextAvailableRunnerIds.slice(0, 1),
              browser_types: type === "ui" ? currentBrowserTypes.length ? currentBrowserTypes : nextEnabledBrowserTypes.slice(0, 1) : []
            };
          });
        }
      })
      .catch(() => {
        setNotificationChannels([]);
        setMembers([]);
        setRunners([]);
        setEnabledBrowserTypes(["chromium"]);
      });
  }, [check, open, type]);

  async function save(): Promise<Check> {
    setSaving(true);
    setError(null);
    try {
      validateRunnerSelection(form, availableRunnerIds);
      validateBrowserSelection(form, enabledBrowserTypes);
      const payload = prepareCheckPayload(form, { bodyMode });
      const saved = activeCheck ? await api.updateCheck(activeCheck.id, payload) : await api.createCheck(payload);
      const savedPayload = checkToPayload(saved);
      setActiveCheck(saved);
      setForm(savedPayload);
      setBaseline(normalizeCheckPayload(savedPayload));
      onSaved();
      return saved;
    } catch (err) {
      setError((err as Error).message);
      throw err;
    } finally {
      setSaving(false);
    }
  }

  async function handleSaveOnly() {
    try {
      await save();
      onClose();
    } catch {
      // save() has already surfaced the validation or request error in the drawer.
    }
  }

  async function handleRunSaved() {
    setRunningMode("saved");
    setError(null);
    try {
      if (!activeCheck) {
        throw new Error("新任务需要先保存后才能运行已保存版本");
      }
      const run = await api.runCheck(activeCheck.id);
      setDebugRun(run);
      setDebugSnapshot(normalizeCheckPayload(checkToPayload(activeCheck)));
      onSaved();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRunningMode(null);
    }
  }

  async function handleRunDraft() {
    setRunningMode("draft");
    setError(null);
    try {
      const payload = prepareCheckPayload(form, { bodyMode });
      const run = await api.debugCheck(payload);
      setDebugRun(run);
      setDebugSnapshot(normalizeCheckPayload(payload));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRunningMode(null);
    }
  }

  function patchForm(values: Partial<CheckPayload>) {
    setForm((current) => ({ ...current, ...values }));
  }

  function applySelectedTemplate() {
    const next = checkFromTemplate(type, selectedTemplateId);
    if (!next) return;
    setError(null);
    setDebugRun(null);
    setDebugSnapshot(null);
    setForm(next);
    setBodyMode(detectBodyMode(next.body));
  }

  function patchAlertPolicy(patch: Partial<AlertPolicy>) {
    patchForm({ alert_policy_json: serializeCheckAlertPolicy({ ...alertPolicy, ...patch }) });
  }

  function setRunnerSelectionMode(mode: RunnerSelectionMode) {
    patchForm({
      runner_selection_mode: mode,
      runner_ids: form.runner_ids ?? ["local"]
    });
  }

  function setBrowserSelectionMode(mode: BrowserSelectionMode) {
    patchForm({
      browser_selection_mode: mode,
      browser_types: form.browser_types ?? ["chromium"]
    });
  }

  function setAlertPolicyMode(mode: "inherit" | "custom") {
    if (mode === "inherit") {
      patchForm({ alert_policy_json: serializeCheckAlertPolicy({ member_ids: alertPolicy.member_ids || [] }) });
      return;
    }
    if (hasAlertPolicyOverrides(alertPolicy)) return;
    patchForm({
      alert_policy_json: serializeCheckAlertPolicy({
        ...defaultAlertPolicy(notificationChannels),
        member_ids: alertPolicy.member_ids || []
      })
    });
  }

  function requestClose() {
    if (!isDirty) {
      onClose();
      return;
    }
    Modal.confirm({
      title: "有未保存变更",
      content: "关闭后将丢失当前编辑内容。",
      okText: "放弃更改",
      okButtonProps: { danger: true },
      cancelText: "继续编辑",
      onOk: onClose
    });
  }

  function formatJsonField(field: "headers_json" | "body", label: string) {
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

  return (
    <Drawer
      title={activeCheck ? "编辑任务" : "新增任务"}
      open={open}
      size={920}
      onClose={requestClose}
      destroyOnHidden
      extra={
        <Space size={8}>
          {debugStale && (
            <Tooltip title="调试结果来自上一次运行，当前草稿已有新改动">
              <Tag color="warning">调试已过期</Tag>
            </Tooltip>
          )}
          <Tooltip title={isDirty ? "运行草稿只验证当前编辑内容，不会保存配置或触发告警" : activeCheck ? "当前内容与已保存配置一致" : "保存后才会创建任务"}>
            <Tag color={activeCheck ? dirtyTagColor(isDirty) : "blue"}>{isDirty ? "未保存变更" : activeCheck ? "已保存" : "新任务草稿"}</Tag>
          </Tooltip>
        </Space>
      }
      footer={
        <Space className="drawer-footer-actions">
          <Button onClick={requestClose}>取消</Button>
          <Button
            icon={<Play size={16} />}
            onClick={handleRunSaved}
            loading={runningMode === "saved"}
            disabled={saving || running || !activeCheck}
          >
            人工验证已保存配置
          </Button>
          <Button icon={<Play size={16} />} onClick={handleRunDraft} loading={runningMode === "draft"} disabled={saving || running}>
            试运行当前配置
          </Button>
          <Button type="primary" icon={<Save size={16} />} onClick={handleSaveOnly} loading={saving} disabled={running}>
            保存
          </Button>
        </Space>
      }
    >
      <Space orientation="vertical" size={16} className="drawer-stack">
        <Form layout="vertical">
          {!activeCheck && templates.length > 0 && (
            <div className="task-template-picker">
              <Form.Item label="任务模板" className="task-template-select">
                <Select
                  value={selectedTemplateId || undefined}
                  onChange={setSelectedTemplateId}
                  options={templates.map((template) => ({ label: template.label, value: template.id }))}
                />
              </Form.Item>
              <Button onClick={applySelectedTemplate} disabled={!selectedTemplateId}>
                应用模板
              </Button>
            </div>
          )}

          <div className="field-grid two">
            <Form.Item label="名称" required>
              <Input name="check-name" value={form.name} onChange={(event) => patchForm({ name: event.target.value })} autoComplete="off" />
            </Form.Item>
            <Form.Item label="标签">
              <Input name="check-tags" value={form.tags} onChange={(event) => patchForm({ tags: event.target.value })} autoComplete="off" />
            </Form.Item>
          </div>

          <Form.Item label={type === "ui" ? "页面 URL" : "接口 URL"} required>
            <Input name="check-entry-url" value={form.entry_url} onChange={(event) => patchForm({ entry_url: event.target.value })} autoComplete="off" />
          </Form.Item>

          <div className="field-grid four">
            {type === "api" && (
              <Form.Item label="Method" required>
                <Select
                  value={form.method}
                  onChange={(value) => patchForm({ method: value })}
                  options={["GET", "POST", "PUT", "DELETE", "PATCH"].map((method) => ({ label: method, value: method }))}
                />
              </Form.Item>
            )}
            {type === "ui" && (
              <Form.Item label="页面模式" required>
                <ViewportModeControl value={form.viewport_mode} onChange={(viewport_mode) => patchForm({ viewport_mode })} />
              </Form.Item>
            )}
            <Form.Item label="执行频率" required>
              <InputNumber
                min={5}
                value={form.interval_seconds}
                suffix="秒"
                onChange={(value) => patchForm({ interval_seconds: Number(value || 5) })}
              />
            </Form.Item>
            <Form.Item label="超时时间" required>
              <InputNumber
                min={500}
                value={form.timeout_ms}
                suffix="ms"
                onChange={(value) => patchForm({ timeout_ms: Number(value || 500) })}
              />
            </Form.Item>
            <Form.Item label="启用">
              <Space className="switch-line">
                <span>{form.enabled ? "已启用" : "已禁用"}</span>
                <Switch aria-label="任务启用状态" checked={form.enabled} onChange={(checked) => patchForm({ enabled: checked })} />
              </Space>
            </Form.Item>
          </div>

          <div className="field-grid two">
            <Form.Item label="执行节点策略" required>
              <Segmented
                value={form.runner_selection_mode || "selected_parallel"}
                onChange={(value) => setRunnerSelectionMode(value as RunnerSelectionMode)}
                options={[
                  { label: "多选并行", value: "selected_parallel" },
                  { label: "轮询所有启用节点", value: "round_robin_all" }
                ]}
              />
            </Form.Item>
            {form.runner_selection_mode === "round_robin_all" ? (
              <Form.Item label="执行节点" extra={hiddenUnavailableRunnerCount > 0 ? `已隐藏 ${hiddenUnavailableRunnerCount} 个当前不可用或已停用节点` : undefined}>
                <Input value="所有当前可用节点，按任务轮询游标挨个请求" disabled />
              </Form.Item>
            ) : (
              <Form.Item label="执行节点" required extra={hiddenUnavailableRunnerCount > 0 ? `已隐藏 ${hiddenUnavailableRunnerCount} 个当前不可用或已停用节点` : undefined}>
                <Select
                  mode="multiple"
                  value={selectedRunnerIds}
                  onChange={(runner_ids) => patchForm({ runner_ids })}
                  options={runnerOptions}
                  placeholder="选择执行节点"
                  optionFilterProp="label"
                />
              </Form.Item>
            )}
          </div>

          {type === "ui" && (
            <div className="field-grid two">
              <Form.Item label="浏览器策略" required>
                <Segmented
                  value={form.browser_selection_mode || "selected_parallel"}
                  onChange={(value) => setBrowserSelectionMode(value as BrowserSelectionMode)}
                  options={[
                    { label: "多选并行", value: "selected_parallel" },
                    { label: "按节点轮询", value: "round_robin_all" }
                  ]}
                />
              </Form.Item>
              {form.browser_selection_mode === "round_robin_all" ? (
                <Form.Item label="浏览器类型">
                  <Input value="按每个执行节点的已启用且已安装类型轮询" disabled />
                </Form.Item>
              ) : (
                <Form.Item label="浏览器类型" required>
                  <Select
                    mode="multiple"
                    value={selectedBrowserTypes}
                    onChange={(browser_types) => patchForm({ browser_types: browser_types as BrowserType[] })}
                    options={browserOptions}
                    placeholder="选择浏览器类型"
                    optionFilterProp="label"
                  />
                </Form.Item>
              )}
            </div>
          )}
        </Form>

        <Collapse
          className="advanced-script-collapse"
          items={[
            {
              key: "alert-policy",
              label: "告警策略",
              children: (
                <Form layout="vertical" className="settings-form-grid" autoComplete="off">
                  <Form.Item label="策略来源">
                    <Segmented
                      value={alertPolicyMode}
                      onChange={(value) => setAlertPolicyMode(value as "inherit" | "custom")}
                      options={[
                        { label: "继承全局", value: "inherit" },
                        { label: "自定义", value: "custom" }
                      ]}
                    />
                  </Form.Item>
                  <Form.Item label="关联成员" className="span-2">
                    <Select
                      mode="multiple"
                      value={alertPolicy.member_ids || []}
                      onChange={(value) => patchAlertPolicy({ member_ids: value })}
                      options={members.map((member) => ({ label: member.name, value: member.id }))}
                      placeholder={members.length ? "选择告警时需要通知的成员" : "暂无可关联成员"}
                      allowClear
                    />
                  </Form.Item>
                  {alertPolicyMode === "custom" && (
                    <>
                      <Form.Item label="通知渠道" className="span-2">
                        <Select
                          mode="multiple"
                          value={alertPolicy.notification_channel_ids || []}
                          onChange={(value) => patchAlertPolicy({ notification_channel_ids: value })}
                          options={notificationChannels.map((channel) => ({
                            label: channel.name || channel.type,
                            value: channel.id,
                            disabled: !channel.enabled
                          }))}
                        />
                      </Form.Item>
                      <Form.Item label="失败冷却时间">
                        <InputNumber
                          min={1}
                          max={1440}
                          value={alertPolicy.alert_cooldown_minutes ?? 30}
                          suffix="分钟"
                          onChange={(value) => patchAlertPolicy({ alert_cooldown_minutes: Number(value || 1) })}
                        />
                      </Form.Item>
                      <Form.Item label="恢复通知">
                        <Switch
                          aria-label="任务恢复通知"
                          checked={alertPolicy.recovery_notification ?? true}
                          onChange={(value) => patchAlertPolicy({ recovery_notification: value })}
                        />
                      </Form.Item>
                    </>
                  )}
                </Form>
              )
            }
          ]}
        />

        {type === "api" && (
          <>
            <div className="api-request-editors">
              <CodeEditorPanel
                title="Headers"
                language="json"
                value={form.headers_json}
                height="210px"
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
                height="210px"
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

        {type === "ui" ? (
          <>
            <Collapse
              className="advanced-script-collapse"
              items={[
                {
                  key: "health-rules",
                  label: "健康判定规则",
                  children: (
                    <UiAssertionsBuilder check={form} value={form.assertions_json} onChange={(assertions_json) => patchForm({ assertions_json })} />
                  )
                }
              ]}
            />
            <UiScriptSections
              setupScript={form.setup_script}
              script={form.script}
              moduleCompletions={moduleCompletions}
              completionScopeKey={`project:${currentProject.id}:library:${moduleLibrary?.effective_revision ?? "none"}`}
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
                      height="320px"
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
            <Collapse
              className="advanced-script-collapse"
              defaultActiveKey="health-rules"
              items={[
                {
                  key: "health-rules",
                  label: `健康判定规则（${enabledApiAssertionCount} 项启用）`,
                  children: (
                    <ApiAssertionsBuilder
                      bodyMode={bodyMode}
                      check={form}
                      heading="接口校验"
                      projectId={currentProject.id}
                      value={form.assertions_json}
                      onChange={(assertions_json) => patchForm({ assertions_json })}
                    />
                  )
                }
              ]}
            />
          </>
        )}

        {error && <Alert type="error" title={error} showIcon />}
        {debugRun && <RunResultPanel run={debugRun} mode="debug" />}
      </Space>
    </Drawer>
  );
}

function isSelectableRunner(runner: ProbeRunner): boolean {
  return Boolean(runner.enabled && runner.available);
}

function runnerOptionLabel(runner: ProbeRunner): string {
  const suffix = !runner.enabled ? " · 已停用" : runner.available ? "" : " · 当前不可用";
  return `${runner.name || runner.runner_id} · ${runner.network_region || "local"}${suffix}`;
}

function validateRunnerSelection(form: CheckPayload, availableRunnerIds: string[]) {
  if (availableRunnerIds.length === 0) {
    throw new Error("当前没有可用执行节点，请先恢复节点后再保存任务");
  }
  if ((form.runner_selection_mode || "selected_parallel") === "round_robin_all") return;
  const available = new Set(availableRunnerIds);
  const selected = form.runner_ids ?? [];
  if (!selected.some((runnerId) => available.has(runnerId))) {
    throw new Error("请选择当前可用的执行节点");
  }
}

function validateBrowserSelection(form: CheckPayload, enabledBrowserTypes: BrowserType[]) {
  if (form.type !== "ui") return;
  if ((form.browser_selection_mode || "selected_parallel") === "round_robin_all") {
    if (!enabledBrowserTypes.length) throw new Error("请先在设置中启用至少一个 browser type");
    return;
  }
  const enabled = new Set(enabledBrowserTypes);
  const selected = normalizeBrowserTypes(form.browser_types);
  if (!selected.some((browserType) => enabled.has(browserType))) {
    throw new Error("请选择已启用的 browser type");
  }
}

function normalizeBrowserTypes(value?: string[] | null): BrowserType[] {
  const seen = new Set<string>();
  const result: BrowserType[] = [];
  for (const item of value || []) {
    const browserType = String(item || "").trim() as BrowserType;
    if (!BROWSER_TYPES.includes(browserType) || seen.has(browserType)) continue;
    seen.add(browserType);
    result.push(browserType);
  }
  return result.length ? result : ["chromium"];
}

function browserTypeLabel(browserType: BrowserType, runners: ProbeRunner[]): string {
  const installedCount = runners.filter((runner) => (runner.installed_browser_types || []).includes(browserType)).length;
  return installedCount ? `${browserType} · ${installedCount} 节点已安装` : `${browserType} · 暂无节点安装`;
}

function parseCheckAlertPolicy(value?: string | null): AlertPolicy {
  if (!value?.trim()) return {};
  try {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const policy = parsed as AlertPolicy;
    return {
      ...(typeof policy.alert_cooldown_minutes === "number" ? { alert_cooldown_minutes: policy.alert_cooldown_minutes } : {}),
      ...(typeof policy.recovery_notification === "boolean" ? { recovery_notification: policy.recovery_notification } : {}),
      ...(Array.isArray(policy.notification_channel_ids) ? { notification_channel_ids: policy.notification_channel_ids.map(String) } : {}),
      ...(Array.isArray(policy.member_ids) ? { member_ids: policy.member_ids.map(String) } : {})
    };
  } catch {
    return {};
  }
}

function serializeCheckAlertPolicy(policy: AlertPolicy): string {
  const normalized: AlertPolicy = {};
  if (typeof policy.alert_cooldown_minutes === "number" && Number.isFinite(policy.alert_cooldown_minutes)) {
    normalized.alert_cooldown_minutes = Math.max(1, Math.min(1440, Math.round(policy.alert_cooldown_minutes)));
  }
  if (typeof policy.recovery_notification === "boolean") {
    normalized.recovery_notification = policy.recovery_notification;
  }
  if (Array.isArray(policy.notification_channel_ids)) {
    normalized.notification_channel_ids = Array.from(new Set(policy.notification_channel_ids.map((item) => String(item || "").trim()).filter(Boolean)));
  }
  if (Array.isArray(policy.member_ids)) {
    normalized.member_ids = Array.from(new Set(policy.member_ids.map((item) => String(item || "").trim()).filter(Boolean)));
  }
  return JSON.stringify(normalized);
}

function hasAlertPolicyOverrides(policy: AlertPolicy): boolean {
  return (
    typeof policy.alert_cooldown_minutes === "number" ||
    typeof policy.recovery_notification === "boolean" ||
    Array.isArray(policy.notification_channel_ids)
  );
}

function defaultAlertPolicy(channels: NotificationChannel[]): AlertPolicy {
  return {
    alert_cooldown_minutes: 30,
    recovery_notification: true,
    notification_channel_ids: channels.filter((channel) => channel.enabled).map((channel) => channel.id)
  };
}
