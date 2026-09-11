import { Alert, App, Button, Card, Drawer, Empty, Form, Input, Popconfirm, Select, Space, Switch, Tag, Tooltip } from "antd";
import { Info, KeyRound, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import type { AlertPreview, AlertPreviewChannel, AlertTagPolicy, EnvironmentVariable, NotificationChannel, SettingsValues } from "../../types";
import { enabledTagColor } from "../../utils";
import { api } from "../../api";
import { StructuredViewer } from "../../components/StructuredViewer";
import { NumberItem } from "./panelPrimitives";
import { channelSecretError, channelTypeLabel, channelUrlError } from "./channelMeta";
import { VARIABLE_NAME_PATTERN } from "./settingsTypes";

export function ProjectVariablesPanel({ projectName, readOnly }: { projectName: string; readOnly: boolean }) {
  const { message } = App.useApp();
  const [variables, setVariables] = useState<EnvironmentVariable[]>([]);
  const [saved, setSaved] = useState<EnvironmentVariable[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api.projectVariables().then((values) => {
      if (active) { setVariables(values); setSaved(values); }
    }).catch((err: Error) => { if (active) setError(err.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  const changed = JSON.stringify(variables) !== JSON.stringify(saved);
  const invalid = firstVariableBlockingError(variables);
  async function saveVariables() {
    setSaving(true);
    try {
      const values = await api.updateProjectVariables(variables.map(toPayloadEnvironmentVariable));
      setVariables(values); setSaved(values);
      message.success("项目变量已保存");
    } catch (err) { message.error((err as Error).message); }
    finally { setSaving(false); }
  }
  return <Card title={`${projectName} · 项目变量`} className="settings-panel-wide" loading={loading} extra={
    <Space>
      <Tooltip title="使用 ${变量名} 引用；本次运行的临时变量优先于项目变量，项目变量优先于全局变量。">
        <Button type="text" aria-label="变量引用说明" icon={<Info size={16} />} />
      </Tooltip>
      <Button disabled={readOnly || loading || saving || Boolean(error)} onClick={() => setVariables([...variables, createEnvironmentVariable()])}>添加变量</Button>
      <Button type="primary" loading={saving} disabled={readOnly || loading || Boolean(error) || !changed || Boolean(invalid)} onClick={saveVariables}>保存变量</Button>
    </Space>
  }>
    {error ? <Alert type="error" message={error} /> : <fieldset className="settings-readonly-fieldset" disabled={readOnly || saving}>
      {variables.length === 0 ? <Empty description="暂无项目变量" /> : <div className="environment-variable-list">
        {variables.map((variable, index) => <EnvironmentVariableEditor key={variable.id} variable={variable} variables={variables} index={index}
          onPatch={(patch) => setVariables((current) => current.map((item) => item.id === variable.id ? { ...item, ...patch } : item))}
          onRemove={() => setVariables((current) => current.filter((item) => item.id !== variable.id))}
        />)}
      </div>}
    </fieldset>}
  </Card>;
}

export function EnvironmentVariableEditor({
  variable,
  variables,
  index,
  onPatch,
  onRemove
}: {
  variable: EnvironmentVariable;
  variables: EnvironmentVariable[];
  index: number;
  onPatch: (patch: Partial<EnvironmentVariable>) => void;
  onRemove: () => void;
}) {
  const nameError = environmentVariableNameError(variable, variables);
  const status = environmentVariableValueStatus(variable);
  const displayName = variable.name.trim() || `变量 ${index + 1}`;
  const canClearSavedSecret = variable.secret && Boolean(variable.value_set) && !variable.value_clear;

  return (
    <section className={`environment-variable-card ${status.color === "success" ? "environment-variable-ready" : ""}`}>
      <div className="environment-variable-header">
        <div className="environment-variable-title">
          <strong>{displayName}</strong>
          <span>{variable.name.trim() ? `\${${variable.name.trim()}}` : "等待填写变量名"}</span>
        </div>
        <Space size={8} wrap>
          <Tag color={variable.secret ? "processing" : "default"}>{variable.secret ? "密钥" : "明文"}</Tag>
          <Tag color={status.color}>{status.label}</Tag>
          <Popconfirm title="删除环境变量" description="删除后保存设置才会生效。" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={onRemove}>
            <Tooltip title="删除变量">
              <Button danger aria-label="删除环境变量" icon={<Trash2 size={15} />} />
            </Tooltip>
          </Popconfirm>
        </Space>
      </div>

      <Form layout="vertical" className="environment-variable-grid" autoComplete="off">
        <Form.Item label="变量名" validateStatus={nameError ? "error" : undefined} help={nameError || "使用 NAME 或 SERVICE_TOKEN 格式"}>
          <Input
            name={`environment-variable-name-${variable.id}`}
            value={variable.name}
            onChange={(event) => onPatch({ name: event.target.value })}
            placeholder="SERVICE_TOKEN"
            autoComplete="off"
            status={nameError ? "error" : undefined}
          />
        </Form.Item>
        <Form.Item label="密钥变量">
          <Switch
            aria-label={`${displayName} 是否作为密钥变量`}
            checked={variable.secret}
            onChange={(secret) => onPatch({ secret, value_clear: secret ? variable.value_clear : false })}
          />
        </Form.Item>
        <Form.Item
          label={
            <Space size={8}>
              <span>变量值</span>
              {variable.secret && variable.value_set && !variable.value && !variable.value_clear && <Tag color="success">已配置</Tag>}
              {variable.secret && variable.value && <Tag color="processing">{variable.value_set ? "待替换" : "待保存"}</Tag>}
              {variable.value_clear && <Tag color="warning">待清空</Tag>}
            </Space>
          }
          className="span-2"
          help={environmentVariableValueHelp(variable)}
        >
          <Space.Compact className="environment-variable-value-row">
            {variable.secret ? (
              <Input.Password
                name={`environment-variable-value-${variable.id}`}
                value={variable.value}
                onChange={(event) => onPatch({ value: event.target.value, value_clear: false })}
                placeholder={variable.value_set ? "输入新值可替换已保存密钥" : "输入变量值"}
                autoComplete="new-password"
                prefix={<KeyRound size={15} />}
              />
            ) : (
              <Input
                name={`environment-variable-value-${variable.id}`}
                value={variable.value}
                onChange={(event) => onPatch({ value: event.target.value, value_clear: false })}
                placeholder="变量值"
                autoComplete="off"
              />
            )}
            {canClearSavedSecret && (
              <Popconfirm
                title="清空已保存密钥"
                description="保存设置后，该变量的已保存密钥值会被清空。"
                okText="清空"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={() => onPatch({ value: "", value_clear: true })}
              >
                <Tooltip title="清空已保存密钥">
                  <Button danger aria-label="清空已保存密钥" icon={<Trash2 size={15} />} />
                </Tooltip>
              </Popconfirm>
            )}
            {variable.value_clear && (
              <Tooltip title="撤销清空密钥">
                <Button aria-label="撤销清空密钥" icon={<RotateCcw size={15} />} onClick={() => onPatch({ value_clear: false })} />
              </Tooltip>
            )}
          </Space.Compact>
        </Form.Item>
      </Form>
    </section>
  );
}

export function AlertTagPolicyEditor({
  policy,
  index,
  channels,
  policies,
  onPatch,
  onRemove
}: {
  policy: AlertTagPolicy;
  index: number;
  channels: NotificationChannel[];
  policies: AlertTagPolicy[];
  onPatch: (patch: Partial<AlertTagPolicy>) => void;
  onRemove: () => void;
}) {
  const tagError = alertTagPolicyTagError(policy, policies);
  return (
    <section className={`notification-channel-card ${policy.enabled && !tagError ? "notification-channel-ready" : ""}`}>
      <div className="notification-channel-header">
        <div className="notification-channel-title">
          <strong>{policy.name.trim() || `标签策略 ${index + 1}`}</strong>
          <span>{policy.enabled ? (tagError || "匹配标签后覆盖全局告警策略") : "已停用"}</span>
        </div>
        <Space size={8}>
          <Tag color={policy.enabled && !tagError ? "success" : "default"}>{policy.enabled ? "启用" : "停用"}</Tag>
          <Switch aria-label="标签告警策略启用状态" checked={policy.enabled} onChange={(enabled) => onPatch({ enabled })} />
          <Popconfirm title="删除标签策略？" okText="删除" cancelText="取消" onConfirm={onRemove}>
            <Button icon={<Trash2 size={16} />} danger />
          </Popconfirm>
        </Space>
      </div>
      <Form layout="vertical" className="notification-channel-grid" autoComplete="off">
        <Form.Item label="策略名称">
          <Input value={policy.name} onChange={(event) => onPatch({ name: event.target.value })} autoComplete="off" />
        </Form.Item>
        <Form.Item label="匹配标签" required validateStatus={tagError ? "error" : undefined} help={tagError || undefined}>
          <Input value={policy.tag} onChange={(event) => onPatch({ tag: event.target.value })} autoComplete="off" />
        </Form.Item>
        <Form.Item label="通知渠道" className="span-2">
          <Select
            mode="multiple"
            value={policy.notification_channel_ids || []}
            onChange={(value) => onPatch({ notification_channel_ids: value })}
            options={channels.map((channel) => ({
              label: channel.name || channelTypeLabel(channel.type),
              value: channel.id,
              disabled: !channel.enabled
            }))}
          />
        </Form.Item>
        <NumberItem
          label="失败冷却时间"
          value={policy.alert_cooldown_minutes ?? 30}
          min={1}
          max={1440}
          suffix="分钟"
          onChange={(value) => onPatch({ alert_cooldown_minutes: value })}
        />
        <Form.Item label="恢复通知">
          <Switch
            aria-label="标签恢复通知"
            checked={policy.recovery_notification ?? true}
            onChange={(recovery_notification) => onPatch({ recovery_notification })}
          />
        </Form.Item>
      </Form>
    </section>
  );
}

export function AlertPreviewTool({
  compact = false,
  preview,
  loading,
  disabledReason,
  usesDraft,
  onPreview,
  onClosePreview
}: {
  compact?: boolean;
  preview: AlertPreview | null;
  loading: boolean;
  disabledReason: string;
  usesDraft: boolean;
  onPreview: () => void;
  onClosePreview?: () => void;
}) {
  const disabled = Boolean(disabledReason);
  const previewContent = preview && (
    <div className="alert-preview-body">
      {preview.channels.length === 0 ? (
        <Empty description="暂无可预检渠道" />
      ) : (
        <div className="alert-preview-channel-list">
          {preview.channels.map((channel) => (
            <PreviewChannel key={channel.id || `${channel.type}-${channel.name}`} channel={channel} />
          ))}
        </div>
      )}
      <StructuredViewer title="消息正文" value={preview.message_text} defaultMode="markdown" />
    </div>
  );

  if (compact) {
    return (
      <>
        <Tooltip title={disabledReason || (usesDraft ? "使用当前草稿生成，不保存、不发送" : "生成预检，不发送网络请求")}>
          <span className="tooltip-button-wrapper">
            <Button icon={<ShieldCheck size={16} />} loading={loading} disabled={disabled} onClick={onPreview}>
              预检渠道
            </Button>
          </span>
        </Tooltip>
        <Drawer title="发送预检" open={Boolean(preview)} size={780} onClose={onClosePreview}>
          {previewContent}
        </Drawer>
      </>
    );
  }

  return (
    <section className="alert-preview-tool">
      <div className="alert-preview-header">
        <div>
          <strong>本地预检</strong>
          <span>{usesDraft ? "使用当前页面草稿生成，不保存也不发送" : "基于已保存配置生成，不发送网络请求"}</span>
        </div>
        <Tooltip title={disabledReason || "生成脱敏目标、消息正文和 Webhook Payload"}>
          <span className="tooltip-button-wrapper">
            <Button icon={<ShieldCheck size={16} />} loading={loading} disabled={disabled} onClick={onPreview}>
              预检配置
            </Button>
          </span>
        </Tooltip>
      </div>

      {previewContent}
    </section>
  );
}

function PreviewChannel({ channel }: { channel: AlertPreviewChannel }) {
  return (
    <section className="alert-preview-channel">
      <div className="alert-preview-channel-header">
        <strong>{channel.name || channelTypeLabel(channel.type)}</strong>
        <Space size={6} wrap>
          <Tag>{channelTypeLabel(channel.type)}</Tag>
          <Tag color={enabledTagColor(channel.enabled)}>{channel.enabled ? "启用" : "停用"}</Tag>
        </Space>
      </div>
      <div className="alert-preview-facts">
        <PreviewFact label="目标 Origin" value={channel.target.origin || "-"} />
        <PreviewFact label="路径" value={channel.target.path || "-"} />
        <PreviewFact label="查询参数" value={channel.target.query_keys.length ? channel.target.query_keys.join(", ") : "-"} />
        <PreviewFact label="钉钉加签" value={channel.signing_enabled ? "发送时追加 timestamp/sign" : "不追加签名"} tone={channel.signing_enabled ? "ok" : "muted"} />
      </div>
      {channel.target.issues.length > 0 && <Alert className="compact-alert" type="warning" title={channel.target.issues.join("；")} showIcon />}
      <StructuredViewer title="Webhook Payload" value={channel.payload} defaultMode="json" />
    </section>
  );
}

function PreviewFact({ label, value, tone = "muted" }: { label: string; value: string; tone?: "ok" | "muted" }) {
  return (
    <div className={`meta-field meta-field-preview meta-field-preview-${tone}`}>
      <small>{label}</small>
      <span>{value}</span>
    </div>
  );
}

function environmentVariableNameError(variable: EnvironmentVariable, variables: EnvironmentVariable[]): string {
  const name = variable.name.trim();
  if (!name) return "变量名称不能为空";
  if (!VARIABLE_NAME_PATTERN.test(name)) return "变量名称必须使用 NAME 或 SERVICE_TOKEN 格式";
  const duplicate = variables.some((candidate) => candidate.id !== variable.id && candidate.name.trim() === name);
  if (duplicate) return "变量名称不能重复";
  return "";
}

function environmentVariableValueStatus(variable: EnvironmentVariable): { label: string; color: "default" | "success" | "warning" | "processing" } {
  if (variable.value_clear) return { label: "待清空", color: "warning" };
  if (variable.secret && variable.value.trim()) {
    return { label: variable.value_set ? "待替换" : "待保存", color: "processing" };
  }
  if (variable.secret && variable.value_set) return { label: "已配置", color: "success" };
  if (variable.value.trim()) return { label: "已配置", color: "success" };
  return { label: "未配置", color: "default" };
}

function environmentVariableValueHelp(variable: EnvironmentVariable): string {
  if (variable.secret) {
    if (variable.value_clear) return "保存后清空已保存密钥";
    if (variable.value_set && !variable.value) return "留空会保留已保存密钥，输入新值会替换";
    return "保存后不会在设置页回显";
  }
  if (variable.value_set && !variable.value) return "切换为明文后需要重新输入值，否则保存为空";
  const placeholder = variable.name.trim() || "NAME";
  return `使用 \${${placeholder}} 引用该变量`;
}

export function createEnvironmentVariable(): EnvironmentVariable {
  return {
    id: `variable-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: "",
    value: "",
    secret: false,
    value_set: false,
    value_clear: false
  };
}

export function createAlertTagPolicy(settings: SettingsValues): AlertTagPolicy {
  return {
    id: `tag-policy-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: "",
    tag: "",
    enabled: true,
    alert_cooldown_minutes: settings.alert_cooldown_minutes,
    recovery_notification: settings.recovery_notification,
    notification_channel_ids: settings.notification_channels.filter((channel) => channel.enabled).map((channel) => channel.id)
  };
}

export function toPayloadAlertTagPolicy(policy: AlertTagPolicy): AlertTagPolicy {
  return {
    id: policy.id,
    name: policy.name.trim(),
    tag: policy.tag.trim(),
    enabled: policy.enabled,
    alert_cooldown_minutes: Math.max(1, Math.min(1440, Number(policy.alert_cooldown_minutes || 1))),
    recovery_notification: Boolean(policy.recovery_notification),
    notification_channel_ids: Array.from(new Set((policy.notification_channel_ids || []).map((id) => id.trim()).filter(Boolean)))
  };
}

export function toPayloadEnvironmentVariable(variable: EnvironmentVariable): EnvironmentVariable {
  const payload: EnvironmentVariable = {
    id: variable.id,
    name: variable.name.trim(),
    value: variable.value.trim(),
    secret: variable.secret
  };
  if (variable.secret && variable.value_clear) {
    payload.value_clear = true;
  }
  return payload;
}

export function toPayloadChannel(channel: NotificationChannel): NotificationChannel {
  const payload: NotificationChannel = {
    id: channel.id,
    name: channel.name.trim(),
    type: channel.type,
    enabled: channel.enabled,
    webhook_url: channel.webhook_url.trim()
  };
  if (channel.type === "dingtalk") {
    payload.dingtalk_secret = (channel.dingtalk_secret || "").trim();
    if (channel.dingtalk_secret_clear) {
      payload.dingtalk_secret_clear = true;
    }
  }
  return payload;
}

export function firstAlertTagPolicyError(policies: AlertTagPolicy[]): string {
  for (const policy of policies) {
    const error = alertTagPolicyTagError(policy, policies);
    if (error) return error;
  }
  return "";
}

function alertTagPolicyTagError(policy: AlertTagPolicy, policies: AlertTagPolicy[]): string {
  const tag = policy.tag.trim();
  if (!tag) return "标签告警策略标签不能为空";
  if (tag.includes(",")) return "标签告警策略标签不能包含逗号";
  const duplicate = policies.some((candidate) => candidate.id !== policy.id && candidate.tag.trim().toLowerCase() === tag.toLowerCase());
  if (duplicate) return "标签告警策略标签不能重复";
  return "";
}

export function firstVariableBlockingError(variables: EnvironmentVariable[]): string {
  for (const variable of variables) {
    const error = environmentVariableNameError(variable, variables);
    if (error) return error;
  }
  return "";
}

export function firstBlockingError(channels: NotificationChannel[]): string {
  for (const channel of channels) {
    const error = channelUrlError(channel) || channelSecretError(channel);
    if (error) return error;
  }
  return "";
}

export function uniqueIds(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}
