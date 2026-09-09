import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tabs,
  Tooltip,
  Tree
} from "antd";
import type { TreeProps } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Braces, Clock3, CopyPlus, FileJson2, ListChecks, MousePointer2, Plus, Send, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import {
  API_ASSERTION_LABELS,
  API_JSON_VALUE_TYPE_LABELS,
  API_LENGTH_OPERATOR_LABELS,
  createApiAssertion,
  JSON_FIELD_ASSERTION_TYPES,
  parseApiAssertions,
  serializeApiAssertions
} from "../apiAssertions";
import { apiInspectSignature, prepareApiInspectPayload, type BodyEditorMode } from "../checkPayload";
import {
  buildJsonPathTree,
  buildJsonPathTreeFromOptions,
  flattenJsonPathTree,
  jsonLength,
  jsonLiteral,
  jsonPreview,
  jsonValueType,
  parseJsonValue,
  readJsonPath,
  type JsonPathNode
} from "../jsonPath";
import type { ApiAssertion, ApiAssertionType, ApiInspectResult, ApiInspectionDiagnostics, CheckPayload } from "../types";
import { formatDuration } from "../utils";
import { StructuredViewer } from "./StructuredViewer";

interface Props {
  bodyMode: BodyEditorMode;
  check: CheckPayload;
  heading?: string;
  projectId?: number | null;
  value: string;
  onChange: (value: string) => void;
}

const BASIC_ASSERTION_ADD_OPTIONS: Array<{ type: ApiAssertionType; label: string }> = [
  { type: "status_code", label: "状态码" },
  { type: "response_time", label: "耗时" }
];

const SELECTED_FIELD_ACTIONS: Array<{ type: ApiAssertionType; label: string; needsLength?: boolean }> = [
  { type: "json_path_exists", label: "存在" },
  { type: "json_path_equals", label: "等于" },
  { type: "json_path_not_empty", label: "非空" },
  { type: "json_path_contains", label: "包含" },
  { type: "json_path_type", label: "类型" },
  { type: "json_path_length", label: "长度", needsLength: true }
];

