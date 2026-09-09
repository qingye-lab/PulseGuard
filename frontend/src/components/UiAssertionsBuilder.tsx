import { App, Button, Checkbox, Empty, Input, InputNumber, Modal, Select, Space, Spin, Switch, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  Eye,
  EyeOff,
  Hash,
  Link2,
  MessageSquareText,
  MousePointer2,
  Plus,
  Save,
  ScanSearch,
  Search,
  Terminal,
  Trash2,
  Type
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, MouseEvent, ReactNode } from "react";
import { api, ApiError } from "../api";
import { useProject } from "../ProjectContext";
import { UiInspectionFeedback } from "./UiInspectionFeedback";
import type {
  CheckPayload,
  UiAssertion,
  UiAssertionType,
  UiElementCandidate,
  UiInspectResult,
  UiInspectionDiagnostics,
  UiRuleInspectItem,
  UiRuleInspectResult,
  UiSelectorOption
} from "../types";
import { createUiAssertion, parseUiAssertions, serializeUiAssertions, UI_ASSERTION_LABELS } from "../uiAssertions";
import { VIEWPORT_PRESETS } from "./ViewportModeControl";

type CandidateRuleType = "element_visible" | "element_not_empty" | "element_count" | "text_present";
type CandidateRuleSelection = Partial<Record<CandidateRuleType, boolean>>;
type CandidateSelectionSource = "candidate" | "preview" | "scan";

const CANDIDATE_RULE_OPTIONS: Array<{ type: CandidateRuleType; label: string; requiresText?: boolean }> = [
  { type: "element_visible", label: "可见" },
  { type: "element_not_empty", label: "非空" },
  { type: "element_count", label: "数量" },
  { type: "text_present", label: "文本", requiresText: true }
];

const RULE_OPTIONS: Array<{ type: UiAssertionType; label: string; icon: ReactNode }> = [
  { type: "page_not_blank", label: "页面不空白", icon: <ScanSearch size={15} /> },
  { type: "element_visible", label: "元素可见", icon: <Eye size={15} /> },
  { type: "element_hidden", label: "元素隐藏", icon: <EyeOff size={15} /> },
  { type: "element_not_empty", label: "组件不为空", icon: <MousePointer2 size={15} /> },
  { type: "text_present", label: "文本出现", icon: <MessageSquareText size={15} /> },
  { type: "text_absent", label: "文本不存在", icon: <MessageSquareText size={15} /> },
  { type: "title_contains", label: "标题包含", icon: <Type size={15} /> },
  { type: "url_contains", label: "URL 包含", icon: <Link2 size={15} /> },
  { type: "element_count", label: "元素数量", icon: <Hash size={15} /> },
  { type: "console_error_absent", label: "控制台无错误", icon: <Terminal size={15} /> }
];

const OPERATOR_OPTIONS = [
  { label: "=", value: "eq" },
  { label: "!=", value: "ne" },
  { label: ">", value: "gt" },
  { label: ">=", value: "gte" },
  { label: "<", value: "lt" },
  { label: "<=", value: "lte" }
];

interface Props {
  check: CheckPayload;
  value: string;
  onChange: (value: string) => void;
}

