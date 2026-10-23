"""Durable, at-most-once dispatch receipts for the single-instance runner.

Persistence makes submission retryable, not arbitrary target side effects exactly-once.
An interrupted running item stays unknown and is NEVER automatically dispatched again.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections import Counter
from typing import Any

from . import storage
from .async_storage import call as storage_call

logger = logging.getLogger(__name__)


class DispatchRejected(ValueError):
    """No probe was dispatched because the approved definition is stale."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def definition_hash(check: dict[str, Any]) -> str:
    return digest(storage._check_snapshot(check))


def init_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_batches (
        id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
        idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(project_id, idempotency_key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_batch_items (
        batch_id TEXT NOT NULL REFERENCES execution_batches(id), check_id INTEGER NOT NULL,
        definition_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        run_id INTEGER, result_status TEXT, message TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(batch_id, check_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_batch_items_state ON execution_batch_items(state, batch_id)")


def submit(project_id: int, key: str, check_ids: list[int]) -> dict[str, Any]:
    ids = sorted(set(check_ids))
    request_hash = digest(ids)
    with storage.write_transaction():
        with storage._connect() as conn:
            old = conn.execute("SELECT * FROM execution_batches WHERE project_id=? AND idempotency_key=?", (project_id, key)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise ValueError("幂等键已用于不同任务集合；请查询原回执")
                return get_batch(project_id, old["id"])
            if storage.is_deployment_window_active():
                raise ValueError("部署维护期间不能提交执行批次")
            project = storage.get_project(project_id)
            if not project or project["status"] != "active":
                raise ValueError("项目不存在或已归档")
            checks = [storage.get_check(i, refresh_stale=False, project_id=project_id) for i in ids]
            if not checks or any(not c or not c["enabled"] for c in checks):
                raise ValueError("批次包含不存在、跨项目或已禁用任务")
            pending = conn.execute("SELECT COUNT(*) FROM execution_batch_items WHERE state IN ('queued','running')").fetchone()[0]
            if pending + len(ids) > 1000:
                raise ValueError("批次队列已满，请等待现有任务完成")
            batch_id, timestamp = str(uuid.uuid4()), storage.now_iso()
            conn.execute("INSERT INTO execution_batches VALUES (?,?,?,?,?)", (batch_id, project_id, key, request_hash, timestamp))
            conn.executemany("""INSERT INTO execution_batch_items(batch_id,check_id,definition_hash,updated_at)
                VALUES (?,?,?,?)""", [(batch_id, c["id"], definition_hash(c), timestamp) for c in checks])
            storage.record_audit_event("submitted", "execution_batch", entity_name=batch_id,
                                       summary="提交探活批次", payload={"check_ids": ids}, project_id=project_id)
            return get_batch(project_id, batch_id)


def get_batch(project_id: int, batch_id: str | None = None, *, key: str | None = None) -> dict[str, Any] | None:
    with storage._connect(read_only=True) as conn:
        batch = conn.execute("SELECT * FROM execution_batches WHERE project_id=? AND " + ("id=?" if batch_id else "idempotency_key=?"),
                             (project_id, batch_id or key)).fetchone()
        if not batch:
            return None
        items = [dict(r) for r in conn.execute("""SELECT check_id,state,run_id,result_status,message,updated_at
            FROM execution_batch_items WHERE batch_id=? ORDER BY check_id""", (batch["id"],))]
        for item in items:
            item["trigger"] = f"mcp-batch:{batch['id']}:{item['check_id']}"
            item["run_ids"] = [r[0] for r in conn.execute("SELECT id FROM runs WHERE project_id=? AND check_id=? AND trigger=? ORDER BY id",
                (project_id, item["check_id"], item["trigger"]))]
    counts = dict(Counter(item["state"] for item in items))
    state = "running" if counts.get("running") else "queued" if counts.get("queued") else "unknown" if counts.get("unknown") else "finished"
    return {"id": batch["id"], "project_id": project_id, "idempotency_key": batch["idempotency_key"],
            "created_at": batch["created_at"], "state": state, "counts": counts, "items": items}


def cancel(project_id: int, batch_id: str) -> dict[str, Any] | None:
    with storage.write_transaction():
        if not get_batch(project_id, batch_id):
            return None
        with storage._connect() as conn:
            conn.execute("UPDATE execution_batch_items SET state='cancelled',updated_at=? WHERE batch_id=? AND state='queued'",
                         (storage.now_iso(), batch_id))
        storage.record_audit_event("cancelled", "execution_batch", entity_name=batch_id,
                                   summary="取消尚未派发的批次任务；已派发任务不撤销", project_id=project_id)
        return get_batch(project_id, batch_id)


def recover_interrupted() -> None:
    with storage._connect() as conn:
        conn.execute("""UPDATE execution_batch_items SET state='unknown',message='进程中断；禁止自动重试，请核对关联运行',
            updated_at=? WHERE state='running'""", (storage.now_iso(),))


def invalidate_after_restore() -> None:
    # A restored snapshot must not replay work that might have happened since it.
    recover_interrupted()
    with storage._connect() as conn:
        conn.execute("""UPDATE execution_batch_items SET state='cancelled',message='数据库已恢复；请重新确认执行范围',
            updated_at=? WHERE state='queued'""", (storage.now_iso(),))
        conn.execute("UPDATE check_change_drafts SET state='cancelled',candidate_json=NULL WHERE state='pending'")


def claim() -> dict[str, Any] | None:
    # Idle polling must not repeatedly acquire SQLite's single writer lock.
    with storage._connect(read_only=True) as conn:
        if not conn.execute("SELECT 1 FROM execution_batch_items WHERE state='queued' LIMIT 1").fetchone():
            return None
    with storage.write_transaction():
        if storage.is_deployment_window_active():
            return None
        with storage._connect() as conn:
            row = conn.execute("""SELECT i.*,b.project_id FROM execution_batch_items i
                JOIN execution_batches b ON b.id=i.batch_id WHERE i.state='queued'
                ORDER BY b.created_at,b.id,i.check_id LIMIT 1""").fetchone()
            if not row:
                return None
            conn.execute("UPDATE execution_batch_items SET state='running',updated_at=? WHERE batch_id=? AND check_id=?",
                         (storage.now_iso(), row["batch_id"], row["check_id"]))
            return dict(row)


def finish(item: dict[str, Any], state: str, result: dict | None = None, message: str | None = None) -> None:
    result = result or {}
    with storage._connect() as conn:
        conn.execute("""UPDATE execution_batch_items SET state=?,run_id=?,result_status=?,message=?,updated_at=?
            WHERE batch_id=? AND check_id=? AND state='running'""",
            (state, result.get("id"), result.get("status"), message, storage.now_iso(), item["batch_id"], item["check_id"]))


class BatchExecutor:
    def __init__(self, runner):
        self.runner = runner
        self.tasks: list[asyncio.Task] = []

    def start(self):
        recover_interrupted()
        self.tasks = [asyncio.create_task(self._loop()) for _ in range(2)]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def _loop(self):
        while True:
            if self.runner.runtime_status().get("maintenance_active"):
                await asyncio.sleep(0.5)
                continue
            try:
                item = (await storage_call(claim, _on_cancelled_result=lambda claimed:
                    finish(claimed, 'unknown', message='领取等待中断；请核对关联运行') if claimed else None))
            except Exception:
                logger.exception("Unable to claim a queued execution item")
                await asyncio.sleep(1)
                continue
            if item is None:
                await asyncio.sleep(0.5)
                continue
            try:
                result = await self.runner.run_check(item["check_id"], project_id=item["project_id"],
                    trigger=f"mcp-batch:{item['batch_id']}:{item['check_id']}", expected_definition_hash=item["definition_hash"])
                (await storage_call(finish, item, "completed", result))
            except DispatchRejected:
                (await storage_call(finish, item, "rejected", message="任务定义或启停状态已变化；未执行，请重新确认任务范围"))
            except asyncio.CancelledError:
                (await storage_call(finish, item, "unknown", message="执行等待中断；不要重试，请核对关联运行"))
                raise
            except Exception:
                # Even a runner error may follow an external side effect. Never requeue.
                (await storage_call(finish, item, "unknown", message="执行结果未知；请核对关联运行和目标状态"))