export function ApiAssertionsBuilder({ bodyMode, check, heading = "健康判定规则", projectId, value, onChange }: Props) {
  const [inspectResult, setInspectResult] = useState<ApiInspectResult | null>(null);
  const [inspectDiagnostics, setInspectDiagnostics] = useState<ApiInspectionDiagnostics | null>(null);
  const [selectedPath, setSelectedPath] = useState<string>("");
  const [inspecting, setInspecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inspectSignature = useMemo(
    () => `${apiInspectSignature(check, projectId)}|body_mode:${bodyMode}`,
    [bodyMode, check.body, check.entry_url, check.headers_json, check.method, check.script, check.timeout_ms, projectId]
  );
  const inspectSignatureRef = useRef(inspectSignature);
  const inspectRequestRef = useRef(0);
  inspectSignatureRef.current = inspectSignature;
  const assertions = useMemo(() => parseApiAssertions(value), [value]);
  const parsedBody = useMemo(() => (inspectResult?.json_valid ? parseJsonValue(inspectResult.body) : { valid: false as const, value: null }), [inspectResult]);
  const pathTree = useMemo(() => (inspectResult?.json_valid ? buildJsonPathTreeFromOptions(inspectResult.json_paths) : []), [inspectResult]);
  const bodyTree = useMemo(() => (parsedBody.valid ? buildJsonPathTree(parsedBody.value) : []), [parsedBody]);
  const tree = pathTree.length ? pathTree : bodyTree;
  const flatNodes = useMemo(() => flattenJsonPathTree(tree), [tree]);
  const selectedNode = useMemo(() => {
    if (!selectedPath) return null;
    const knownNode = flatNodes.find((node) => node.path === selectedPath);
    if (knownNode) {
      if (!parsedBody.valid) return knownNode;
      const resolved = readJsonPath(parsedBody.value, selectedPath);
      if (!resolved.exists) return knownNode;
      return {
        ...knownNode,
        type: jsonValueType(resolved.value),
        preview: knownNode.preview || jsonPreview(resolved.value),
        length: knownNode.length ?? jsonLength(resolved.value),
        value: resolved.value
      } satisfies JsonPathNode;
    }
    if (!parsedBody.valid) return null;
    const resolved = readJsonPath(parsedBody.value, selectedPath);
    if (!resolved.exists) return null;
    return {
      key: selectedPath,
      path: selectedPath,
      type: jsonValueType(resolved.value),
      preview: jsonPreview(resolved.value),
      length: jsonLength(resolved.value),
      value: resolved.value
    } satisfies JsonPathNode;
  }, [flatNodes, parsedBody, selectedPath]);
  const pathOptions = useMemo(
    () =>
      flatNodes.map((item) => ({
        value: item.path,
        label: `${item.path} · ${API_JSON_VALUE_TYPE_LABELS[item.type]}`
      })),
    [flatNodes]
  );
  const treeData = useMemo<TreeProps["treeData"]>(() => tree.map(toTreeNode), [tree]);
  const selectedBaselineRuleTypes = useMemo(() => (selectedNode ? baselineRuleTypesForNode(selectedNode) : []), [selectedNode]);
  const selectedBaselineNewCount = useMemo(
    () => (selectedNode ? selectedBaselineRuleTypes.filter((type) => !hasFieldAssertion(assertions, type, selectedNode.path)).length : 0),
    [assertions, selectedBaselineRuleTypes, selectedNode]
  );

  useEffect(() => {
    inspectRequestRef.current += 1;
    setInspectResult(null);
    setInspectDiagnostics(null);
    setSelectedPath("");
    setError(null);
    setInspecting(false);
  }, [inspectSignature]);

  function commit(next: ApiAssertion[]) {
    onChange(serializeApiAssertions(next));
  }

  function updateAssertion(id: string, patch: Partial<ApiAssertion>) {
    commit(assertions.map((assertion) => (assertion.id === id ? { ...assertion, ...patch } : assertion)));
  }

  function removeAssertion(id: string) {
    commit(assertions.filter((assertion) => assertion.id !== id));
  }

  function addAssertion(type: ApiAssertionType, path = selectedPath) {
    const seed = seedFromSelectedNode(type, selectedNode);
    commit([...assertions, createApiAssertion(type, path || firstSelectablePath(flatNodes), seed)]);
  }

  function addBaselineFieldAssertions(node: JsonPathNode) {
    const next = [...assertions];
    baselineRuleTypesForNode(node).forEach((type) => {
      if (hasFieldAssertion(next, type, node.path)) return;
      next.push(createApiAssertion(type, node.path, seedFromSelectedNode(type, node)));
    });
    if (next.length !== assertions.length) {
      commit(next);
    }
  }

  async function inspect() {
    const signature = inspectSignature;
    const requestVersion = inspectRequestRef.current + 1;
    inspectRequestRef.current = requestVersion;
    setInspecting(true);
    setError(null);
    setInspectResult(null);
    setInspectDiagnostics(null);
    setSelectedPath("");
    try {
      const payload = prepareApiInspectPayload(check, { bodyMode });
      const result = await api.inspectApi(payload);
      if (requestVersion !== inspectRequestRef.current || signature !== inspectSignatureRef.current) return;
      setInspectResult(result);
      setInspectDiagnostics(result.diagnostics || null);
      if (result.json_valid) {
        const pathNodes = flattenJsonPathTree(buildJsonPathTreeFromOptions(result.json_paths));
        if (pathNodes.length) {
          setSelectedPath(firstSelectablePath(pathNodes));
        } else {
          const parsed = parseJsonValue(result.body);
          if (parsed.valid) {
            const nodes = flattenJsonPathTree(buildJsonPathTree(parsed.value));
            setSelectedPath(firstSelectablePath(nodes));
          }
        }
      } else {
        setSelectedPath("");
      }
    } catch (err) {
      if (requestVersion !== inspectRequestRef.current || signature !== inspectSignatureRef.current) return;
      setError((err as Error).message);
      setInspectDiagnostics(err instanceof ApiError ? asApiDiagnostics(err.diagnostics) || null : null);
    } finally {
      if (requestVersion === inspectRequestRef.current) setInspecting(false);
    }
  }

  const columns: ColumnsType<ApiAssertion> = [
    {
      title: "启用",
      width: 70,
      render: (_, assertion) => (
        <Switch
          size="small"
          aria-label={`${API_ASSERTION_LABELS[assertion.type]} 校验项启用状态`}
          checked={assertion.enabled}
          onChange={(enabled) => updateAssertion(assertion.id, { enabled })}
        />
      )
    },
    {
      title: "类型",
      width: 108,
      responsive: ["sm"],
      render: (_, assertion) => <Tag>{API_ASSERTION_LABELS[assertion.type]}</Tag>
    },
    {
      title: "规则",
      render: (_, assertion) => renderRuleEditor(assertion, pathOptions, updateAssertion)
    },
    {
      title: "操作",
      width: 70,
      align: "center",
      render: (_, assertion) => (
        <Button
          size="small"
          title="删除"
          aria-label="删除校验项"
          danger
          icon={<Trash2 size={14} />}
          onClick={() => removeAssertion(assertion.id)}
        />
      )
    }
  ];

  return (
    <section className="api-assertions-panel">
      <div className="section-title api-assertions-header">
        <div>
          <h3>{heading}</h3>
          <span>{assertions.filter((item) => item.enabled).length} 项启用</span>
        </div>
        <Space wrap>
          <Button icon={<Send size={15} />} onClick={inspect} loading={inspecting}>
            执行请求
          </Button>
          {BASIC_ASSERTION_ADD_OPTIONS.map((item) => (
            <Button key={item.type} size="small" icon={<Plus size={14} />} onClick={() => addAssertion(item.type)}>
              {item.label}
            </Button>
          ))}
        </Space>
      </div>

      {error && (
        <Alert
          type="error"
          title={error}
          showIcon
          action={inspectDiagnostics ? <ApiInspectionDetails diagnostics={inspectDiagnostics} /> : undefined}
        />
      )}

      {inspectResult && (
        <div className="api-inspect-summary">
          <span>
            <ListChecks size={15} />
            HTTP {inspectResult.status_code}
          </span>
          <span>
            <Clock3 size={15} />
            目标请求 {formatDuration(inspectResult.duration_ms)}
          </span>
          {inspectResult.timings?.task_ms !== undefined && (
            <span>
              <Clock3 size={15} />
              全程探测 {formatDuration(inspectResult.timings.task_ms)}
            </span>
          )}
          <span>
            <FileJson2 size={15} />
            {inspectResult.json_valid ? `${flatNodes.length} 个 JSON 节点` : "非 JSON 响应"}
          </span>
        </div>
      )}

      <div className="api-assertion-workbench">
        <section className="api-json-tree-card">
          <div className="api-json-tree-header">
            <strong>响应字段</strong>
            {selectedPath ? <Tag color="blue">{selectedPath}</Tag> : <Tag>未执行</Tag>}
          </div>
          {inspectResult ? (
            inspectResult.json_valid && treeData?.length ? (
              <Tree
                key={`${inspectResult.status_code}-${inspectResult.duration_ms}-${flatNodes.length}`}
                blockNode
                defaultExpandAll
                selectedKeys={selectedPath ? [selectedPath] : []}
                treeData={treeData}
                onSelect={(keys) => setSelectedPath(String(keys[0] || ""))}
              />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="响应没有可选字段" />
            )
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="执行请求后选择响应字段" />
          )}
        </section>

        <section className="api-selected-field-card">
          <div className="api-json-tree-header">
            <strong>添加字段校验</strong>
            <MousePointer2 size={15} />
          </div>
          {selectedNode ? (
            <>
              <div className="api-selected-field">
                <div>
                  <small>路径</small>
                  <strong>{selectedNode.path}</strong>
                </div>
                <div>
                  <small>类型</small>
                  <strong>{API_JSON_VALUE_TYPE_LABELS[selectedNode.type]}</strong>
                </div>
                <div>
                  <small>值预览</small>
                  <strong title={selectedNode.preview || "-"}>{selectedNode.preview || "-"}</strong>
                </div>
              </div>
              <Space wrap className="api-selected-actions">
                <Tooltip title="生成字段存在、类型、非空和长度基础规则">
                  <Button
                    type="primary"
                    size="small"
                    icon={<CopyPlus size={14} />}
                    disabled={selectedBaselineNewCount === 0}
                    onClick={() => addBaselineFieldAssertions(selectedNode)}
                  >
                    一键生成基础规则
                  </Button>
                </Tooltip>
                {SELECTED_FIELD_ACTIONS.map((item) => (
                  <Tooltip
                    key={item.type}
                    title={item.needsLength && selectedNode.length === null ? "字符串、数组和对象支持长度校验" : API_ASSERTION_LABELS[item.type]}
                  >
                    <Button
                      size="small"
                      icon={<CopyPlus size={14} />}
                      disabled={item.needsLength && selectedNode.length === null}
                      onClick={() => addAssertion(item.type, selectedNode.path)}
                    >
                      {item.label}
                    </Button>
                  </Tooltip>
                ))}
              </Space>
            </>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择响应字段后添加规则" />
          )}
        </section>
      </div>

      <Table
        rowKey="id"
        size="small"
        pagination={false}
        columns={columns}
        dataSource={assertions}
        className="api-assertions-table"
        scroll={{ x: 820 }}
        locale={{
          emptyText: (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无健康判定规则">
              <Button type="primary" size="small" icon={<Plus size={14} />} onClick={() => addAssertion("status_code")}>
                添加状态码
              </Button>
            </Empty>
          )
        }}
      />

      {inspectResult && (
        <StructuredViewer title="最近响应" value={inspectResult.body} defaultMode={inspectResult.json_valid ? "json" : "text"} />
      )}
      {!error && inspectDiagnostics && <ApiInspectionDetails diagnostics={inspectDiagnostics} />}
    </section>
  );
}

function ApiInspectionDetails({ diagnostics }: { diagnostics: ApiInspectionDiagnostics }) {
  const [open, setOpen] = useState(false);
  const history = (diagnostics.http_requests || []).slice(-10);
  return (
    <>
      <Button size="small" onClick={() => setOpen(true)}>
        执行详情
      </Button>
      <Modal title="执行详情" open={open} onCancel={() => setOpen(false)} footer={null} width={860} destroyOnHidden>
        <Tabs
          items={[
            {
              key: "http",
              label: "请求记录",
              children: <StructuredViewer title="最近请求与响应（最多 10 条）" value={history} defaultMode="json" />
            },
            {
              key: "timing",
              label: "耗时",
              children: (
                <Descriptions bordered column={1} size="small">
                  <Descriptions.Item label="目标请求耗时">{formatDuration(diagnostics.timings?.request_ms)}</Descriptions.Item>
                  <Descriptions.Item label="全程探测耗时">{formatDuration(diagnostics.timings?.task_ms)}</Descriptions.Item>
                </Descriptions>
              )
            },
            {
              key: "logs",
              label: "日志",
              children: <StructuredViewer title="本次执行" value={[diagnostics.message, diagnostics.logs].filter(Boolean).join("\n")} defaultMode="text" />
            }
          ]}
        />
      </Modal>
    </>
  );
}

function renderRuleEditor(
  assertion: ApiAssertion,
  pathOptions: Array<{ value: string; label: string }>,
  updateAssertion: (id: string, patch: Partial<ApiAssertion>) => void
) {
  if (assertion.type === "status_code") {
    return (
      <Space className="api-assertion-rule" wrap>
        <Tag>等于</Tag>
        <InputNumber
          min={100}
          max={599}
          value={assertion.expected_status}
          onChange={(expected_status) => updateAssertion(assertion.id, { expected_status: Number(expected_status || 200) })}
        />
      </Space>
    );
  }

  if (assertion.type === "response_time") {
    return (
      <Space className="api-assertion-rule" wrap>
        <Tag>小于等于</Tag>
        <InputNumber
          min={1}
          value={assertion.max_ms}
          suffix="ms"
          onChange={(max_ms) => updateAssertion(assertion.id, { max_ms: Number(max_ms || 1) })}
        />
      </Space>
    );
  }

  return (
    <Space className="api-assertion-rule" wrap>
      <Select
        showSearch
        value={assertion.path}
        options={pathOptions}
        onChange={(path) => updateAssertion(assertion.id, { path })}
        placeholder="先执行请求并选择字段…"
        popupMatchSelectWidth={false}
        filterOption={(input, option) => String(option?.label || "").toLowerCase().includes(input.toLowerCase())}
      />
      {assertion.type === "json_path_equals" && (
        <Input
          value={assertion.expected_value}
          placeholder="期望值，支持 JSON 字面量…"
          onChange={(event) => updateAssertion(assertion.id, { expected_value: event.target.value })}
        />
      )}
      {assertion.type === "json_path_contains" && (
        <Input
          value={assertion.expected_value}
          placeholder="包含的文本、数组元素或对象键…"
          onChange={(event) => updateAssertion(assertion.id, { expected_value: event.target.value })}
        />
      )}
      {assertion.type === "json_path_type" && (
        <Select
          value={assertion.expected_type}
          className="api-assertion-mini-select"
          options={Object.entries(API_JSON_VALUE_TYPE_LABELS).map(([type, label]) => ({ value: type, label }))}
          onChange={(expected_type) => updateAssertion(assertion.id, { expected_type })}
        />
      )}
      {assertion.type === "json_path_length" && (
        <>
          <Select
            value={assertion.operator}
            className="api-assertion-operator"
            options={Object.entries(API_LENGTH_OPERATOR_LABELS).map(([operator, label]) => ({ value: operator, label }))}
            onChange={(operator) => updateAssertion(assertion.id, { operator })}
          />
          <InputNumber
            min={0}
            value={assertion.expected_length}
            suffix="长度"
            onChange={(expected_length) => updateAssertion(assertion.id, { expected_length: Number(expected_length || 0) })}
          />
        </>
      )}
    </Space>
  );
}

function asApiDiagnostics(value: ApiError["diagnostics"]): ApiInspectionDiagnostics | undefined {
  if (!value || typeof value !== "object") return undefined;
  return value as ApiInspectionDiagnostics;
}

function toTreeNode(node: JsonPathNode): NonNullable<TreeProps["treeData"]>[number] {
  return {
    key: node.path,
    title: (
      <span className="api-json-tree-node">
        <Braces size={13} />
        <strong>{node.path === "$" ? "$ 根节点" : node.path.split(".").pop() || node.path}</strong>
        <Tag>{API_JSON_VALUE_TYPE_LABELS[node.type]}</Tag>
        {node.length !== null && <small>{node.length}</small>}
        {node.preview && <em title={node.preview}>{node.preview}</em>}
      </span>
    ),
    children: node.children?.map(toTreeNode)
  };
}

function firstSelectablePath(nodes: JsonPathNode[]): string {
  return nodes.find((node) => node.path !== "$")?.path || nodes[0]?.path || "";
}

function baselineRuleTypesForNode(node: JsonPathNode): ApiAssertionType[] {
  const types: ApiAssertionType[] = ["json_path_exists", "json_path_type"];
  if (node.type !== "null") {
    types.push("json_path_not_empty");
  }
  if (node.length !== null) {
    types.push("json_path_length");
  }
  return types;
}

function hasFieldAssertion(assertions: ApiAssertion[], type: ApiAssertionType, path: string): boolean {
  return assertions.some((assertion) => assertion.type === type && assertion.path === path);
}

function seedFromSelectedNode(type: ApiAssertionType, node: JsonPathNode | null): Partial<ApiAssertion> {
  if (!node || !JSON_FIELD_ASSERTION_TYPES.includes(type)) return {};
  if (type === "json_path_equals") return { path: node.path, expected_value: jsonLiteral(node.value) };
  if (type === "json_path_contains") {
    return { path: node.path, expected_value: typeof node.value === "string" ? node.value : node.type === "object" ? "" : jsonLiteral(node.value) };
  }
  if (type === "json_path_type") return { path: node.path, expected_type: node.type };
  if (type === "json_path_length") return { path: node.path, operator: "gte", expected_length: node.length ?? 1 };
  return { path: node.path };
}