export function UiAssertionsBuilder({ check, value, onChange }: Props) {
  const { message } = App.useApp();
  const { currentProject } = useProject();
  const [modal, modalContextHolder] = Modal.useModal();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [inspectResult, setInspectResult] = useState<UiInspectResult | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<UiElementCandidate | null>(null);
  const [candidateSelectionSource, setCandidateSelectionSource] = useState<CandidateSelectionSource>("scan");
  const [pickerSelections, setPickerSelections] = useState<Record<string, CandidateRuleSelection>>({});
  const [pickerSelectorChoices, setPickerSelectorChoices] = useState<Record<string, string>>({});
  const [candidateQuery, setCandidateQuery] = useState("");
  const [inspecting, setInspecting] = useState(false);
  const [inspectError, setInspectError] = useState<Error | null>(null);
  const [ruleInspectError, setRuleInspectError] = useState<Error | null>(null);
  const [ruleInspecting, setRuleInspecting] = useState(false);
  const [ruleInspectResult, setRuleInspectResult] = useState<UiRuleInspectResult | null>(null);
  const candidateItemRefs = useRef<Map<string, HTMLElement>>(new Map());
  const inspectGenerationRef = useRef(0);
  const ruleInspectGenerationRef = useRef(0);
  const previewScrollRef = useRef<HTMLDivElement | null>(null);
  const previewStageRef = useRef<HTMLDivElement | null>(null);
  const assertions = useMemo(() => parseUiAssertions(value), [value]);
  const enabledCount = assertions.filter((item) => item.enabled).length;
  const selectorRuleCount = assertions.filter(isSelectorAssertion).length;
  const ruleInspectionById = useMemo(
    () => new Map((ruleInspectResult?.results || []).map((result) => [result.id, result])),
    [ruleInspectResult]
  );
  const filteredCandidates = useMemo(() => {
    if (!inspectResult) return [];
    const query = candidateQuery.trim().toLowerCase();
    if (!query) return inspectResult.candidates;
    return inspectResult.candidates.filter((candidate) =>
      [
        candidate.semantic_label || "",
        candidate.name || "",
        candidate.selector,
        candidate.text,
        candidate.tag,
        candidate.role,
        candidate.kind || "",
        candidate.selector_type || "",
        candidate.stability || "",
        ...(candidate.selector_options || []).flatMap((option) => [option.selector, option.selector_type])
      ].some(
        (value) => value.toLowerCase().includes(query)
      )
    );
  }, [candidateQuery, inspectResult]);
  const markerCandidates = useMemo(() => {
    if (!inspectResult) return [];
    const viewportArea = inspectResult.viewport.width * inspectResult.viewport.height;
    return filteredCandidates
      .filter((candidate) => candidate.box.width >= 10 && candidate.box.height >= 10 && candidate.box.width * candidate.box.height < viewportArea * 0.75)
      .sort((left, right) => candidateArea(right) - candidateArea(left))
      .slice(0, 100);
  }, [filteredCandidates, inspectResult]);
  const viewportMode = check.viewport_mode || "web";
  const currentPreset = VIEWPORT_PRESETS[viewportMode];
  const pendingRuleCount = useMemo(
    () => Object.values(pickerSelections).reduce((total, selection) => total + CANDIDATE_RULE_OPTIONS.filter((option) => selection[option.type]).length, 0),
    [pickerSelections]
  );
  const pendingElementCount = useMemo(
    () => Object.values(pickerSelections).filter((selection) => CANDIDATE_RULE_OPTIONS.some((option) => selection[option.type])).length,
    [pickerSelections]
  );

  useEffect(() => {
    if (!pickerOpen || !selectedCandidate || !inspectResult) return;
    if (candidateSelectionSource === "preview") {
      scrollCandidateIntoView(selectedCandidate);
      return;
    }
    if (candidateSelectionSource === "candidate") {
      scrollPreviewToCandidate(selectedCandidate);
    }
  }, [candidateSelectionSource, filteredCandidates, inspectResult, pickerOpen, selectedCandidate]);

  useEffect(() => {
    ruleInspectGenerationRef.current += 1;
    setRuleInspecting(false);
    setRuleInspectError(null);
    setRuleInspectResult(null);
    return () => { ruleInspectGenerationRef.current += 1; };
  }, [check.entry_url, check.setup_script, check.timeout_ms, value, viewportMode, currentProject.id]);

  useEffect(() => {
    inspectGenerationRef.current += 1;
    setInspecting(false);
    setInspectError(null);
    setInspectResult(null);
    setSelectedCandidate(null);
    setPickerSelections({});
    setPickerSelectorChoices({});
    return () => { inspectGenerationRef.current += 1; };
  }, [check.entry_url, check.setup_script, check.timeout_ms, viewportMode, currentProject.id]);

  function commit(next: UiAssertion[]) {
    onChange(serializeUiAssertions(next));
  }

  function addAssertion(type: UiAssertionType) {
    commit([...assertions, createUiAssertion(type)]);
  }

  function selectCandidate(candidate: UiElementCandidate, source: CandidateSelectionSource = "preview") {
    setCandidateSelectionSource(source);
    setSelectedCandidate(candidate);
  }

  function configuredCandidate(candidate: UiElementCandidate): UiElementCandidate {
    return withCandidateSelector(candidate, pickerSelectorChoices[candidateKey(candidate)]);
  }

  function chooseCandidateSelector(candidate: UiElementCandidate, selector: string) {
    const key = candidateKey(candidate);
    setPickerSelectorChoices((current) => ({ ...current, [key]: selector }));
    selectCandidate(candidate, "candidate");
  }

  function setCandidateItemRef(key: string, node: HTMLElement | null) {
    if (node) {
      candidateItemRefs.current.set(key, node);
    } else {
      candidateItemRefs.current.delete(key);
    }
  }

  function scrollCandidateIntoView(candidate: UiElementCandidate) {
    const key = candidateKey(candidate);
    window.requestAnimationFrame(() => {
      const node = candidateItemRefs.current.get(key);
      node?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
    });
  }

  function scrollPreviewToCandidate(candidate: UiElementCandidate) {
    if (!inspectResult) return;
    window.requestAnimationFrame(() => {
      const scroll = previewScrollRef.current;
      const stage = previewStageRef.current;
      if (!scroll || !stage) return;
      const pageSize = inspectResult.page_size || inspectResult.viewport;
      const scale = stage.clientWidth / pageSize.width;
      const targetLeft = stage.offsetLeft + (candidate.box.x + candidate.box.width / 2) * scale - scroll.clientWidth / 2;
      const targetTop = stage.offsetTop + (candidate.box.y + candidate.box.height / 2) * scale - scroll.clientHeight / 2;
      scroll.scrollTo({
        left: Math.max(0, targetLeft),
        top: Math.max(0, targetTop),
        behavior: "smooth"
      });
    });
  }

  function buildCandidateAssertion(
    candidate: UiElementCandidate,
    type: CandidateRuleType
  ) {
    const configured = configuredCandidate(candidate);
    const assertion = createUiAssertion(type);
    if (type === "text_present") {
      assertion.expected_text = configured.text || configured.selector;
      assertion.selector = configured.selector;
    } else if (type === "element_count") {
      const selectedOption = candidateSelectorOptions(candidate)
        .find((option) => option.selector === configured.selector);
      assertion.selector = configured.selector;
      assertion.operator = Number(selectedOption?.match_count || 0) > 1 ? "gte" : "eq";
      assertion.expected_count = 1;
    } else {
      assertion.selector = configured.selector;
    }
    if (assertion.selector) {
      applyCandidateSelectorMetadata(assertion, configured);
      if (type === "text_present") {
        assertion.match_mode = "first";
        assertion.scope_selector = undefined;
        assertion.relative_selector = undefined;
      }
    }
    return assertion;
  }

  function toggleCandidateRule(candidate: UiElementCandidate, type: CandidateRuleType, checked: boolean) {
    if (type === "text_present" && !candidate.text) return;
    selectCandidate(candidate, "candidate");
    const key = candidateKey(candidate);
    setPickerSelections((current) => {
      const nextSelection = { ...(current[key] || {}), [type]: checked };
      if (!CANDIDATE_RULE_OPTIONS.some((option) => nextSelection[option.type])) {
        const { [key]: _removed, ...rest } = current;
        return rest;
      }
      return { ...current, [key]: nextSelection };
    });
  }

  function savePickerSelections() {
    if (!inspectResult || pendingRuleCount === 0) return;
    const candidateMap = new Map(inspectResult.candidates.map((candidate) => [candidateKey(candidate), candidate]));
    const nextAssertions: UiAssertion[] = [];
    const lowStabilityCandidates = new Map<string, UiElementCandidate>();
    Object.entries(pickerSelections).forEach(([key, selection]) => {
      const candidate = candidateMap.get(key);
      if (!candidate) return;
      const configured = configuredCandidate(candidate);
      CANDIDATE_RULE_OPTIONS.forEach((option) => {
        if (!selection[option.type]) return;
        if (configured.stability === "low" && option.type !== "text_present") {
          lowStabilityCandidates.set(key, configured);
        }
        nextAssertions.push(buildCandidateAssertion(candidate, option.type));
      });
    });
    if (!nextAssertions.length) return;
    const persist = () => {
      commit([...assertions, ...nextAssertions]);
      setPickerSelections({});
      setPickerSelectorChoices({});
      setPickerOpen(false);
    };
    if (lowStabilityCandidates.size) {
      const candidates = Array.from(lowStabilityCandidates.values()).slice(0, 4);
      modal.confirm({
        title: "低稳定性 selector",
        content: (
          <div className="ui-selector-warning">
            <p>以下 selector 页面结构变化时更容易失效，建议优先选择 testid、role 或更稳定的元素。</p>
            <ul>
              {candidates.map((candidate) => (
                <li key={candidateKey(candidate)} title={candidate.selector}>
                  {candidateDisplayName(candidate)}
                </li>
              ))}
            </ul>
          </div>
        ),
        okText: "继续保存",
        cancelText: "返回调整",
        onOk: persist
      });
      return;
    }
    persist();
  }

  function updateAssertion(id: string, patch: Partial<UiAssertion>) {
    commit(assertions.map((assertion) => (assertion.id === id ? { ...assertion, ...patch } : assertion)));
  }

  function removeAssertion(id: string) {
    commit(assertions.filter((assertion) => assertion.id !== id));
  }

  function openPicker() {
    setPickerOpen(true);
    setInspectError(null);
    setPickerSelections({});
    setPickerSelectorChoices({});
    setCandidateQuery("");
    setInspectResult(null);
    setSelectedCandidate(null);
    void inspectPage();
  }

  function closePicker() {
    inspectGenerationRef.current += 1;
    setPickerOpen(false);
    setInspecting(false);
  }

  async function inspectPage() {
    const entryUrl = check.entry_url.trim();
    if (!entryUrl) {
      setInspectError(new Error("请先填写页面 URL"));
      message.warning("请先填写页面 URL");
      return;
    }
    const generation = inspectGenerationRef.current + 1;
    inspectGenerationRef.current = generation;
    setInspecting(true);
    setInspectError(null);
    setInspectResult(null);
    setSelectedCandidate(null);
    setPickerSelections({});
    setPickerSelectorChoices({});
    try {
      const result = await api.inspectUi({
        type: "ui",
        entry_url: entryUrl,
        timeout_ms: check.timeout_ms,
        viewport_mode: viewportMode,
        viewport_width: currentPreset.width,
        viewport_height: currentPreset.height,
        setup_script: check.setup_script || ""
      });
      if (inspectGenerationRef.current !== generation) return;
      setInspectResult(result);
      setCandidateSelectionSource("scan");
      setSelectedCandidate(result.candidates[0] || null);
      setPickerSelections({});
      setPickerSelectorChoices({});
      setCandidateQuery("");
    } catch (err) {
      if (inspectGenerationRef.current !== generation) return;
      setInspectError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (inspectGenerationRef.current === generation) setInspecting(false);
    }
  }

  async function inspectRules() {
    const entryUrl = check.entry_url.trim();
    if (!entryUrl) {
      message.warning("请先填写页面 URL");
      return;
    }
    if (!selectorRuleCount) {
      message.warning("当前没有可检测的 selector 规则");
      return;
    }
    const generation = ++ruleInspectGenerationRef.current;
    setRuleInspecting(true);
    setRuleInspectError(null);
    setRuleInspectResult(null);
    try {
      const result = await api.inspectUiRules({
        type: "ui",
        entry_url: entryUrl,
        timeout_ms: check.timeout_ms,
        viewport_mode: viewportMode,
        viewport_width: currentPreset.width,
        viewport_height: currentPreset.height,
        setup_script: check.setup_script || "",
        assertions_json: serializeUiAssertions(assertions)
      });
      if (ruleInspectGenerationRef.current !== generation) return;
      setRuleInspectResult(result);
      const abnormal = result.results.filter((item) => item.status !== "ok" && item.status !== "disabled");
      if (abnormal.length) {
        message.warning(`检测完成，${abnormal.length} 条 selector 需要关注`);
      } else {
        message.success("检测完成，selector 匹配正常");
      }
    } catch (err) {
      if (ruleInspectGenerationRef.current !== generation) return;
      setRuleInspectError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (ruleInspectGenerationRef.current === generation) setRuleInspecting(false);
    }
  }

  function handlePreviewClick(event: MouseEvent<HTMLDivElement>) {
    if (!inspectResult) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const pageSize = inspectResult.page_size || inspectResult.viewport;
    const x = ((event.clientX - rect.left) / rect.width) * pageSize.width;
    const y = ((event.clientY - rect.top) / rect.height) * pageSize.height;
    const candidate = filteredCandidates
      .filter((item) => containsPoint(item, x, y))
      .sort((left, right) => candidateArea(left) - candidateArea(right))[0];
    if (candidate) selectCandidate(candidate, "preview");
  }

  const columns: ColumnsType<UiAssertion> = [
    {
      title: "启用",
      dataIndex: "enabled",
      width: 64,
      render: (_, assertion) => (
        <Switch
          size="small"
          aria-label={`${UI_ASSERTION_LABELS[assertion.type]} 规则启用状态`}
          checked={assertion.enabled}
          onChange={(enabled) => updateAssertion(assertion.id, { enabled })}
        />
      )
    },
    {
      title: "类型",
      dataIndex: "type",
      width: 116,
      responsive: ["sm"],
      render: (_, assertion) => <Tag>{UI_ASSERTION_LABELS[assertion.type]}</Tag>
    },
    {
      title: "规则",
      dataIndex: "selector",
      render: (_, assertion) => renderRuleEditor(assertion, updateAssertion, ruleInspectionById.get(assertion.id))
    },
    {
      title: "操作",
      width: 64,
      align: "center",
      render: (_, assertion) => (
        <Tooltip title="删除规则">
          <Button size="small" danger aria-label="删除规则" icon={<Trash2 size={15} />} onClick={() => removeAssertion(assertion.id)} />
        </Tooltip>
      )
    }
  ];

  return (
    <section className="ui-assertions-panel">
      {modalContextHolder}
      <div className="section-title ui-assertions-header">
        <div>
          <h3>UI 校验</h3>
          <span>{enabledCount} 项启用</span>
        </div>
        <Space wrap>
          <Button icon={<MousePointer2 size={15} />} onClick={openPicker}>
            选择元素
          </Button>
          <Button icon={<ScanSearch size={15} />} onClick={() => void inspectRules()} loading={ruleInspecting} disabled={!selectorRuleCount}>
            检测规则
          </Button>
          <Button icon={<Plus size={15} />} onClick={() => addAssertion("title_contains")}>
            添加标题校验
          </Button>
        </Space>
      </div>

      <div className="ui-assertion-actions">
        {RULE_OPTIONS.map((option) => (
          <Button key={option.type} size="small" icon={option.icon} onClick={() => addAssertion(option.type)}>
            {option.label}
          </Button>
        ))}
      </div>

      <UiInspectionFeedback error={ruleInspectError?.message}
        diagnostics={ruleInspectError instanceof ApiError ? asUiDiagnostics(ruleInspectError.diagnostics) : ruleInspectResult?.diagnostics}
        onRetry={() => void inspectRules()} />
      {assertions.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={false} />
      ) : (
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          columns={columns}
          dataSource={assertions}
          className="ui-assertions-table"
          scroll={{ x: 720 }}
        />
      )}

      <Modal
        open={pickerOpen}
        title="选择页面元素"
        width="min(1420px, 96vw)"
        footer={
          <div className="ui-picker-footer">
            {pendingRuleCount ? <span>{`已选择 ${pendingElementCount} 个元素 / ${pendingRuleCount} 条规则`}</span> : <span />}
            <Space>
              <Button onClick={closePicker}>取消</Button>
              <Button icon={<Save size={15} />} disabled={!pendingRuleCount || inspecting || !inspectResult} onClick={savePickerSelections}>
                保存选择
              </Button>
            </Space>
          </div>
        }
        className="ui-picker-modal"
        onCancel={closePicker}
      >
        <div className="ui-picker-shell">
          <div className="ui-picker-toolbar">
            <div className="ui-picker-url">
              <small>页面 URL</small>
              <strong title={check.entry_url}>{check.entry_url || "未填写"}</strong>
            </div>
            <Tag>
              <span className="ui-picker-viewport-label">
                {currentPreset.icon}
                {currentPreset.label}
              </span>
            </Tag>
            <Tag>
              {currentPreset.width} × {currentPreset.height}
            </Tag>
            <Button icon={<ScanSearch size={15} />} onClick={() => void inspectPage()} loading={inspecting}>
              {inspecting ? "正在扫描" : inspectResult ? "重新扫描" : "扫描页面"}
            </Button>
            {inspectResult?.diagnostics && <UiInspectionFeedback diagnostics={inspectResult.diagnostics} />}
          </div>

          {inspectError && <UiInspectionFeedback error={inspectError.message}
            diagnostics={inspectError instanceof ApiError ? asUiDiagnostics(inspectError.diagnostics) : undefined}
            onRetry={() => void inspectPage()} />}

          <div className="ui-picker-layout">
            <section className="ui-picker-preview-card">
              <div className="ui-picker-card-title">
                <div>
                  <strong>页面预览</strong>
                </div>
                {inspectResult ? (
                  <Tag color="blue">{filteredCandidates.length} 个候选</Tag>
                ) : inspecting ? (
                  <Tag color="processing">扫描中</Tag>
                ) : inspectError ? (
                  <Tag color="error">扫描失败</Tag>
                ) : (
                  <Tag>未扫描</Tag>
                )}
              </div>
              {inspectResult ? (
                <div className="ui-picker-preview-scroll" ref={previewScrollRef}>
                  <div
                    ref={previewStageRef}
                    className={["ui-inspect-stage", viewportMode === "h5" ? "h5" : ""].filter(Boolean).join(" ")}
                    role="button"
                    tabIndex={0}
                    onClick={handlePreviewClick}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && selectedCandidate) selectCandidate(selectedCandidate, "preview");
                    }}
                  >
                    <img src={inspectResult.screenshot} alt={inspectResult.title || inspectResult.url} draggable={false} />
                    {markerCandidates.map((candidate, index) => (
                      <Button
                        key={`${candidate.selector}-${candidate.box.x}-${candidate.box.y}`}
                        className={["ui-inspect-marker", selectedCandidate === candidate ? "selected" : ""].filter(Boolean).join(" ")}
                        title={candidateLabel(candidate)}
                        aria-label={`选择元素 ${candidateLabel(candidate)}`}
                        style={markerStyle(candidate, inspectResult, index)}
                        onClick={(event) => {
                          event.stopPropagation();
                          selectCandidate(candidate, "preview");
                        }}
                      />
                    ))}
                  </div>
                </div>
              ) : inspecting ? (
                <div className="ui-picker-loading" aria-live="polite">
                  <Spin />
                </div>
              ) : inspectError ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={false}>
                  <Button icon={<ScanSearch size={15} />} onClick={() => void inspectPage()} loading={inspecting}>
                    重新扫描
                  </Button>
                </Empty>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={false}>
                  <Button icon={<ScanSearch size={15} />} onClick={() => void inspectPage()} loading={inspecting}>
                    扫描页面
                  </Button>
                </Empty>
              )}
            </section>

            <aside className="ui-picker-side">
              <section className="ui-candidate-card">
                <div className="ui-picker-card-title">
                  <div>
                    <strong>候选元素</strong>
                    <span>{filteredCandidates.length} / {inspectResult?.candidates.length || 0} 个候选</span>
                  </div>
                  <Input
                    className="ui-candidate-search"
                    size="small"
                    value={candidateQuery}
                    prefix={<Search size={15} />}
                    placeholder="搜索名称 / selector…"
                    allowClear
                    onChange={(event) => setCandidateQuery(event.target.value)}
                  />
                </div>
                {filteredCandidates.length ? (
                  <div className="ui-candidate-list">
                    {filteredCandidates.slice(0, 120).map((candidate) => {
                      const key = candidateKey(candidate);
                      const configured = configuredCandidate(candidate);
                      const selectorOptions = candidateSelectorOptions(candidate);
                      const selection = pickerSelections[key] || {};
                      const checkedCount = CANDIDATE_RULE_OPTIONS.filter((option) => selection[option.type]).length;
                      return (
                      <article
                        key={key}
                        ref={(node) => setCandidateItemRef(key, node)}
                        className={[
                          "ui-candidate-item",
                          selectedCandidate && candidateKey(selectedCandidate) === key ? "selected" : "",
                          checkedCount ? "checked" : ""
                        ]
                          .filter(Boolean)
                          .join(" ")}
                      >
                        <div className="ui-candidate-content">
                          <Button
                            type="text"
                            htmlType="button"
                            className="ui-candidate-main"
                            onClick={() => selectCandidate(candidate, "candidate")}
                            aria-label={`选择候选元素 ${candidateDisplayName(candidate)}`}
                          >
                            <strong className="ui-candidate-name" title={candidateDisplayName(candidate)}>
                              {candidateDisplayName(candidate)}
                            </strong>
                          </Button>
                          <Select
                            className="ui-candidate-selector"
                            size="small"
                            value={configured.selector}
                            title={configured.selector}
                            options={selectorOptions.map((option) => ({
                              value: option.selector,
                              label: selectorOptionLabel(option)
                            }))}
                            onChange={(selector) => chooseCandidateSelector(candidate, selector)}
                          />
                        </div>
                        <div className="ui-candidate-rule-grid">
                          {CANDIDATE_RULE_OPTIONS.map((option) => (
                            <Checkbox
                              key={option.type}
                              checked={Boolean(selection[option.type])}
                              disabled={Boolean(option.requiresText && !candidate.text)}
                              onChange={(event) => toggleCandidateRule(candidate, option.type, event.target.checked)}
                            >
                              <span className="ui-candidate-rule-label">
                                {option.label}
                              </span>
                            </Checkbox>
                          ))}
                        </div>
                      </article>
                      );
                    })}
                  </div>
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={false} />
                )}
              </section>
            </aside>
          </div>
        </div>
      </Modal>
    </section>
  );
}

