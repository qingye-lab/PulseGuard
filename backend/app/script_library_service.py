"""Asynchronous orchestration for project script-library validation.

The storage module owns the immutable candidate and dependency snapshot.  This
module only selects the affected checks and hands each frozen check to the
runtime runner, which owns real runner/browser dispatch and non-observational
run creation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import storage
from .async_storage import call as storage_call
from .script_library import ModuleLibraryError, referenced_module_names


logger = logging.getLogger(__name__)


def _diagnostic(code: str, message: str, *, check_id: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"level": "error", "code": code, "message": str(message)}
    if check_id is not None:
        result["check_id"] = int(check_id)
    return result


def _run_status(result: Any) -> str:
    if not isinstance(result, dict):
        return "failed"
    status = str(result.get("status") or "failed").strip().lower()
    runs = result.get("runs")
    if not isinstance(runs, list) or not runs:
        return "failed"
    if any(not isinstance(run, dict) or str(run.get("status") or "failed").strip().lower() != "ok" for run in runs):
        return "failed"
    return "ok" if status == "ok" else "failed"


def _with_result_identity(result: Any, check: dict[str, Any]) -> dict[str, Any]:
    """Keep the complete runtime response while ensuring publish can index it."""

    payload = dict(result) if isinstance(result, dict) else {}
    check_id = int(check.get("id") or 0)
    payload.setdefault("check_id", check_id)
    payload.setdefault("original_check_id", check_id)
    runs = payload.get("runs")
    payload["runs"] = runs if isinstance(runs, list) else []
    if str(payload.get("status") or "").lower() not in {"ok", "failed", "timeout", "skipped", "cancelled", "preparation_failed"}:
        payload["status"] = "failed"
    return payload


async def _finish(
    project_id: int,
    validation_id: int,
    status: str,
    *,
    checks: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        return (await storage_call(storage.finish_project_library_validation,
            project_id,
            validation_id,
            status,
            checks=checks,
            diagnostics=diagnostics,
        ))
    except Exception:
        logger.exception("Unable to finish script library validation %s/%s", project_id, validation_id)
        return (await storage_call(storage.get_project_library_validation, project_id, validation_id))


async def run_project_library_validation(
    project_id: int,
    validation_id: int,
    runner: Any,
) -> dict[str, Any] | None:
    """Execute one queued validation and always settle its persisted state."""

    claimed = (await storage_call(storage.claim_project_library_validation, project_id, validation_id,
        _on_cancelled_result=lambda row: storage.finish_project_library_validation(
            project_id, validation_id, 'cancelled', checks=[], diagnostics=[])
            if row and row.get('status') == 'running' else None))
    if claimed is None:
        return None
    if str(claimed.get("status") or "") != "running":
        return claimed

    checks_result: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    try:
        snapshot = (await storage_call(storage.get_project_library_validation_snapshot, project_id, validation_id))
        if not snapshot:
            return await _finish(
                project_id,
                validation_id,
                "failed",
                checks=checks_result,
                diagnostics=[_diagnostic("validation_missing", "验证快照不存在")],
            )
        validation = snapshot["validation"]
        modules = snapshot["modules"]
        checks = snapshot["checks"]
        bundle = snapshot["bundle"]
        include_disabled = bool(validation.get("include_disabled"))
        static_diagnostics = (await storage_call(storage.library_static_reference_diagnostics,
            project_id,
            modules,
            checks=checks,
        ))
        if static_diagnostics:
            return await _finish(
                project_id,
                validation_id,
                "failed",
                checks=checks_result,
                diagnostics=static_diagnostics,
            )

        changed_modules = {
            str(item)
            for item in snapshot.get("changed_modules", [])
            if str(item)
        }
        run_method = getattr(runner, "run_library_validation", None)
        if not callable(run_method):
            return await _finish(
                project_id,
                validation_id,
                "failed",
                checks=checks_result,
                diagnostics=[_diagnostic("runner_capability_missing", "执行器不支持公共方法验证")],
            )

        for check in checks:
            check_id = int(check.get("id") or 0)
            module_names = set(referenced_module_names(check))
            if not module_names or not (module_names & changed_modules):
                continue
            if not bool(check.get("enabled")) and not include_disabled:
                continue
            if (await storage_call(storage.is_project_library_validation_cancel_requested, project_id, validation_id)):
                diagnostics.append(_diagnostic("validation_cancelled", "验证已取消"))
                return await _finish(
                    project_id,
                    validation_id,
                    "cancelled",
                    checks=checks_result,
                    diagnostics=diagnostics,
                )
            try:
                result = await run_method(check, bundle, trigger="library-validation")
                result = _with_result_identity(result, check)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = {
                    "check_id": check_id,
                    "original_check_id": check_id,
                    "status": "failed",
                    "runs": [],
                }
                diagnostics.append(_diagnostic("validation_execution_error", str(exc) or exc.__class__.__name__, check_id=check_id))
            checks_result.append(result)
            if not bool(check.get("enabled")) and _run_status(result) != "ok":
                # Disabled checks are explicitly requested for diagnostics but
                # never gate publication.
                diagnostics.append(
                    {
                        "level": "warning",
                        "code": "disabled_check_failed",
                        "message": "停用任务验证未通过；停用任务结果不阻止发布",
                        "check_id": check_id,
                    }
                )
            if (await storage_call(storage.is_project_library_validation_cancel_requested, project_id, validation_id)):
                diagnostics.append(_diagnostic("validation_cancelled", "验证已取消"))
                return await _finish(
                    project_id,
                    validation_id,
                    "cancelled",
                    checks=checks_result,
                    diagnostics=diagnostics,
                )

        enabled_checks = [
            item
            for item in checks
            if bool(item.get("enabled"))
            and set(referenced_module_names(item)) & changed_modules
        ]
        result_by_id = {
            int(item.get("check_id") or 0): item
            for item in checks_result
            if isinstance(item, dict)
        }
        success = all(_run_status(result_by_id.get(int(check["id"]))) == "ok" for check in enabled_checks)
        return await _finish(
            project_id,
            validation_id,
            "succeeded" if success else "failed",
            checks=checks_result,
            diagnostics=diagnostics,
        )
    except asyncio.CancelledError:
        diagnostics.append(_diagnostic("validation_cancelled", "验证被服务中断，已取消"))
        await _finish(
            project_id,
            validation_id,
            "cancelled",
            checks=checks_result,
            diagnostics=diagnostics,
        )
        raise
    except (ModuleLibraryError, ValueError, TypeError) as exc:
        diagnostics.append(_diagnostic("validation_input_error", str(exc) or exc.__class__.__name__))
        return await _finish(
            project_id,
            validation_id,
            "failed",
            checks=checks_result,
            diagnostics=diagnostics,
        )
    except Exception as exc:
        logger.exception("Script library validation %s/%s failed", project_id, validation_id)
        diagnostics.append(_diagnostic("validation_internal_error", str(exc) or exc.__class__.__name__))
        return await _finish(
            project_id,
            validation_id,
            "failed",
            checks=checks_result,
            diagnostics=diagnostics,
        )


__all__ = ["run_project_library_validation"]
