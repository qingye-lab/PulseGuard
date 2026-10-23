"""Run synchronous persistence operations without blocking the asyncio loop."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


async def call(operation: Callable[..., Any], *args: Any, _on_cancelled_result=None, **kwargs: Any) -> Any:
    # A connection/transaction remains wholly owned by one worker thread. Await
    # completion even on repeated cancellation: cleanup must not race a late write.
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning("storage_failed_during_cancellation error_type=%s", type(error).__name__)
            elif _on_cancelled_result is not None:
                # Allocation/claim callers cannot take ownership after their await
                # is cancelled. Settle the returned resource before propagating it.
                cleanup = asyncio.create_task(asyncio.to_thread(_on_cancelled_result, task.result()))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                cleanup.result()
        raise


def _cancel_created_run(run):
    from . import storage
    if run is not None:
        storage.finish_run(int(run['id']), {
            'status': 'skipped', 'finished_at': storage.now_iso(), 'duration_ms': 0,
            'error_message': '执行在任务接管前被取消', 'failure_kind': 'runner', 'affects_health': False,
        })
        storage.update_run_notification(int(run['id']), 'not_required', channel=None, error=None, sent_at=None)


async def create_run(*args, **kwargs):
    from . import storage
    return await call(storage.create_run, *args, _on_cancelled_result=_cancel_created_run, **kwargs)