function renderRuleEditor(assertion: UiAssertion, update: (id: string, patch: Partial<UiAssertion>) => void, ruleInspection?: UiRuleInspectItem) {
  if (["element_visible", "element_hidden", "element_not_empty"].includes(assertion.type)) {
    return (
      <Space className="ui-assertion-rule" wrap>
        <Input
          value={assertion.selector}
          placeholder="CSS / text= / role= Playwright 选择器…"
          onChange={(event) => update(assertion.id, editedSelectorPatch(event.target.value))}
        />
        {renderRuleInspection(ruleInspection)}
      </Space>
    );
  }
  if (["text_present", "text_absent", "title_contains", "url_contains"].includes(assertion.type)) {
    return (
      <Space className="ui-assertion-rule" wrap>
        {assertion.selector && (
          <Input
            value={assertion.selector}
            placeholder="限定文本所在区域的 selector…"
            onChange={(event) => update(assertion.id, editedSelectorPatch(event.target.value))}
          />
        )}
        <Input
          value={assertion.expected_text}
          placeholder={assertion.type.includes("text") ? "输入要匹配的文本" : "输入要包含的片段"}
          onChange={(event) => update(assertion.id, { expected_text: event.target.value })}
        />
      </Space>
    );
  }
  if (assertion.type === "element_count") {
    return (
      <Space className="ui-assertion-rule" wrap>
        <Input
          value={assertion.selector}
          placeholder="CSS / text= / role= Playwright 选择器…"
          onChange={(event) => update(assertion.id, editedSelectorPatch(event.target.value))}
        />
        {renderRuleInspection(ruleInspection)}
        <Select
          value={assertion.operator || "eq"}
          className="ui-assertion-operator"
          options={OPERATOR_OPTIONS}
          onChange={(operator) => update(assertion.id, { operator })}
        />
        <InputNumber
          min={0}
          value={assertion.expected_count}
          onChange={(expected_count) => update(assertion.id, { expected_count: Number(expected_count || 0) })}
        />
      </Space>
    );
  }
  return <Tag color="success">无需配置</Tag>;
}

