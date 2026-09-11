import { App, Button, Empty, Form, Input, Modal, Popconfirm, Skeleton, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Edit3, Plus, Trash2, Users } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { InitialLoadState } from "../components/InitialLoadState";
import type { Member } from "../types";

type MemberFormValues = Omit<Member, "id">;

const EMPTY_MEMBER: MemberFormValues = {
  name: "",
  feishu_open_id: "",
  wecom_user_id: "",
  wecom_mobile: "",
  dingtalk_user_id: "",
  dingtalk_mobile: ""
};

export function MembersPage() {
  const { message } = App.useApp();
  const [form] = Form.useForm<MemberFormValues>();
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<Member | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [isCompactList, setIsCompactList] = useState(() => window.matchMedia("(max-width: 720px)").matches);

  function loadMembers() {
    setLoading(true);
    api
      .settings()
      .then((values) => { setMembers(values.members || []); setLoaded(true); setLoadError(null); })
      .catch((err: Error) => setLoadError(err.message))
      .finally(() => setLoading(false));
  }
  useEffect(() => { loadMembers(); }, []);

  useEffect(() => {
    if (!modalOpen) return;
    form.setFieldsValue(editing || EMPTY_MEMBER);
  }, [editing, form, modalOpen]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 720px)");
    const sync = () => setIsCompactList(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  const channelCounts = useMemo(
    () => ({
      feishu: members.filter((member) => member.feishu_open_id).length,
      wecom: members.filter((member) => member.wecom_user_id || member.wecom_mobile).length,
      dingtalk: members.filter((member) => member.dingtalk_user_id || member.dingtalk_mobile).length
    }),
    [members]
  );

  function openCreate() {
    setEditing(null);
    setModalOpen(true);
  }

  function openEdit(member: Member) {
    setEditing(member);
    setModalOpen(true);
  }

  async function persist(nextMembers: Member[], successMessage: string) {
    setSaving(true);
    try {
      const values = await api.updateSettings({ members: nextMembers });
      setMembers(values.members || []);
      setModalOpen(false);
      message.success(successMessage);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function saveMember(values: MemberFormValues) {
    const normalized = normalizeMember(values);
    if (!hasAnyChannel(normalized)) {
      message.error("至少配置一个通知渠道账号");
      return;
    }
    const member = { id: editing?.id || createMemberId(), ...normalized };
    const nextMembers = editing
      ? members.map((item) => (item.id === editing.id ? member : item))
      : [...members, member];
    await persist(nextMembers, editing ? "成员已更新" : "成员已添加");
  }

  async function removeMember(memberId: string) {
    await persist(members.filter((member) => member.id !== memberId), "成员已删除，任务关联已同步清理");
  }

  function renderMemberActions(member: Member, compact = false) {
    return (
      <div className={compact ? "member-row-actions member-row-actions-compact" : "member-row-actions"}>
        <Tooltip title="编辑成员">
          <Button aria-label={`编辑成员 ${member.name}`} icon={<Edit3 size={15} />} onClick={() => openEdit(member)}>
            {compact ? "编辑" : undefined}
          </Button>
        </Tooltip>
        <Popconfirm
          title="删除成员"
          description="删除后会同步清理所有任务中的成员关联。"
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => removeMember(member.id)}
        >
          <Tooltip title="删除成员">
            <Button danger aria-label={`删除成员 ${member.name}`} icon={<Trash2 size={15} />}>
              {compact ? "删除" : undefined}
            </Button>
          </Tooltip>
        </Popconfirm>
      </div>
    );
  }

  const columns: ColumnsType<Member> = [
    {
      title: "成员",
      dataIndex: "name",
      width: 176,
      render: (name: string, member) => (
        <div className="member-name-cell">
          <strong>{name}</strong>
          <div className="member-mobile-channels">
            <ChannelAccount configured={Boolean(member.feishu_open_id)} detail={member.feishu_open_id} label="飞书" />
            <ChannelAccount
              configured={Boolean(member.wecom_user_id || member.wecom_mobile)}
              detail={[member.wecom_user_id, member.wecom_mobile].filter(Boolean).join(" / ")}
              label="企微"
            />
            <ChannelAccount
              configured={Boolean(member.dingtalk_user_id || member.dingtalk_mobile)}
              detail={[member.dingtalk_user_id, member.dingtalk_mobile].filter(Boolean).join(" / ")}
              label="钉钉"
            />
          </div>
        </div>
      )
    },
    {
      title: "飞书",
      width: 132,
      responsive: ["lg"],
      render: (_, member) => <ChannelAccount configured={Boolean(member.feishu_open_id)} detail={member.feishu_open_id} />
    },
    {
      title: "企业微信",
      width: 148,
      responsive: ["lg"],
      render: (_, member) => (
        <ChannelAccount
          configured={Boolean(member.wecom_user_id || member.wecom_mobile)}
          detail={[member.wecom_user_id, member.wecom_mobile].filter(Boolean).join(" / ")}
        />
      )
    },
    {
      title: "钉钉",
      width: 132,
      responsive: ["lg"],
      render: (_, member) => (
        <ChannelAccount
          configured={Boolean(member.dingtalk_user_id || member.dingtalk_mobile)}
          detail={[member.dingtalk_user_id, member.dingtalk_mobile].filter(Boolean).join(" / ")}
        />
      )
    },
    {
      title: "操作",
      align: "right",
      width: 96,
      render: (_, member) => renderMemberActions(member)
    }
  ];

  if (!loaded) return <div className="page-content"><InitialLoadState loading={loading} error={loadError} onRetry={loadMembers} /></div>;

  return (
    <div className="page-content members-page">
      <section className="members-command-bar">
        <div className="members-command-title">
          <Users size={18} />
          <strong>{members.length} 名成员</strong>
          <Tag>飞书 {channelCounts.feishu}</Tag>
          <Tag>企业微信 {channelCounts.wecom}</Tag>
          <Tag>钉钉 {channelCounts.dingtalk}</Tag>
        </div>
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
          添加成员
        </Button>
      </section>

      {isCompactList ? (
        <section className="members-compact-list" aria-label="成员列表">
          {loading ? (
            <Skeleton active paragraph={{ rows: 6 }} />
          ) : members.length ? (
            members.map((member) => (
              <article className="member-compact-card" key={member.id}>
                <div className="member-compact-header">
                  <div className="member-compact-title">
                    <strong>{member.name}</strong>
                    <span>通知账号</span>
                  </div>
                  {renderMemberActions(member, true)}
                </div>
                <div className="member-compact-channels">
                  <ChannelAccount configured={Boolean(member.feishu_open_id)} detail={member.feishu_open_id} label="飞书" />
                  <ChannelAccount
                    configured={Boolean(member.wecom_user_id || member.wecom_mobile)}
                    detail={[member.wecom_user_id, member.wecom_mobile].filter(Boolean).join(" / ")}
                    label="企微"
                  />
                  <ChannelAccount
                    configured={Boolean(member.dingtalk_user_id || member.dingtalk_mobile)}
                    detail={[member.dingtalk_user_id, member.dingtalk_mobile].filter(Boolean).join(" / ")}
                    label="钉钉"
                  />
                </div>
              </article>
            ))
          ) : (
            <Empty description="暂无成员" />
          )}
        </section>
      ) : (
        <Table
          rowKey="id"
          columns={columns}
          dataSource={members}
          loading={loading}
          pagination={false}
          locale={{ emptyText: <Empty description="暂无成员" /> }}
          className="members-table"
          tableLayout="fixed"
        />
      )}

      <Modal
        title={editing ? "编辑成员" : "添加成员"}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" onFinish={saveMember} initialValues={EMPTY_MEMBER} autoComplete="off">
          <Form.Item label="姓名" name="name" rules={[{ required: true, message: "请填写姓名" }]}>
            <Input maxLength={80} />
          </Form.Item>
          <div className="member-channel-section">
            <strong>飞书</strong>
            <Form.Item label="Open ID" name="feishu_open_id">
              <Input maxLength={120} />
            </Form.Item>
          </div>
          <div className="member-channel-section">
            <strong>企业微信</strong>
            <div className="field-grid two">
              <Form.Item label="User ID" name="wecom_user_id">
                <Input maxLength={120} />
              </Form.Item>
              <Form.Item label="手机号" name="wecom_mobile">
                <Input maxLength={32} />
              </Form.Item>
            </div>
          </div>
          <div className="member-channel-section">
            <strong>钉钉</strong>
            <div className="field-grid two">
              <Form.Item label="User ID" name="dingtalk_user_id">
                <Input maxLength={120} />
              </Form.Item>
              <Form.Item label="手机号" name="dingtalk_mobile">
                <Input maxLength={32} />
              </Form.Item>
            </div>
          </div>
        </Form>
      </Modal>
    </div>
  );
}

function ChannelAccount({ configured, detail, label }: { configured: boolean; detail: string; label?: string }) {
  const tag = <Tag color={configured ? "success" : "default"}>{label ? `${label} ${configured ? "已配" : "未配"}` : configured ? "已配置" : "未配置"}</Tag>;
  return configured ? <Tooltip title={detail}>{tag}</Tooltip> : tag;
}

function normalizeMember(values: MemberFormValues): MemberFormValues {
  return {
    name: (values.name || "").trim(),
    feishu_open_id: (values.feishu_open_id || "").trim(),
    wecom_user_id: (values.wecom_user_id || "").trim(),
    wecom_mobile: (values.wecom_mobile || "").trim(),
    dingtalk_user_id: (values.dingtalk_user_id || "").trim(),
    dingtalk_mobile: (values.dingtalk_mobile || "").trim()
  };
}

function hasAnyChannel(member: MemberFormValues): boolean {
  return Boolean(
    member.feishu_open_id ||
      member.wecom_user_id ||
      member.wecom_mobile ||
      member.dingtalk_user_id ||
      member.dingtalk_mobile
  );
}

function createMemberId(): string {
  return `member-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}
