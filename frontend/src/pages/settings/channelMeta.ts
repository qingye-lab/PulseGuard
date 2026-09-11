import type { NotificationChannel, WebhookType } from "../../types";

export function createChannel(type: WebhookType): NotificationChannel {
  return {
    id: `channel-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: "",
    type,
    enabled: true,
    webhook_url: "",
    dingtalk_secret: ""
  };
}

export function channelDisplayName(channel: NotificationChannel, index?: number): string {
  const name = channel.name.trim();
  if (name) return name;
  const suffix = typeof index === "number" ? ` ${index + 1}` : "";
  return `${channelTypeLabel(channel.type)}渠道${suffix}`;
}

export function channelTestDisabledReason(channel: NotificationChannel): string {
  if (!channel.enabled) return "启用渠道后再发送测试";
  if (!channel.webhook_url.trim()) return "填写 Webhook URL 后再发送测试";
  return channelUrlError(channel) || channelSecretError(channel);
}

export function channelUrlError(channel: NotificationChannel): string {
  const value = channel.webhook_url.trim();
  if (!value) return "";
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return "Webhook URL 需要是完整的 http:// 或 https:// 地址";
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return "Webhook URL 仅支持 http:// 或 https://";
  }
  return "";
}

export function channelHostHint(channel: NotificationChannel): string {
  const value = channel.webhook_url.trim();
  if (!value || channelUrlError(channel)) return "";
  const parsed = new URL(value);
  if (isLocalWebhookHost(parsed.hostname)) return "";
  const guide = CHANNEL_GUIDES[channel.type];
  if (!parsed.hostname.endsWith(guide.expectedHost)) {
    return `当前 URL 不像${guide.label}官方机器人地址；如果通过内部网关转发，可以忽略此提示`;
  }
  return "";
}

export function channelSecretError(channel: NotificationChannel): string {
  if (channel.type !== "dingtalk") return "";
  const secret = (channel.dingtalk_secret || "").trim();
  if (secret && !secret.startsWith("SEC")) {
    return "钉钉加签密钥应以 SEC 开头";
  }
  return "";
}

export function isLocalWebhookHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

export function channelTypeLabel(type: WebhookType): string {
  return CHANNEL_GUIDES[type]?.label || type;
}

export function isReadyForSend(channel: NotificationChannel): boolean {
  return channel.enabled && Boolean(channel.webhook_url.trim()) && !channelUrlError(channel) && !channelSecretError(channel);
}

export const CHANNEL_OPTIONS: Array<{ label: string; value: WebhookType }> = [
  { label: "飞书", value: "feishu" },
  { label: "企业微信", value: "wecom" },
  { label: "钉钉", value: "dingtalk" }
];

export const CHANNEL_GUIDES: Record<WebhookType, { label: string; expectedHost: string; webhookHint: string; secretHint: string }> = {
  feishu: {
    label: "飞书",
    expectedHost: "open.feishu.cn",
    webhookHint: "粘贴飞书群机器人 Webhook 地址",
    secretHint: "飞书文本机器人无需额外密钥"
  },
  wecom: {
    label: "企业微信",
    expectedHost: "qyapi.weixin.qq.com",
    webhookHint: "粘贴企业微信群机器人 Webhook 地址",
    secretHint: "企业微信群机器人无需额外密钥"
  },
  dingtalk: {
    label: "钉钉",
    expectedHost: "oapi.dingtalk.com",
    webhookHint: "粘贴钉钉自定义机器人 Webhook 地址",
    secretHint: "启用加签时填写 SEC 开头的密钥"
  }
};