function renderRuleInspection(result?: UiRuleInspectItem) {
  if (!result) return null;
  const meta = ruleInspectionMeta(result);
  return (
    <Tooltip title={result.message || meta.tooltip}>
      <Tag color={meta.color}>{meta.label}</Tag>
    </Tooltip>
  );
}

function ruleInspectionMeta(result: UiRuleInspectItem): { label: string; color: string; tooltip: string } {
  if (result.status === "ok") {
    return { label: typeof result.count === "number" ? `匹配 ${result.count}` : "匹配正常", color: "success", tooltip: "selector 当前匹配正常" };
  }
  if (result.status === "missing") return { label: "未匹配", color: "warning", tooltip: "selector 当前未匹配到元素" };
  if (result.status === "multiple") return { label: `匹配 ${result.count ?? "多"} 个`, color: "warning", tooltip: "selector 当前匹配多个元素" };
  if (result.status === "invalid_selector") return { label: "选择器无效", color: "error", tooltip: "selector 无法解析" };
  if (result.status === "disabled") return { label: "未检测", color: "default", tooltip: "规则已停用" };
  return { label: "检测失败", color: "error", tooltip: "规则检测失败" };
}

function isSelectorAssertion(assertion: UiAssertion): boolean {
  return ["element_visible", "element_hidden", "element_not_empty", "element_count"].includes(assertion.type) ||
    (["text_present", "text_absent"].includes(assertion.type) && Boolean(assertion.selector));
}

