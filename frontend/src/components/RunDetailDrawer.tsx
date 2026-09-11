import { Alert, Button, Drawer, Skeleton, Space } from "antd";
import { ExternalLink, ListChecks, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { Run } from "../types";
import { checkListPath } from "../utils";
import { RunResultPanel } from "./RunResultPanel";

interface Props {
  runId: number | null;
  onClose: () => void;
  onRerun?: (run: Run) => Promise<void> | void;
  returnTo?: string;
}

export function RunDetailDrawer({ runId, onClose, onRerun, returnTo }: Props) {
  const navigate = useNavigate();
  const [run, setRun] = useState<Run | null>(null);
  const [loading, setLoading] = useState(false);
  const [rerunning, setRerunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      setError(null);
      setLoading(false);
      return;
    }
    setRun(null);
    setLoading(true);
    setError(null);
    api
      .run(runId)
      .then(setRun)
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, [runId]);

  async function handleRerun() {
    if (!run || !onRerun || run.check_id <= 0) return;
    setRerunning(true);
    setError(null);
    try {
      await onRerun(run);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRerunning(false);
    }
  }

  return (
    <Drawer
      title={run?.check_name || "执行详情"}
      open={Boolean(runId)}
      onClose={onClose}
      size={840}
      destroyOnHidden
      extra={
        run ? (
          <Space wrap>
            <Button icon={<ExternalLink size={16} />} onClick={() => navigate(runDetailPath(run.id, returnTo))}>
              完整详情
            </Button>
            {run.check_id > 0 && (
              <Button icon={<ListChecks size={16} />} onClick={() => navigate(checkListPath(run.check_type, run.check_id))}>
                定位任务
              </Button>
            )}
            {onRerun && run.check_id > 0 && (
              <Button icon={<RefreshCw size={16} />} onClick={handleRerun} loading={rerunning}>
                重新执行
              </Button>
            )}
          </Space>
        ) : null
      }
    >
      {(loading || !run) && !error && <Skeleton active paragraph={{ rows: 8 }} />}
      {error && <Alert type="error" title={error} showIcon />}
      {!loading && !error && run && <RunResultPanel run={run} mode="detail" />}
    </Drawer>
  );
}

function runDetailPath(runId: number, returnTo?: string): string {
  if (!returnTo) return `/runs/${runId}`;
  return `/runs/${runId}?from=${encodeURIComponent(returnTo)}`;
}
