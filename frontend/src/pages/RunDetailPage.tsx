import { Alert, Button, Card, Skeleton } from "antd";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { RunResultPanel } from "../components/RunResultPanel";
import { useProject } from "../ProjectContext";
import type { Run } from "../types";

export function RunDetailPage() {
  const { currentProject } = useProject();
  const readOnly = currentProject.status === "archived";
  const { runId } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const id = Number(runId);
  const returnTo = safeReturnTo(searchParams.get("from"));
  const [run, setRun] = useState<Run | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [rerunning, setRerunning] = useState(false);

  async function load(targetId = id) {
    setLoading(true);
    try {
      setRun(await api.run(targetId));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(id);
  }, [id]);

  async function rerun() {
    if (!run || run.check_id <= 0) return;
    setRerunning(true);
    try {
      const latest = await api.rerun(run.id);
      navigate(runDetailPath(latest.id, returnTo));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRerunning(false);
    }
  }

  return (
    <div className="page-content run-detail-page">
      <Card>
        <div className="run-detail-header">
          <Button icon={<ArrowLeft size={16} />} onClick={() => navigate(returnTo)}>
            {returnTo.startsWith("/runs") ? "返回历史" : "返回来源"}
          </Button>
          <div>
            <h2>{run?.check_name || `运行记录 #${id}`}</h2>
          </div>
          <Button icon={<RefreshCw size={16} />} onClick={rerun} loading={rerunning} disabled={readOnly || !run || run.check_id <= 0}>
            重新执行
          </Button>
        </div>
      </Card>

      {error && <Alert type="error" title={error} showIcon />}

      <Card className="run-detail-card">
        {loading ? <Skeleton active paragraph={{ rows: 8 }} /> : <RunResultPanel run={run} mode="detail" />}
      </Card>
    </div>
  );
}

function safeReturnTo(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("://")) {
    return "/runs";
  }
  return value;
}

function runDetailPath(runId: number, returnTo: string): string {
  return `/runs/${runId}?from=${encodeURIComponent(returnTo)}`;
}