function applyCandidateSelectorMetadata(assertion: UiAssertion, candidate: UiElementCandidate) {
  if (candidate.selector_type) assertion.selector_type = candidate.selector_type;
  if (candidate.stability) assertion.selector_stability = candidate.stability;
  if (typeof candidate.score === "number") assertion.selector_score = candidate.score;
  if (candidate.scope_selector) assertion.scope_selector = candidate.scope_selector;
  if (candidate.relative_selector) assertion.relative_selector = candidate.relative_selector;
  if (candidate.match_mode) assertion.match_mode = candidate.match_mode;
  if (candidate.count_mode) assertion.count_mode = candidate.count_mode;
  if (candidate.frame_path?.length) assertion.frame_path = [...candidate.frame_path];
}

function editedSelectorPatch(selector: string): Partial<UiAssertion> {
  return {
    selector,
    selector_type: undefined,
    selector_stability: undefined,
    selector_score: undefined,
    scope_selector: undefined,
    relative_selector: undefined,
    match_mode: undefined,
    count_mode: undefined,
    frame_path: undefined
  };
}

function containsPoint(candidate: UiElementCandidate, x: number, y: number): boolean {
  return (
    x >= candidate.box.x &&
    x <= candidate.box.x + candidate.box.width &&
    y >= candidate.box.y &&
    y <= candidate.box.y + candidate.box.height
  );
}

