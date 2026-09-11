import { App, Button, Card, Checkbox, Drawer, Dropdown, Empty, Form, Input, Modal, Segmented, Skeleton, Space, Switch, Tag } from "antd";
import type { MenuProps } from "antd";
import { Clipboard, Download, Info, KeyRound, Monitor, MoreHorizontal, Pencil, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import type { ProbeRunner, ProbeRunnerPayload, RelayStatus, RunnerProvisionResult, RunnerUpdateStatus } from "../../types";
import { copyTextToClipboard, formatDate } from "../../utils";
import { api } from "../../api";
import { StructuredViewer } from "../../components/StructuredViewer";
import { SettingsPanel } from "./panelPrimitives";

export function RunnerNodePanel() {
  const { message } = App.useApp();
  const [runners, setRunners] = useState<ProbeRunner[]>([]);
  const [relayStatus, setRelayStatus] = useState<RelayStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [createMode, setCreateMode] = useState<RunnerCreateMode>("manual");
  const [provisionPlatform, setProvisionPlatform] = useState<RunnerProvisionPlatform>("linux");
  const [provisionComposeUrl, setProvisionComposeUrl] = useState("");
  const [provisionResult, setProvisionResult] = useState<RunnerProvisionResult | null>(null);
  const [draft, setDraft] = useState<ProbeRunnerPayload>(emptyRunnerDraft);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [authRunner, setAuthRunner] = useState<{ runner: ProbeRunner; token: string } | null>(null);
  const [editRunner, setEditRunner] = useState<{ runner: ProbeRunner; draft: ProbeRunnerPayload } | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);

  useEffect(() => {
    void loadRunners();
  }, []);

  async function loadRunners() {
    setLoading(true);
    try {
      const [nextRunners, nextRelayStatus] = await Promise.all([api.runners(), api.relayStatus()]);
      setRunners(nextRunners);
      setRelayStatus(nextRelayStatus);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  function openCreateDrawer() {
    setDraft(emptyRunnerDraft);
    setCreateMode("manual");
    setProvisionPlatform("linux");
    setProvisionComposeUrl("");
    setDrawerOpen(true);
  }

  function openEditDrawer(runner: ProbeRunner) {
    setEditRunner({
      runner,
      draft: {
        name: runner.name || runner.runner_id,
        address: runner.address || "",
        network_region: runner.network_region || "local",
        enabled: runner.enabled,
        allow_insecure_http: Boolean(runner.allow_insecure_http)
      }
    });
  }

  async function createRunner() {
    if (createMode === "relay") {
      const payload = {
        name: draft.name.trim(),
        network_region: draft.network_region.trim() || "local",
        target_platform: provisionPlatform,
        compose_url: provisionComposeUrl.trim(),
        enabled: draft.enabled !== false
      };
      if (!payload.name || !payload.compose_url) {
        message.error("请填写节点名称和 Compose URL");
        return;
      }
      setSaving(true);
      try {
        const result = await api.provisionRunner(payload);
        setProvisionResult(result);
        setDrawerOpen(false);
        await loadRunners();
        message.success("部署命令已生成");
      } catch (err) {
        message.error((err as Error).message);
      } finally {
        setSaving(false);
      }
      return;
    }
    const payload: ProbeRunnerPayload = {
      name: draft.name.trim(),
      address: draft.address.trim(),
      network_region: draft.network_region.trim() || "local",
      enabled: draft.enabled !== false,
      token: draft.token?.trim() || "",
      allow_insecure_http: runnerAddressUsesInsecureHttp(draft.address) && Boolean(draft.allow_insecure_http)
    };
    if (!payload.name || !payload.address || !payload.token) {
      message.error("请填写节点名称、地址和认证信息");
      return;
    }
    if (runnerAddressUsesInsecureHttp(payload.address) && !payload.allow_insecure_http) {
      message.error("请确认手工 HTTP 节点的明文传输风险，或改用 HTTPS / Relay");
      return;
    }
    setSaving(true);
    try {
      await api.createRunner(payload);
      setDrawerOpen(false);
      await loadRunners();
      message.success("执行节点已创建");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function updateRunnerEnabled(runner: ProbeRunner, enabled: boolean) {
    setBusyId(runner.runner_id);
    try {
      await api.updateRunner(runner.runner_id, { enabled });
      await loadRunners();
      message.success(enabled ? "节点已启用" : "节点已停用");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function saveRunnerEdit() {
    if (!editRunner) return;
    const payload: ProbeRunnerPayload = {
      name: editRunner.draft.name.trim(),
      address: editRunner.draft.address.trim(),
      network_region: editRunner.draft.network_region.trim() || "local",
      enabled: editRunner.draft.enabled !== false,
      allow_insecure_http:
        editRunner.runner.role === "child" &&
        editRunner.runner.connection_mode !== "relay" &&
        runnerAddressUsesInsecureHttp(editRunner.draft.address) &&
        Boolean(editRunner.draft.allow_insecure_http)
    };
    if (!payload.name || !payload.address) {
      message.error("请填写节点名称和地址");
      return;
    }
    if (
      editRunner.runner.role === "child" &&
      editRunner.runner.connection_mode !== "relay" &&
      runnerAddressUsesInsecureHttp(payload.address) &&
      !payload.allow_insecure_http
    ) {
      message.error("请确认手工 HTTP 节点的明文传输风险，或改用 HTTPS / Relay");
      return;
    }
    setSaving(true);
    setBusyId(editRunner.runner.runner_id);
    try {
      await api.updateRunner(editRunner.runner.runner_id, payload);
      setEditRunner(null);
      await loadRunners();
      message.success("执行节点已更新");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSaving(false);
      setBusyId(null);
    }
  }

  async function testRunner(runner: ProbeRunner) {
    setBusyId(runner.runner_id);
    try {
      const result = await api.testRunner(runner.runner_id);
      await loadRunners();
      message.success(result.message || "节点连接正常");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function updateRunnerToken() {
    if (!authRunner) return;
    const token = authRunner.token.trim();
    if (!token) {
      message.error("请填写认证信息");
      return;
    }
    setBusyId(authRunner.runner.runner_id);
    try {
      await api.updateRunner(authRunner.runner.runner_id, { token });
      setAuthRunner(null);
      await loadRunners();
      message.success("节点认证已更新");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function deleteRunner(runner: ProbeRunner) {
    setBusyId(runner.runner_id);
    try {
      await api.deleteRunner(runner.runner_id);
      await loadRunners();
      message.success("执行节点已删除");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  function confirmRunnerDelete(runner: ProbeRunner) {
    Modal.confirm({
      title: "删除执行节点",
      content: "删除后，仍引用该节点的任务会在执行时回退到可用节点。",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => deleteRunner(runner)
    });
  }

  async function copyProvisionCommand(result: RunnerProvisionResult) {
    try {
      await copyTextToClipboard(result.deployment.command);
      message.success("部署命令已复制");
    } catch {
      message.error("复制失败，请手动选择命令");
    }
  }

  async function regenerateProvision(runner: ProbeRunner, platform: RunnerProvisionPlatform = "linux") {
    const composeUrl = window.prompt("请输入 relay worker compose URL", provisionComposeUrl.trim());
    if (composeUrl === null) return;
    const normalizedComposeUrl = composeUrl.trim();
    if (!normalizedComposeUrl) {
      message.error("请填写 Compose URL");
      return;
    }
    setProvisionComposeUrl(normalizedComposeUrl);
    setBusyId(runner.runner_id);
    try {
      const result = await api.regenerateRunnerProvision(runner.runner_id, {
        name: runner.name || runner.runner_id,
        network_region: runner.network_region || "local",
        target_platform: platform,
        compose_url: normalizedComposeUrl,
        enabled: runner.enabled
      });
      setProvisionResult(result);
      await loadRunners();
      message.success("部署命令已重新生成");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  function confirmRunnerUpdate(runner: ProbeRunner) {
    const targetImage = runnerMetaText(runner, "update_target_image");
    const currentImage = runnerMetaText(runner, "image");
    Modal.confirm({
      title: "更新执行节点",
      content: (
        <Space orientation="vertical" size={8}>
          <span>主节点会向子节点下发受控更新请求，由子节点 updater 拉取镜像并重建 worker 服务。</span>
          <span>当前镜像：{currentImage || "-"}</span>
          <span>目标镜像：{targetImage || "子节点默认目标镜像"}</span>
        </Space>
      ),
      okText: "下发更新",
      cancelText: "取消",
      onOk: async () => {
        setBusyId(runner.runner_id);
        try {
          const result = await api.updateRunnerNode(runner.runner_id, targetImage ? { target_image: targetImage } : {});
          const updateId = result.worker?.update?.update_id;
          message.success(result.message || "节点更新任务已下发，正在等待结果");
          const finalStatus = await waitForRunnerUpdateStatus(runner.runner_id, updateId);
          if (finalStatus?.status === "succeeded") {
            message.success("节点更新成功");
          } else if (finalStatus?.status === "failed") {
            message.error(finalStatus.message || "节点更新失败");
          } else {
            message.warning("节点更新仍在执行，请稍后查看更新状态");
          }
          await loadRunners();
        } catch (err) {
          message.error((err as Error).message);
        } finally {
          setBusyId(null);
        }
      }
    });
  }

  async function showRunnerUpdateStatus(runner: ProbeRunner) {
    setBusyId(runner.runner_id);
    try {
      const result = await api.runnerUpdateStatus(runner.runner_id);
      Modal.info({
        title: "节点更新状态",
        width: 620,
        content: <StructuredViewer value={updateStatusLines(result.worker?.update)} />
      });
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <SettingsPanel title="执行节点" icon={<Monitor size={18} />} className="settings-panel-wide">
      <Space orientation="vertical" size={14} className="drawer-stack">
        <div className="read-only-token-toolbar">
          <Space size={8} wrap>
            <strong>节点列表</strong>
            <Tag color="processing">{runners.length} 个节点</Tag>
          </Space>
          <Space size={8} wrap>
            <Button icon={<Info size={16} />} onClick={() => setHelpOpen(true)}>
              部署方法
            </Button>
            <Button icon={<RefreshCw size={16} />} loading={loading} onClick={loadRunners}>
              刷新
            </Button>
            <Button type="primary" icon={<Plus size={16} />} onClick={openCreateDrawer}>
              新增子节点
            </Button>
          </Space>
        </div>

        {loading ? (
          <Skeleton active paragraph={{ rows: 4 }} />
        ) : runners.length === 0 ? (
          <Empty description="暂无执行节点" />
        ) : (
          <Space orientation="vertical" size={10} className="drawer-stack">
            {runners.map((runner) => {
              const version = runnerMetaText(runner, "version");
              const buildSha = runnerMetaText(runner, "build_sha");
              const image = runnerMetaText(runner, "image");
              const targetImage = runnerMetaText(runner, "update_target_image");
              const updateSupported = runnerMetaBool(runner, "update_supported");
              const updateAvailable = runnerMetaBool(runner, "update_available");
              const updateActionEnabled = updateSupported && updateAvailable && runner.available;
              const updateActionLabel = updateAvailable ? (
                <Space size={6}>
                  <span>更新节点</span>
                  <Tag color="warning">可更新</Tag>
                </Space>
              ) : (
                "更新节点"
              );
              const isBusy = busyId === runner.runner_id;
              const tokenStatus = runner.token_set ? runner.token_hint || "已配置" : "未配置";
              const imageSummary = image ? runnerImageSummary(image) : "-";
              const buildSummary = buildSha && buildSha !== "unknown" ? buildSha.slice(0, 8) : "-";
              const runnerMenuItems: MenuProps["items"] = [
                { key: "auth", icon: <KeyRound size={15} />, label: "更新认证" },
                ...(runner.role !== "local" && runner.connection_mode === "relay"
                  ? [{ key: "deploy", icon: <Clipboard size={15} />, label: "重新生成命令" }]
                  : []),
                { key: "update", icon: <Download size={15} />, label: updateActionLabel, disabled: !updateActionEnabled },
                { key: "status", icon: <Info size={15} />, label: "更新状态", disabled: !updateSupported },
                { type: "divider" },
                { key: "delete", icon: <Trash2 size={15} />, label: "删除", danger: true }
              ];
              const onRunnerMenuClick: MenuProps["onClick"] = ({ key }) => {
                if (key === "auth") setAuthRunner({ runner, token: "" });
                if (key === "deploy") void regenerateProvision(runner, "linux");
                if (key === "update") confirmRunnerUpdate(runner);
                if (key === "status") void showRunnerUpdateStatus(runner);
                if (key === "delete") confirmRunnerDelete(runner);
              };
              return (
                <Card size="small" key={runner.runner_id} className="runner-node-card">
                  <div className="runner-node-card-shell">
                    <div className="runner-node-main">
                      <div className="runner-node-heading">
                        <div className="runner-node-title-row">
                          <strong>{runner.name || runner.runner_id}</strong>
                          <Space size={6} wrap className="runner-node-tags">
                            <Tag>{runner.role === "local" ? "本机" : "子节点"}</Tag>
                            {runner.role !== "local" && <Tag>{runner.connection_mode === "relay" ? "Relay 自动" : "手动直连"}</Tag>}
                            {runner.role !== "local" &&
                              runner.connection_mode !== "relay" &&
                              runnerAddressUsesInsecureHttp(runner.address) && (
                                <Tag color={runner.allow_insecure_http ? "warning" : "error"}>
                                  {runner.allow_insecure_http ? "HTTP 风险已确认" : "HTTP 未确认"}
                                </Tag>
                              )}
                            <Tag color={runner.enabled ? "success" : "default"}>{runner.enabled ? "已启用" : "已停用"}</Tag>
                            <Tag color={runnerStatusColor(runner)}>{runnerStatusLabel(runner)}</Tag>
                            <Tag>{runner.network_region || "local"}</Tag>
                            {version && <Tag color="blue">v{version}</Tag>}
                            {runner.role !== "local" && (
                              <Tag color={updateSupported ? (updateAvailable ? "warning" : "processing") : "default"}>
                                {updateSupported ? (updateAvailable ? "有可更新镜像" : "支持平台更新") : "未启用 updater"}
                              </Tag>
                            )}
                          </Space>
                        </div>
                        <span className="runner-node-address">
                          {runner.address || "-"} · 最近心跳 {runner.last_seen_at ? formatDate(runner.last_seen_at) : "无"}
                        </span>
                      </div>
                      <div className="runner-node-facts">
                        <div className="runner-node-fact">
                          <span>认证</span>
                          <strong>{runner.connection_mode === "relay" && runner.relay_token_hint ? `${tokenStatus} / ${runner.relay_token_hint}` : tokenStatus}</strong>
                        </div>
                        <div className="runner-node-fact">
                          <span>镜像</span>
                          <strong>{imageSummary}</strong>
                        </div>
                        <div className="runner-node-fact">
                          <span>构建</span>
                          <strong>{buildSummary}</strong>
                        </div>
                      </div>
                      {runner.connection_mode === "relay" && runner.deploy_command_expires_at && (
                        <span className="runner-node-target">部署命令过期时间 {formatDate(runner.deploy_command_expires_at)}</span>
                      )}
                      {runner.connection_mode === "relay" && runner.last_disconnect_reason && (
                        <span className="runner-node-target">断开原因 {runner.last_disconnect_reason}</span>
                      )}
                      {targetImage && targetImage !== image && <span className="runner-node-target">目标镜像 {runnerImageSummary(targetImage)}</span>}
                    </div>
                    <div className="runner-node-controls">
                      <div className="runner-node-toggle">
                        <span>{runner.enabled ? "启用" : "停用"}</span>
                        <Switch checked={runner.enabled} loading={isBusy} onChange={(enabled) => updateRunnerEnabled(runner, enabled)} aria-label={`切换执行节点 ${runner.runner_id}`} />
                      </div>
                      <div className="runner-node-actions">
                        <Button size="small" icon={<Pencil size={15} />} loading={isBusy} onClick={() => openEditDrawer(runner)}>
                          编辑
                        </Button>
                        <Button size="small" loading={isBusy} onClick={() => testRunner(runner)}>
                          测试
                        </Button>
                        {runner.role !== "local" && (
                          <Dropdown menu={{ items: runnerMenuItems, onClick: onRunnerMenuClick }} trigger={["click"]}>
                            <Button size="small" type={updateActionEnabled ? "primary" : "default"} icon={<MoreHorizontal size={15} />} loading={isBusy}>
                              更多
                            </Button>
                          </Dropdown>
                        )}
                      </div>
                    </div>
                  </div>
                </Card>
              );
            })}
          </Space>
        )}
      </Space>

      <Drawer
        title="新增子节点"
        size={520}
        open={drawerOpen}
        destroyOnHidden
        onClose={() => setDrawerOpen(false)}
        extra={
          <Button type="primary" icon={<Save size={16} />} loading={saving} onClick={createRunner}>
            {createMode === "relay" ? "生成" : "创建"}
          </Button>
        }
      >
        <Form layout="vertical" autoComplete="off">
          <Form.Item label="接入模式">
            <Segmented
              block
              value={createMode}
              onChange={(value) => setCreateMode(value as RunnerCreateMode)}
              options={[
                { label: "手动添加", value: "manual" },
                { label: "生成部署命令", value: "relay" }
              ]}
            />
          </Form.Item>
          <Form.Item label="节点名称" required>
            <Input value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} />
          </Form.Item>
          {createMode === "manual" && (
            <>
              <Form.Item label="节点地址" required help="优先填写 HTTPS 地址；裸 IP/域名会按 worker 默认端口补成 http://地址:8788。">
                <Input
                  value={draft.address}
                  placeholder="10.0.0.12 或 http://10.0.0.12:8788"
                  onChange={(event) => setDraft((current) => ({ ...current, address: event.target.value }))}
                />
              </Form.Item>
              {runnerAddressUsesInsecureHttp(draft.address) && (
                <Form.Item>
                  <Checkbox
                    checked={Boolean(draft.allow_insecure_http)}
                    onChange={(event) =>
                      setDraft((current) => ({ ...current, allow_insecure_http: event.target.checked }))
                    }
                  >
                    我确认该节点位于可信网络，允许 token、任务参数和环境变量通过明文 HTTP 传输
                  </Checkbox>
                </Form.Item>
              )}
              <Form.Item label="认证信息" required help="粘贴 pgrn_ 开头的 token；也可以粘贴日志里的 token: pgrn_... 整行。">
                <Input.Password
                  value={draft.token}
                  placeholder="pgrn_..."
                  onChange={(event) => setDraft((current) => ({ ...current, token: event.target.value }))}
                />
              </Form.Item>
            </>
          )}
          {createMode === "relay" && (
            <>
              <Form.Item label="目标平台">
                <Segmented
                  block
                  value={provisionPlatform}
                  onChange={(value) => setProvisionPlatform(value as RunnerProvisionPlatform)}
                  options={[
                    { label: "Linux shell", value: "linux" },
                    { label: "PowerShell", value: "powershell" }
                  ]}
                />
              </Form.Item>
              <Form.Item label="Relay 状态">
                <Space size={8} wrap>
                  <Tag color={relayStatus?.enabled ? "success" : "warning"}>{relayStatus?.enabled ? "可生成命令" : "未就绪"}</Tag>
                  <span>{relayStatus?.public_url || relayStatus?.error || "-"}</span>
                </Space>
              </Form.Item>
              <Form.Item label="Compose URL" required help="填写 worker 宿主机可访问的 docker-compose.relay-worker.yml 下载地址。">
                <Input
                  value={provisionComposeUrl}
                  placeholder="https://example.com/docker-compose.relay-worker.yml"
                  onChange={(event) => setProvisionComposeUrl(event.target.value)}
                />
              </Form.Item>
            </>
          )}
          <Form.Item label="网络区域">
            <Input
              value={draft.network_region}
              placeholder="local"
              onChange={(event) => setDraft((current) => ({ ...current, network_region: event.target.value }))}
            />
          </Form.Item>
          <Form.Item label="启用状态">
            <Switch checked={draft.enabled !== false} onChange={(enabled) => setDraft((current) => ({ ...current, enabled }))} />
          </Form.Item>
        </Form>
      </Drawer>

      <Drawer
        title="编辑执行节点"
        size={520}
        open={Boolean(editRunner)}
        destroyOnHidden
        onClose={() => setEditRunner(null)}
        extra={
          <Button type="primary" icon={<Save size={16} />} loading={saving} onClick={saveRunnerEdit}>
            保存
          </Button>
        }
      >
        <Form layout="vertical" autoComplete="off">
          <Form.Item label="节点名称" required>
            <Input
              value={editRunner?.draft.name || ""}
              onChange={(event) =>
                setEditRunner((current) =>
                  current ? { ...current, draft: { ...current.draft, name: event.target.value } } : current
                )
              }
            />
          </Form.Item>
          <Form.Item label="节点地址" required help="可填写裸 IP/域名，系统会按 worker 默认端口补成 http://地址:8788。">
            <Input
              value={editRunner?.draft.address || ""}
              placeholder={editRunner?.runner.role === "local" ? "127.0.0.1" : "10.0.0.12 或 http://10.0.0.12:8788"}
              onChange={(event) =>
                setEditRunner((current) =>
                  current ? { ...current, draft: { ...current.draft, address: event.target.value } } : current
                )
              }
            />
          </Form.Item>
          {editRunner?.runner.role === "child" &&
            editRunner.runner.connection_mode !== "relay" &&
            runnerAddressUsesInsecureHttp(editRunner.draft.address) && (
              <Form.Item>
                <Checkbox
                  checked={Boolean(editRunner.draft.allow_insecure_http)}
                  onChange={(event) =>
                    setEditRunner((current) =>
                      current
                        ? {
                            ...current,
                            draft: { ...current.draft, allow_insecure_http: event.target.checked }
                          }
                        : current
                    )
                  }
                >
                  我确认该节点位于可信网络，允许 token、任务参数和环境变量通过明文 HTTP 传输
                </Checkbox>
              </Form.Item>
            )}
          <Form.Item label="网络区域">
            <Input
              value={editRunner?.draft.network_region || ""}
              placeholder="local"
              onChange={(event) =>
                setEditRunner((current) =>
                  current ? { ...current, draft: { ...current.draft, network_region: event.target.value } } : current
                )
              }
            />
          </Form.Item>
          <Form.Item label="启用状态">
            <Switch
              checked={editRunner?.draft.enabled !== false}
              onChange={(enabled) =>
                setEditRunner((current) => (current ? { ...current, draft: { ...current.draft, enabled } } : current))
              }
            />
          </Form.Item>
        </Form>
      </Drawer>

      <Modal
        title="更新节点认证"
        open={Boolean(authRunner)}
        okText="保存"
        cancelText="取消"
        confirmLoading={Boolean(authRunner && busyId === authRunner.runner.runner_id)}
        onOk={updateRunnerToken}
        onCancel={() => setAuthRunner(null)}
      >
        <Form layout="vertical" autoComplete="off">
          <Form.Item label="认证信息" required help="粘贴 pgrn_ 开头的 token；也可以粘贴日志里的 token: pgrn_... 整行。">
            <Input.Password
              value={authRunner?.token || ""}
              placeholder="pgrn_..."
              onChange={(event) => setAuthRunner((current) => (current ? { ...current, token: event.target.value } : current))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="子节点部署命令"
        open={Boolean(provisionResult)}
        width={860}
        okText="复制命令"
        cancelText="关闭"
        onOk={() => provisionResult && copyProvisionCommand(provisionResult)}
        onCancel={() => setProvisionResult(null)}
      >
        <Space orientation="vertical" size={10} className="drawer-stack">
          <Space size={8} wrap>
            <Tag color="processing">{provisionResult?.runner_id}</Tag>
            <Tag>{provisionResult?.deployment.target_platform === "powershell" ? "PowerShell" : "Linux shell"}</Tag>
            {provisionResult?.deployment.compose_url && <Tag>{provisionResult.deployment.compose_url}</Tag>}
            <span>过期时间 {provisionResult?.deployment.expires_at ? formatDate(provisionResult.deployment.expires_at) : "-"}</span>
          </Space>
          {provisionResult?.deployment.server_fingerprint && (
            <div className="runner-provision-fingerprint">
              <span>Server fingerprint</span>
              <code>{provisionResult.deployment.server_fingerprint}</code>
            </div>
          )}
          <pre>{provisionResult?.deployment.command || ""}</pre>
          <span>命令只显示一次。重新生成会轮换 relay token；在线子节点会在部署窗口内继续使用上一版 token，新版本连接成功后旧 token 自动失效。标准部署命令默认启用 updater。</span>
        </Space>
      </Modal>

      <Modal title="创建子节点" open={helpOpen} footer={null} onCancel={() => setHelpOpen(false)} width={820}>
        <Space orientation="vertical" size={12} className="drawer-stack">
          <span>子节点支持两种接入方式：手动添加适合主节点能直接访问 worker 的网络；生成部署命令适合 worker 只能主动出站连接主节点 relay 的网络。</span>
          <div className="worker-help-block">
            <strong>Relay 自动接入</strong>
            <pre>{`docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d`}</pre>
            <span>主节点启用 relay 后，在新增子节点里选择“生成部署命令”，复制命令到 worker 宿主机执行。worker 不需要开放 8788 入站端口，部署命令默认启动 updater。</span>
          </div>
          <span>手动添加模式优先使用 HTTPS。仅在可信内网无法提供 HTTPS 时，才显式确认使用明文 HTTP；否则使用 Relay 自动接入。</span>
          <div className="worker-help-block">
            <strong>推荐部署：公司 Git 源码构建（Windows PowerShell）</strong>
            <pre>{`git clone git@git.corpautohome.com:liyanqing/PulseGuard.git
cd PulseGuard
$env:PULSEGUARD_WORKER_NAME = $env:COMPUTERNAME
$env:PULSEGUARD_WORKER_REGION = "default"
$env:COMPOSE_PROJECT_NAME = "pulseguard-worker"
$env:COMPOSE_PROFILES = "updater"
$env:PULSEGUARD_WORKER_UPDATER_URL = "http://pulseguard-worker-updater:8790"
docker compose -f docker-compose.worker.yml -f docker-compose.worker.build.yml up --build -d
docker update --restart unless-stopped pulseguard-worker pulseguard-worker-updater
docker logs --tail 80 pulseguard-worker`}</pre>
            <span>这条命令会从公司 Git 项目构建 worker 和 updater，后台启动容器并设置 Docker 自启策略。</span>
          </div>
          <div className="worker-help-block">
            <strong>管理平台推送更新（默认启用）</strong>
            <pre>{`$env:COMPOSE_PROJECT_NAME = "pulseguard-worker"
$env:COMPOSE_PROFILES = "updater"
$env:PULSEGUARD_WORKER_UPDATER_URL = "http://pulseguard-worker-updater:8790"
$env:PULSEGUARD_WORKER_UPDATE_IMAGE = "pulseguard-worker:local"
docker compose -f docker-compose.worker.yml -f docker-compose.worker.build.yml up --build -d
docker update --restart unless-stopped pulseguard-worker pulseguard-worker-updater`}</pre>
            <span>updater 会后台运行并跟随 Docker 自启；它会挂载宿主机 Docker socket，只允许更新当前 worker 服务，不支持任意命令。</span>
          </div>
          <div className="worker-help-block">
            <strong>内网镜像部署（可选）</strong>
            <pre>{`$env:PULSEGUARD_WORKER_IMAGE = "registry.example.com/pulseguard-worker:local"
$env:PULSEGUARD_WORKER_UPDATE_IMAGE = "registry.example.com/pulseguard-worker:local"
docker compose -f docker-compose.worker.yml up -d
docker update --restart unless-stopped pulseguard-worker
docker logs --tail 80 pulseguard-worker`}</pre>
            <span>子节点不能访问公司 Git 项目时，先在可访问环境构建并推送到内网镜像仓库，再替换镜像地址。</span>
          </div>
          <div className="worker-help-block">
            <strong>刷新子节点 token</strong>
            <pre>{`docker compose -f docker-compose.worker.yml exec pulseguard-worker python -m app.worker --rotate-token
docker compose -f docker-compose.worker.yml restart pulseguard-worker`}</pre>
          </div>
          <span>添加时地址通常是 <code>http://&lt;子节点 IP&gt;:8788</code>。保存后先测试连接，测试通过后再把任务绑定到该节点。</span>
        </Space>
      </Modal>
    </SettingsPanel>
  );
}

const emptyRunnerDraft: ProbeRunnerPayload = {
  name: "",
  address: "",
  network_region: "local",
  enabled: true,
  token: "",
  allow_insecure_http: false
};

type RunnerCreateMode = "manual" | "relay";

type RunnerProvisionPlatform = "linux" | "powershell";

function runnerStatusLabel(runner: ProbeRunner): string {
  if (!runner.enabled) return "已停用";
  const status = runner.status || "";
  const labels: Record<string, string> = {
    pending_deployment: "待部署",
    expired: "已过期",
    connecting: "连接中",
    updating: "更新中",
    available: "可用",
    unhealthy: "异常",
    unavailable: "异常",
    auth_failed: "认证失败",
    ok: "可用",
    warning: "异常",
    offline: "异常",
    disabled: "已停用"
  };
  return labels[status] || (runner.available ? "可用" : "异常");
}

function runnerStatusColor(runner: ProbeRunner): "default" | "processing" | "success" | "warning" | "error" {
  if (!runner.enabled) return "default";
  if (runner.available || runner.status === "available" || runner.status === "ok") return "success";
  if (runner.status === "pending_deployment" || runner.status === "connecting" || runner.status === "updating") return "processing";
  if (runner.status === "expired" || runner.status === "auth_failed") return "warning";
  return "error";
}

function runnerMetaText(runner: ProbeRunner, key: string): string {
  const value = runner.metadata?.[key];
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}

function runnerMetaBool(runner: ProbeRunner, key: string): boolean {
  return runner.metadata?.[key] === true;
}

function runnerImageSummary(image: string): string {
  if (!image) return "-";
  const parts = image.split("/");
  return parts[parts.length - 1] || image;
}

function runnerAddressUsesInsecureHttp(address: string): boolean {
  const value = address.trim().toLowerCase();
  return Boolean(value) && (value.startsWith("http://") || !value.includes("://"));
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function waitForRunnerUpdateStatus(runnerId: string, updateId?: string): Promise<RunnerUpdateStatus | undefined> {
  let latest: RunnerUpdateStatus | undefined;
  for (let attempt = 0; attempt < 75; attempt += 1) {
    await wait(attempt === 0 ? 1000 : 2000);
    const result = await api.runnerUpdateStatus(runnerId);
    const update = result.worker?.update;
    if (!update) continue;
    if (updateId && update.update_id && update.update_id !== updateId) continue;
    latest = update;
    if (latest.status === "succeeded" || latest.status === "failed") return latest;
  }
  return latest;
}

function updateStatusLines(status?: RunnerUpdateStatus): Record<string, unknown> {
  if (!status) return { 状态: "暂无状态" };
  return {
    状态: status.status || "-",
    当前镜像: status.current_image || "-",
    目标镜像: status.target_image || "-",
    原镜像: status.previous_image || "-",
    更新服务: status.update_services?.join(", ") || "-",
    是否可更新: typeof status.update_available === "boolean" ? (status.update_available ? "是" : "否") : "-",
    开始时间: status.started_at || "-",
    完成时间: status.finished_at || "-",
    更新时间: status.updated_at || "-",
    消息: status.message || "-"
  };
}
