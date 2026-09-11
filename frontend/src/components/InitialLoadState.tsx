import { Alert, Button, Skeleton } from "antd";

/** Render only before a resource has produced its first successful response. */
export function InitialLoadState({ error, loading, onRetry }: {
  error: string | null;
  loading: boolean;
  onRetry: () => void;
}) {
  if (error && !loading) {
    return <Alert type="error" showIcon title="加载失败" description={error}
      action={<Button onClick={onRetry}>重新加载</Button>} />;
  }
  return <div role="status" aria-label="正在加载" aria-busy="true"><Skeleton active paragraph={{ rows: 6 }} /></div>;
}
