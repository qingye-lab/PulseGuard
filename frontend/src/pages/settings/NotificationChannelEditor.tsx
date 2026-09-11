import { Button, Dropdown, Form, Input, Modal, Popconfirm, Select, Space, Switch, Tag, Tooltip } from "antd";
import type { MenuProps } from "antd";
import { CheckCircle2, KeyRound, MoreHorizontal, Pencil, RotateCcw, Send, Trash2 } from "lucide-react";
import type { NotificationChannel, WebhookType } from "../../types";
import { CHANNEL_GUIDES, CHANNEL_OPTIONS, channelDisplayName, channelHostHint, channelSecretError, channelUrlError, isReadyForSend } from "./channelMeta";

export function NotificationChannelEditor({
  channel,
  index,
  editing,
  testing,
  testDisabledReason,
  usesDraft,
  onPatch,
  onRemove,
  onEdit,
  onDone,
  onTest
}: {
  channel: NotificationChannel;
  index: number;
  editing: boolean;
  testing: boolean;
  testDisabledReason: string;
  usesDraft: boolean;
  onPatch: (patch: Partial<NotificationChannel>) => void;
  onRemove: () => void;
  onEdit: () => void;
  onDone: () => void;
  onTest: () => void;
}) {
  const guide = CHANNEL_GUIDES[channel.type];
  const urlError = channelUrlError(channel);
  const hostHint = channelHostHint(channel);
  const secretError = channelSecretError(channel);
  const ready = isReadyForSend(channel);
  const displayName = channelDisplayName(channel, index);

  function changeType(type: WebhookType) {
    onPatch({
      type,
      dingtalk_secret: "",
      dingtalk_secret_set: type === "dingtalk" ? channel.dingtalk_secret_set : false,
      dingtalk_secret_clear: type === "dingtalk" ? channel.dingtalk_secret_clear : false
    });
  }

  const menuItems: MenuProps["items"] = [
    ...(!editing ? [{ key: "edit", icon: <Pencil size={15} />, label: "编辑" }] : []),
    ...(!editing ? [{ type: "divider" as const }] : []),
    { key: "delete", icon: <Trash2 size={15} />, label: "删除", danger: true }
  ];

  const onMenuClick: MenuProps["onClick"] = ({ key }) => {
    if (key === "edit") {
      onEdit();
      return;
    }
    if (key === "delete") {
      Modal.confirm({
        title: "删除通知渠道",
        content: "删除后保存设置才会生效。",
        okText: "删除",
        cancelText: "取消",
        okButtonProps: { danger: true },
        onOk: onRemove
      });
    }
  };

  return (
    <section className={`notification-channel-card ${ready ? "notification-channel-ready" : ""} ${editing ? "notification-channel-editing" : "notification-channel-compact"}`}>
      <div className="notification-channel-header">
        <div className="notification-channel-title">
          <strong>{displayName}</strong>
        </div>
        <Space size={8} wrap className="notification-channel-actions">
          <Tag>{guide.label}</Tag>
          <span className="notification-channel-enable">
            <Switch aria-label={`${displayName} 渠道启用状态`} checked={channel.enabled} onChange={(value) => onPatch({ enabled: value })} />
          </span>
          <Tooltip title={testDisabledReason || (usesDraft ? "使用当前渠道草稿发送测试，不会保存设置" : "发送一条测试告警到当前渠道")}>
            <span className="tooltip-button-wrapper">
              <Button size="small" icon={<Send size={15} />} loading={testing} disabled={Boolean(testDisabledReason)} onClick={onTest}>
                发送测试
              </Button>
            </span>
          </Tooltip>
          {editing && (
            <Button size="small" icon={<CheckCircle2 size={15} />} onClick={onDone}>
              完成
            </Button>
          )}
          <Dropdown menu={{ items: menuItems, onClick: onMenuClick }} trigger={["click"]}>
            <Button size="small" aria-label={`${displayName} 更多操作`} icon={<MoreHorizontal size={15} />} />
          </Dropdown>
        </Space>
      </div>

      {editing ? (
        <Form layout="vertical" className="notification-channel-grid" autoComplete="off">
          <Form.Item label="渠道名称">
            <Input
              name={`notification-channel-name-${channel.id}`}
              value={channel.name}
              onChange={(event) => onPatch({ name: event.target.value })}
              placeholder="例如：值班群"
              autoComplete="off"
            />
          </Form.Item>
          <Form.Item label="渠道类型">
            <Select value={channel.type} onChange={(value) => changeType(value)} options={CHANNEL_OPTIONS} />
          </Form.Item>
          <Form.Item label="Webhook URL" className="span-2" validateStatus={urlError ? "error" : hostHint ? "warning" : undefined} help={urlError || hostHint || guide.webhookHint}>
            <Input.Password
              name={`notification-channel-webhook-${channel.id}`}
              value={channel.webhook_url}
              onChange={(event) => onPatch({ webhook_url: event.target.value })}
              placeholder="https://…"
              status={urlError ? "error" : hostHint ? "warning" : undefined}
              autoComplete="new-password"
            />
          </Form.Item>
          {channel.type === "dingtalk" && (
            <Form.Item
              label={
                <Space size={8}>
                  <span>钉钉加签密钥</span>
                  {channel.dingtalk_secret_set && !channel.dingtalk_secret_clear && !channel.dingtalk_secret && <Tag color="success">已配置</Tag>}
                  {channel.dingtalk_secret_clear && <Tag color="warning">待清除</Tag>}
                </Space>
              }
              className="span-2"
              validateStatus={secretError ? "warning" : undefined}
              help={secretError || guide.secretHint}
            >
              <Space.Compact className="secret-input-row">
                <Input.Password
                  name={`notification-channel-secret-${channel.id}`}
                  value={channel.dingtalk_secret || ""}
                  onChange={(event) => onPatch({ dingtalk_secret: event.target.value, dingtalk_secret_clear: false })}
                  autoComplete="new-password"
                  placeholder={channel.dingtalk_secret_set ? "输入新密钥可替换现有密钥" : "SEC 开头的加签密钥"}
                  prefix={<KeyRound size={15} />}
                />
                {channel.dingtalk_secret_set && !channel.dingtalk_secret_clear && (
                  <Popconfirm
                    title="清除钉钉加签密钥"
                    description="保存设置后，该渠道将不再携带钉钉签名参数。"
                    okText="清除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => onPatch({ dingtalk_secret: "", dingtalk_secret_set: false, dingtalk_secret_clear: true })}
                  >
                    <Tooltip title="清除已保存密钥">
                      <Button danger aria-label="清除已保存密钥" icon={<Trash2 size={15} />} />
                    </Tooltip>
                  </Popconfirm>
                )}
                {channel.dingtalk_secret_clear && (
                  <Tooltip title="撤销清除密钥">
                    <Button aria-label="撤销清除密钥" icon={<RotateCcw size={15} />} onClick={() => onPatch({ dingtalk_secret_clear: false, dingtalk_secret_set: true })} />
                  </Tooltip>
                )}
              </Space.Compact>
            </Form.Item>
          )}
        </Form>
      ) : null}
    </section>
  );
}
