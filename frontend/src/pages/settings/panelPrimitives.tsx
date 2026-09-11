import { Button, Card, Form, InputNumber, Space, Tooltip } from "antd";
import { Save } from "lucide-react";
import { ReactNode, useId } from "react";

export function SettingsPanel({
  title,
  icon,
  children,
  accent,
  className,
  extra
}: {
  title: string;
  icon: ReactNode;
  children: ReactNode;
  accent?: boolean;
  className?: string;
  extra?: ReactNode;
}) {
  return (
    <Card
      className={["settings-panel", accent ? "settings-panel-accent" : "", className].filter(Boolean).join(" ")}
      title={<Space size={8}>{icon}<span>{title}</span></Space>}
      extra={extra}
    >
      {children}
    </Card>
  );
}

export function SaveSettingsButton({
  loading,
  disabledReason,
  onClick
}: {
  loading: boolean;
  disabledReason: string;
  onClick: () => void;
}) {
  const disabled = Boolean(disabledReason);
  return (
    <Tooltip title={disabledReason || "保存当前页设置"}>
      <span className="tooltip-button-wrapper">
        <Button type="primary" icon={<Save size={16} />} onClick={onClick} loading={loading} disabled={disabled}>
          保存当前页
        </Button>
      </span>
    </Tooltip>
  );
}

export function NumberItem({
  label,
  value,
  onChange,
  suffix,
  min,
  max
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  suffix?: string;
  min?: number;
  max?: number;
}) {
  const inputId = useId();
  return (
    <Form.Item label={label} htmlFor={inputId}>
      <InputNumber
        id={inputId}
        aria-label={label}
        min={min}
        max={max}
        value={value}
        suffix={suffix}
        onChange={(next) => onChange(Number(next || 0))}
      />
    </Form.Item>
  );
}