function candidateArea(candidate: UiElementCandidate): number {
  return candidate.box.width * candidate.box.height;
}

function candidateLabel(candidate: UiElementCandidate): string {
  return candidateDisplayName(candidate) || candidate.text || candidate.selector || candidate.role || candidate.tag;
}

function candidateDisplayName(candidate: UiElementCandidate): string {
  return candidate.semantic_label || candidate.name || candidate.text || candidate.selector || candidate.role || candidate.tag;
}

function candidateKey(candidate: UiElementCandidate): string {
  if (candidate.candidate_id) return candidate.candidate_id;
  return [
    candidate.tag,
    candidate.kind || "",
    candidate.semantic_label || candidate.name || "",
    Math.round(candidate.box.x),
    Math.round(candidate.box.y),
    Math.round(candidate.box.width),
    Math.round(candidate.box.height)
  ].join("|");
}

function candidateSelectorOptions(candidate: UiElementCandidate): UiSelectorOption[] {
  const options = candidate.selector_options?.filter((option) => option.selector) || [];
  if (options.length) return options;
  return [{
    selector: candidate.selector,
    selector_type: candidate.selector_type || "css",
    stability: candidate.stability,
    score: candidate.score,
    scope_selector: candidate.scope_selector,
    relative_selector: candidate.relative_selector,
    match_mode: candidate.match_mode,
    count_mode: candidate.count_mode,
    family_id: candidate.family_id,
    frame_path: candidate.frame_path
  }];
}

