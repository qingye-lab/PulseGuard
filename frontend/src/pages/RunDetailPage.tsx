import { Alert, Button, Card } from "antd";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { InitialLoadState } from "../components/InitialLoadState";
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
  const loadRequest = useRef(0);
  const currentRun = run?.id === id ? run : null;

  async function load(targetId = id) {
    const request = ++loadRequest.current;
    setLoading(true);
    try {
      const nextRun = await api.run(targetId);
      if (request !== loadRequest.current) return;
      setRun(nextRun);
      setError(null);
    } catch (err) {
      if (request !== loadRequest.current) return;
      setError((err as Error).message);
    } finally {
      if (request === loadRequest.current) setLoading(false);
    }
  }

  useEffect(() => {
    load(id);
    return () => { loadRequest.current += 1; };
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
            <h2>{currentRun?.check_name || `运行记录 #${id}`}</h2>
          </div>
          <Button icon={<RefreshCw size={16} />} onClick={rerun} loading={rerunning} disabled={readOnly || !currentRun || currentRun.check_id <= 0}>
            重新执行
          </Button>
        </div>
      </Card>

      {error && currentRun && <Alert type="error" title={error} showIcon />}

      <Card className="run-detail-card">
        {!currentRun ? <InitialLoadState loading={loading} error={error} onRetry={() => void load()} /> : <RunResultPanel run={currentRun} mode="detail" />}
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