function withCandidateSelector(candidate: UiElementCandidate, selector?: string): UiElementCandidate {
  if (!selector || selector === candidate.selector) return candidate;
  const option = candidateSelectorOptions(candidate).find((item) => item.selector === selector);
  if (!option) return candidate;
  return {
    ...candidate,
    selector: option.selector,
    selector_type: option.selector_type,
    stability: option.stability,
    score: option.score,
    scope_selector: option.scope_selector,
    relative_selector: option.relative_selector,
    match_mode: option.match_mode,
    count_mode: option.count_mode,
    family_id: option.family_id,
    frame_path: option.frame_path || candidate.frame_path
  };
}

function selectorOptionLabel(option: UiSelectorOption): string {
  return option.selector;
}

function asUiDiagnostics(value: ApiError["diagnostics"]): UiInspectionDiagnostics | undefined {
  if (!value || !("url_variables" in value) || !("variables" in value)) return undefined;
  return value as UiInspectionDiagnostics;
}

function markerStyle(candidate: UiElementCandidate, result: UiInspectResult, index: number): CSSProperties {
  const pageSize = result.page_size || result.viewport;
  return {
    left: `${(candidate.box.x / pageSize.width) * 100}%`,
    top: `${(candidate.box.y / pageSize.height) * 100}%`,
    width: `${(candidate.box.width / pageSize.width) * 100}%`,
    height: `${(candidate.box.height / pageSize.height) * 100}%`,
    zIndex: 10 + index
  };
}
