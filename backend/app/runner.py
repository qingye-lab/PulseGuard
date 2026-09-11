from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, AsyncIterator

import httpx

from .api_assertions import api_inspection_diagnostics, has_enabled_api_assertions, run_structured_api_check
from .browser_installation import browser_capabilities, ensure_browser_types_installed
from .browser_types import (
    DEFAULT_BROWSER_TYPE,
    browser_pool_sizes,
    enabled_browser_types,
    normalize_browser_selection_mode,
    normalize_browser_types,
    settings_for_browser_type,
)
from .ui_assertions import has_enabled_ui_assertions, run_structured_ui_check
from . import notifier, storage
from .artifacts import ArtifactStore
from .context import RunContext, RunFailure, RunnerEnvironmentFailure, PreparationFailure, compile_check_function
from .monitoring import run_metadata
from .resource_pool import ProbeResourcePool
from .script_modules import (
    ModuleBundleError,
    analyze_script,
    module_metadata,
    normalize_module_bundle,
)
from .variables import mask_data, mask_text

_TASK_DURATION_ATTR = "_pulseguard_task_duration_ms"
API_SCRIPT_ASSERTIONS_CAPABILITY = "api_script_assertions_v1"
SCRIPT_MODULES_CAPABILITY = "script_modules_v1"
SYSTEM_ERROR_PATTERNS = (
    "Target crashed",
    "ERR_INSUFFICIENT_RESOURCES",
    "Host system is missing dependencies",
    "BrowserType.launch",
    "Executable doesn't exist",
    "browser executable",
)


class MaintenanceBusyError(RuntimeError):
    pass


class RuntimeSettingsReloadError(RuntimeError):
    """The database switched successfully, but execution must remain paused."""


class ModulePreflightError(ValueError):
    """A module preflight error that retains the bundle used for the check."""

    def __init__(self, message: str, *, bundle: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.bundle = bundle


@dataclass
class RunJob:
    check: dict[str, Any]
    trigger: str
    run_id: int
    record_status: bool
    notify: bool
    future: asyncio.Future[dict[str, Any]]
    runner_metadata: dict[str, Any] | None = None
    started: bool = False
    manage_active: bool = True
    manage_capacity: bool = True


@dataclass(frozen=True)
class BrowserRunTarget:
    runner: dict[str, Any]
    browser_type: str
    skip_reason: str = ""


class AsyncCapacityLimiter:
    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, int(capacity))
        self._in_use = 0
        self._condition = asyncio.Condition()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            await self.release()

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_use < self._capacity)
            self._in_use += 1

    async def release(self) -> None:
        async with self._condition:
            self._in_use = max(0, self._in_use - 1)
            self._condition.notify_all()

    async def resize(self, capacity: int) -> None:
        async with self._condition:
            self._capacity = max(1, int(capacity))
            self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        return {"limit": self._capacity, "in_use": self._in_use, "available": max(0, self._capacity - self._in_use)}


class CheckRunner:
    def __init__(self) -> None:
        self.artifacts = ArtifactStore()
        self._active_checks: set[int] = set()
        self._lock = asyncio.Lock()
        self._queue_limit = self._max_queue_size()
        self._queued_jobs = 0
        self._running_jobs = 0
        self._worker_jobs = 0
        self._jobs: set[asyncio.Task[None]] = set()
        self._closing = False
        self._maintenance_active = False
        self._suppress_shutdown_records = False
        self._global_limiter = AsyncCapacityLimiter(self._max_concurrency())
        self._ui_limiter = AsyncCapacityLimiter(self._max_ui_concurrency())
        self.resources = ProbeResourcePool(self._api_pool_size(), self._browser_pool_sizes())

    async def start(self) -> None:
        self._closing = False
        settings = storage.get_settings()
        await self.resources.start(
            settings,
            api_pool_size=self._api_pool_size(settings),
            browser_pool_sizes_value=self._browser_pool_sizes(settings),
        )
        self._refresh_local_runner_browser_capabilities(settings)

    async def shutdown(self, record_cancelled: bool = True) -> None:
        previous_suppression = self._suppress_shutdown_records
        self._suppress_shutdown_records = not record_cancelled
        async with self._lock:
            self._closing = True
            jobs = [job for job in self._jobs if not job.done()]
        try:
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            await self.resources.shutdown()
        finally:
            self._suppress_shutdown_records = previous_suppression

    async def reload_settings(self) -> None:
        settings = storage.get_settings()
        self._queue_limit = self._max_queue_size(settings)
        await self._global_limiter.resize(self._max_concurrency(settings))
        await self._ui_limiter.resize(self._max_ui_concurrency(settings))
        await self.resources.reload(
            settings,
            api_pool_size=self._api_pool_size(settings),
            browser_pool_sizes_value=self._browser_pool_sizes(settings),
        )
        self._refresh_local_runner_browser_capabilities(settings)

    async def run_check(
        self, check_id: int, trigger: str = "scheduled", project_id: int | None = None
    ) -> dict[str, Any]:
        check = storage.get_check(check_id, project_id=project_id)
        if not check:
            raise ValueError("任务不存在")
        project = storage.get_project(int(check["project_id"]))
        if not project or project.get("status") != "active":
            raise ValueError("归档项目不能执行任务")
        try:
            check = self._attach_frozen_module_bundle(check, require_published=True)
        except ValueError as exc:
            # Module resolution is a run precondition.  Keep the attempt
            # observable through the normal failure path so scheduled runs do
            # not disappear and manual callers do not turn a script error into
            # a misleading 404 response.
            bundle = getattr(exc, "bundle", None)
            if isinstance(bundle, dict):
                check = {**check, "_module_bundle": bundle}
            return await self._run_module_preflight_failure(check, trigger, exc)
        if storage.is_deployment_window_active() and trigger != "post-deploy":
            return self._deployment_paused_result(check, trigger)
        runners = self._resolve_check_runners(check)
        targets = self._resolve_browser_targets(check, runners)
        if self._uses_browser(check):
            if len(targets) == 1 and targets[0].runner.get("runner_id") == storage.LOCAL_RUNNER_ID and not targets[0].skip_reason:
                target = targets[0]
                check_payload = self._with_browser_type(check, target.browser_type)
                return await self._submit(
                    check_payload,
                    trigger,
                    runner_metadata=storage.runner_metadata(target.runner, browser_type=target.browser_type),
                )
            return await self._run_distributed_check(check, targets, trigger)
        if (
            len(runners) == 1
            and runners[0].get("runner_id") == storage.LOCAL_RUNNER_ID
            and runners[0].get("enabled")
            and runners[0].get("available")
        ):
            return await self._submit(check, trigger, runner_metadata=storage.runner_metadata(runners[0]))
        targets = [BrowserRunTarget(runner=runner, browser_type="") for runner in runners]
        return await self._run_distributed_check(check, targets, trigger)

    async def run_draft(self, check: dict[str, Any], trigger: str = "draft") -> dict[str, Any]:
        draft = dict(check)
        original_check_id = int(draft.get("original_check_id") or draft.get("id") or 0)
        if original_check_id > 0:
            draft["original_check_id"] = original_check_id
            draft["_original_check_id"] = original_check_id
        draft["id"] = 0
        if not draft.get("project_id"):
            raise ValueError("草稿调试缺少项目归属")
        try:
            draft = self._attach_frozen_module_bundle(draft, require_published=False)
        except ValueError as exc:
            bundle = getattr(exc, "bundle", None)
            if isinstance(bundle, dict):
                draft = {**draft, "_module_bundle": bundle}
            draft = self._with_module_preflight_failure(draft, exc)
        settings = storage.get_settings()
        local = storage.get_probe_runner(storage.LOCAL_RUNNER_ID) or self._local_runner_from_settings(settings)
        browser_type = self._first_runnable_browser_type(draft, local, settings) if self._uses_browser(draft) else ""
        if browser_type:
            draft = self._with_browser_type(draft, browser_type)
        return await self._submit(
            draft,
            trigger,
            record_status=False,
            notify=False,
            runner_metadata=storage.runner_metadata(local, browser_type=browser_type),
            manage_active=False,
        )

    async def _run_module_preflight_failure(
        self, check: dict[str, Any], trigger: str, error: ValueError
    ) -> dict[str, Any]:
        failed_check = self._with_module_preflight_failure(check, error)
        return await self._submit(failed_check, trigger)

    @classmethod
    def _with_module_preflight_failure(
        cls, check: dict[str, Any], error: ValueError
    ) -> dict[str, Any]:
        message = str(error).strip() or error.__class__.__name__
        result = dict(check)
        module_bundle = result.get("_module_bundle")
        if module_bundle is None:
            module_bundle = result.get("module_bundle")
        result["_module_preflight_error"] = message
        result["_module_preflight_stack"] = traceback.format_exc()
        diagnostics: list[dict[str, Any]] = []
        for entry, raw in (("check", result.get("script")), ("setup", result.get("setup_script"))):
            source = str(raw or "")
            if not source.strip():
                continue
            analysis = analyze_script(source, entry_points=(entry,), module_bundle=module_bundle)
            diagnostics.extend(item for item in analysis.get("diagnostics") or [] if item.get("level") == "error")
        if not diagnostics:
            diagnostics.append(
                {
                    "level": "error",
                    "code": "module_preflight",
                    "message": message,
                    "filename": f"<pulseguard:{'setup' if not str(result.get('script') or '').strip() else 'check'}>",
                    "line": 0,
                    "column": 1,
                }
            )
        else:
            # Keep the precise bundle/reference failure as the first report;
            # the source analysis below may only describe the syntax shape.
            diagnostics.insert(
                0,
                {
                    "level": "error",
                    "code": "module_preflight",
                    "message": message,
                    "filename": diagnostics[0].get("filename") or "<pulseguard:check>",
                    "line": diagnostics[0].get("line") or 0,
                    "column": diagnostics[0].get("column") or 1,
                },
            )
        result["_module_preflight_diagnostics"] = diagnostics
        return result

    async def run_library_validation(
        self,
        check: dict[str, Any],
        bundle: dict[str, Any],
        *,
        trigger: str = "library-validation",
    ) -> dict[str, Any]:
        """Run a candidate/published module bundle through real node dispatch.

        Validation runs intentionally use the same target resolution, browser
        pools, remote worker transport, retries, and last-response API path as a
        normal check.  They are marked as non-observations so no health state,
        trend rollup, or business/system notification is produced.
        """
        validation_check = dict(check)
        original_check_id = int(
            validation_check.get("original_check_id")
            or validation_check.get("id")
            or validation_check.get("check_id")
            or 0
        )
        project_id = int(validation_check.get("project_id") or 0)
        if project_id <= 0:
            raise ValueError("公共方法验证缺少项目归属")
        if original_check_id <= 0:
            raise ValueError("公共方法验证缺少原始任务引用")
        try:
            normalized_bundle = normalize_module_bundle(bundle, expected_project_id=project_id)
        except ModuleBundleError as exc:
            raise ValueError(str(exc)) from exc
        validation_check["_module_bundle"] = normalized_bundle
        validation_check["_library_validation"] = True
        validation_check["original_check_id"] = original_check_id
        validation_check["_original_check_id"] = original_check_id
        if "id" not in validation_check:
            validation_check["id"] = original_check_id
        self._validate_module_references(validation_check, normalized_bundle)

        runners = self._resolve_check_runners(validation_check)
        targets = self._resolve_browser_targets(validation_check, runners)
        if self._uses_browser(validation_check):
            if (
                len(targets) == 1
                and targets[0].runner.get("runner_id") == storage.LOCAL_RUNNER_ID
                and not targets[0].skip_reason
            ):
                target = targets[0]
                validation_check = self._with_browser_type(validation_check, target.browser_type)
                run = await self._submit(
                    validation_check,
                    trigger,
                    record_status=False,
                    notify=False,
                    runner_metadata=storage.runner_metadata(target.runner, browser_type=target.browser_type),
                    manage_active=False,
                )
                runs = [run]
            else:
                aggregate = await self._run_distributed_check(validation_check, targets, trigger)
                runs = list(aggregate.get("runs") or []) if isinstance(aggregate, dict) else []
                if not runs and isinstance(aggregate, dict) and aggregate.get("id"):
                    runs = [aggregate]
        elif (
            len(runners) == 1
            and runners[0].get("runner_id") == storage.LOCAL_RUNNER_ID
            and runners[0].get("enabled")
            and runners[0].get("available")
        ):
            run = await self._submit(
                validation_check,
                trigger,
                record_status=False,
                notify=False,
                runner_metadata=storage.runner_metadata(runners[0]),
                manage_active=False,
            )
            runs = [run]
        else:
            aggregate = await self._run_distributed_check(validation_check, targets, trigger)
            runs = list(aggregate.get("runs") or []) if isinstance(aggregate, dict) else []
            if not runs and isinstance(aggregate, dict) and aggregate.get("id"):
                runs = [aggregate]

        statuses = [str(run.get("status") or "failed") for run in runs]
        non_success = [item for item in statuses if item != "ok"]
        status = "ok" if runs and not non_success else (non_success[0] if non_success else "skipped")
        return {
            "check_id": original_check_id,
            "original_check_id": original_check_id,
            "status": status,
            "runs": runs,
            "bundle": module_metadata(normalized_bundle),
        }

    async def execute_worker_run(
        self,
        check: dict[str, Any],
        trigger: str,
        run_id: int,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._lock:
            if self._maintenance_active:
                return self._deployment_paused_result(check, trigger, run_id=run_id)
            self._worker_jobs += 1
        try:
            return await self._execute_worker_run(check, trigger, run_id, settings)
        finally:
            async with self._lock:
                self._worker_jobs -= 1

    async def _execute_worker_run(
        self,
        check: dict[str, Any],
        trigger: str,
        run_id: int,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        await self.resources.reload(
            settings,
            api_pool_size=self._api_pool_size(settings),
            browser_pool_sizes_value=self._browser_pool_sizes(settings),
        )
        if self._uses_browser(check):
            async with self._ui_limiter.slot():
                async with self._global_limiter.slot():
                    return await self._execute_core(check, trigger, run_id, settings)
        async with self._global_limiter.slot():
            return await self._execute_core(check, trigger, run_id, settings)

    async def inspect_ui(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .ui_assertions import inspect_ui_page

        return await self._inspect_ui(payload, inspect_ui_page, "页面扫描")

    async def inspect_ui_rules(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .ui_assertions import inspect_ui_rule_selectors

        return await self._inspect_ui(payload, inspect_ui_rule_selectors, "规则检测")

    async def _inspect_ui(self, payload: dict[str, Any], inspect_func: Any, name: str) -> dict[str, Any]:
        from .ui_diagnostics import UiInspectionFailure, inspection_diagnostics

        payload = self._attach_frozen_module_bundle(payload, require_published=True)
        settings = storage.settings_for_project(storage.get_settings(), payload.get("project_id"))
        ctx = RunContext(
            {
                "id": 0,
                "project_id": payload.get("project_id"),
                "name": name,
                "type": "ui",
                "entry_url": payload.get("entry_url") or "",
                "timeout_ms": payload.get("timeout_ms") or settings.get("default_ui_timeout_ms", 15000),
                "viewport_mode": payload.get("viewport_mode") or "web",
                "setup_script": payload.get("setup_script") or "",
                "method": "",
                "headers_json": "{}",
                "body": "",
                "viewport_width": payload.get("viewport_width"),
                "viewport_height": payload.get("viewport_height"),
                "_module_bundle": payload.get("_module_bundle"),
            },
            0,
            settings,
            self.artifacts,
            resources=self.resources,
            attempt=1,
        )
        setup_func = None
        failure: UiInspectionFailure | None = None
        result: dict[str, Any] | None = None
        try:
            ctx.ui_stage = "compile"
            setup_script = str(payload.get("setup_script") or "")
            if setup_script.strip():
                setup_func = self._load_check_function(
                    setup_script,
                    ctx,
                    function_name="setup",
                    expected_signature="async def setup(ctx, page)",
                    script_label="前置脚本",
                    attempt=ctx.attempt,
                )
            ctx.ui_stage = "queue"
            async def inspect_with_slot() -> dict[str, Any]:
                async with self._ui_limiter.slot():
                    return await inspect_func(payload, settings, ctx=ctx, setup_func=setup_func)

            result = await asyncio.wait_for(
                inspect_with_slot(),
                timeout=int(settings.get("max_task_runtime_seconds", 60)),
            )
            result["diagnostics"] = inspection_diagnostics(ctx)
        except Exception as exc:
            diagnostics = inspection_diagnostics(ctx, error=exc)
            failure = UiInspectionFailure(diagnostics["message"], diagnostics)
        finally:
            try:
                await ctx.close(False)
            except Exception as exc:
                # Preserve cancellation and the primary failure; cleanup is secondary.
                if failure is not None:
                    failure.diagnostics["cleanup_error"] = mask_text(str(exc) or type(exc).__name__, ctx.settings)[:8000]
                elif result is not None:
                    ctx.ui_stage = "cleanup"
                    diagnostics = inspection_diagnostics(ctx, error=exc)
                    failure = UiInspectionFailure(diagnostics["message"], diagnostics)
        if failure is not None:
            raise failure from None
        assert result is not None
        return result

    def runtime_status(self) -> dict[str, Any]:
        global_slots = self._global_limiter.snapshot()
        ui_slots = self._ui_limiter.snapshot()
        return {
            "queue": {
                "queued": self._queued_jobs,
                "limit": self._queue_limit,
                "available": max(0, self._queue_limit - self._queued_jobs),
            },
            "workers": {
                "running": self._running_jobs,
                "limit": global_slots["limit"],
                "available": global_slots["available"],
            },
            "browser": {
                "running": ui_slots["in_use"],
                "limit": ui_slots["limit"],
                "available": ui_slots["available"],
            },
            "pools": self.resources.snapshot(),
            "active_checks": len(self._active_checks),
            "closing": self._closing,
            "maintenance_active": self._maintenance_active,
        }

    async def wait_for_idle(self, timeout_seconds: float = 30) -> dict[str, Any]:
        deadline = perf_counter() + max(0, float(timeout_seconds))
        while True:
            async with self._lock:
                queued = self._queued_jobs
                running = self._running_jobs
                active_checks = len(self._active_checks)
                self._jobs = {job for job in self._jobs if not job.done()}
                in_flight_jobs = len(self._jobs)
                worker_jobs = self._worker_jobs
            # A cancelled distributed request can release group counters while
            # its shielded local children are still finishing persisted runs.
            idle = queued == 0 and running == 0 and active_checks == 0 and in_flight_jobs == 0 and worker_jobs == 0
            if idle or perf_counter() >= deadline:
                return {
                    "idle": idle,
                    "queued": queued,
                    "running": running,
                    "active_checks": active_checks,
                    "in_flight_jobs": in_flight_jobs,
                    "worker_jobs": worker_jobs,
                }
            await asyncio.sleep(0.2)

    @asynccontextmanager
    async def maintenance_window(self) -> AsyncIterator[None]:
        """Stop new admissions while previously accepted work drains."""
        async with self._lock:
            if self._maintenance_active:
                raise MaintenanceBusyError("数据库维护正在进行，请稍后重试")
            self._maintenance_active = True
        release_admissions = True
        try:
            yield
        except (storage.DatabaseRestoreRecoveryError, RuntimeSettingsReloadError):
            # Recovery or runtime reinitialization failed. Keep execution closed
            # until an operator follows the recovery error and restarts safely.
            release_admissions = False
            raise
        finally:
            if release_admissions:
                async with self._lock:
                    self._maintenance_active = False

    async def _submit(
        self,
        check: dict[str, Any],
        trigger: str,
        record_status: bool = True,
        notify: bool = True,
        runner_metadata: dict[str, Any] | None = None,
        manage_active: bool = True,
        manage_capacity: bool = True,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        check_id = int(check.get("id") or 0)
        async with self._lock:
            self._jobs = {job for job in self._jobs if not job.done()}
            # Internal children belong to an already counted distributed group.
            if self._maintenance_active and manage_capacity:
                return self._deployment_paused_result(check, trigger)
            if self._closing:
                return self._create_skipped_run(check, "执行器正在关闭，本次已跳过", trigger)
            if manage_active and check_id > 0 and check_id in self._active_checks:
                return self._create_skipped_run(check, "同一任务上一次执行尚未结束或仍在排队，本次已跳过", trigger)
            if manage_capacity and self._queued_jobs >= self._queue_limit:
                return self._create_skipped_run(check, "执行队列已满，本次已跳过", trigger)

            settings = storage.get_settings()
            metadata = run_metadata(trigger)
            if check.get("_library_validation"):
                metadata.update({"trigger": trigger, "observation_kind": "library_validation", "affects_health": False})
            run_group_id = str(check.get("_run_group_id") or metadata.get("run_group_id") or "")
            run_metadata_payload = {**metadata, **({"run_group_id": run_group_id} if run_group_id else {})}
            runner_payload = runner_metadata or self._runner_metadata(settings, failure_kind="none")
            run = storage.create_run(
                self._with_runner_metadata(
                    check,
                    settings,
                    runner_payload,
                    trigger=trigger,
                    run_metadata_payload=run_metadata_payload,
                ),
                "pending",
            )
            job = RunJob(
                check=check,
                trigger=trigger,
                run_id=int(run["id"]),
                record_status=record_status and bool(metadata["affects_health"]) and not bool(check.get("_library_validation")),
                notify=notify and bool(metadata["affects_health"]) and not bool(check.get("_library_validation")),
                future=loop.create_future(),
                runner_metadata=runner_payload,
                manage_active=manage_active,
                manage_capacity=manage_capacity,
            )
            if manage_capacity:
                self._queued_jobs += 1
            if manage_active and check_id > 0:
                self._active_checks.add(check_id)
            task = loop.create_task(self._run_job(job))
            task.add_done_callback(self._jobs.discard)
            self._jobs.add(task)

        return await asyncio.shield(job.future)

    async def _run_job(self, job: RunJob) -> None:
        result: dict[str, Any] | None = None
        try:
            result = await self._run_job_body(job)
        except asyncio.TimeoutError:
            try:
                result = self._finish_watchdog_timeout(job)
            except Exception as finish_exc:
                result = self._ephemeral_job_error(
                    job,
                    "执行总时长超时后的状态写入失败",
                    finish_exc,
                    status="timeout",
                )
            if not job.check.get("_library_validation"):
                result = await self._notify_system_result(
                    job.check,
                    job.trigger,
                    job.run_id,
                    result,
                    self._settings_for_system_alert(),
                    source="watchdog",
                )
        except asyncio.CancelledError:
            result = self._finish_cancelled_job(job)
        except Exception as exc:
            result = self._finish_internal_error(job, exc)
            if not job.check.get("_library_validation"):
                result = await self._notify_system_result(
                    job.check,
                    job.trigger,
                    job.run_id,
                    result,
                    self._settings_for_system_alert(),
                    source="runner",
                )
        finally:
            await self._complete_job(job, result)

    async def _run_job_body(self, job: RunJob) -> dict[str, Any]:
        if not job.manage_capacity:
            storage.start_run(job.run_id)
            return await self._execute_started_job(job)
        if self._uses_browser(job.check):
            async with self._ui_limiter.slot():
                async with self._global_limiter.slot():
                    return await self._start_and_execute(job)
        async with self._global_limiter.slot():
            return await self._start_and_execute(job)

    async def _start_and_execute(self, job: RunJob) -> dict[str, Any]:
        async with self._lock:
            job.started = True
            self._queued_jobs = max(0, self._queued_jobs - 1)
            self._running_jobs += 1
        storage.start_run(job.run_id)
        return await self._execute_started_job(job)

    async def _execute_started_job(self, job: RunJob) -> dict[str, Any]:
        return await asyncio.wait_for(
            self._execute(
                job.check,
                job.trigger,
                job.run_id,
                job.record_status,
                job.notify,
                job.runner_metadata,
            ),
            timeout=self._job_timeout_seconds(job.check, storage.get_settings()),
        )

    async def _complete_job(self, job: RunJob, result: dict[str, Any] | None) -> None:
        check_id = int(job.check.get("id") or 0)
        async with self._lock:
            if job.manage_capacity:
                if job.started:
                    self._running_jobs = max(0, self._running_jobs - 1)
                else:
                    self._queued_jobs = max(0, self._queued_jobs - 1)
            if job.manage_active and check_id > 0:
                self._active_checks.discard(check_id)
        if result is None:
            try:
                result = storage.get_run(job.run_id) or {}
            except Exception as exc:
                result = self._ephemeral_job_error(job, "执行器完成状态读取失败", exc)
        if not job.future.done():
            job.future.set_result(result)

    async def _execute(
        self,
        check: dict[str, Any],
        trigger: str,
        run_id: int,
        record_status: bool = True,
        notify: bool = True,
        runner_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = storage.settings_for_project(storage.get_settings(), check.get("project_id"))
        library_validation = bool(check.get("_library_validation"))
        result_data = await self._execute_core(check, trigger, run_id, settings)
        if runner_metadata:
            result_data.update(
                {
                    "runner_id": runner_metadata.get("runner_id"),
                    "runner_name": runner_metadata.get("runner_name"),
                    "runner_address": runner_metadata.get("runner_address"),
                    "runner_region": runner_metadata.get("runner_region"),
                    "runner_browser_version": result_data.get("runner_browser_version") or runner_metadata.get("runner_browser_version"),
                    "browser_type": result_data.get("browser_type") or runner_metadata.get("browser_type"),
                }
            )
        if self._is_runner_system_result(result_data):
            result = self._finish_runner_result(check, trigger, run_id, result_data)
            if notify and not library_validation:
                result = await self._notify_system_result(check, trigger, run_id, result, settings, source="runner")
            return result
        if library_validation:
            result_data = {**result_data, "affects_health": False}
        finished_run = storage.finish_run(run_id, result_data)

        if finished_run is None:
            raise RuntimeError("运行记录更新失败")

        if library_validation or not record_status or not finished_run.get("affects_health", True):
            storage.update_run_notification(run_id, "not_required", channel=None, error=None, sent_at=None)
            return storage.get_run(run_id) or finished_run

        transition = storage.update_check_status(int(check["id"]), finished_run)
        transition["trigger"] = trigger
        if notify and not library_validation:
            await notifier.maybe_notify(check, finished_run, transition)
        return storage.get_run(run_id) or finished_run

    async def _execute_core(
        self,
        check: dict[str, Any],
        trigger: str,
        run_id: int,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        ctx: RunContext | None = None
        duration_ms = 0
        status = "ok"
        error_message: str | None = None
        error_stack: str | None = None
        failure_kind = "none"
        log_lines: list[str] = []
        preflight_error = str(check.get("_module_preflight_error") or "").strip()
        if preflight_error:
            preflight_diagnostics = check.get("_module_preflight_diagnostics")
            if not isinstance(preflight_diagnostics, list):
                preflight_diagnostics = [
                    {
                        "level": "error",
                        "code": "module_preflight",
                        "message": preflight_error,
                        "filename": "<pulseguard:check>",
                        "line": 0,
                        "column": 1,
                    }
                ]
            request_snapshot = {
                "type": str(check.get("type") or ""),
                "url": str(check.get("entry_url") or ""),
                "method": str(check.get("method") or "GET").upper(),
                "timeout_ms": int(check.get("timeout_ms") or 0),
                "diagnostics": {
                    "modules": (
                        module_metadata(
                            check.get("_module_bundle")
                            if check.get("_module_bundle") is not None
                            else check.get("module_bundle")
                        )
                        if check.get("_module_bundle") is not None or check.get("module_bundle") is not None
                        else None
                    ),
                    "compile": preflight_diagnostics,
                },
            }
            settings_for_failure = settings
            return {
                "status": "preparation_failed",
                "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "duration_ms": 0,
                "error_message": mask_text(preflight_error, settings_for_failure),
                "error_stack": mask_text(str(check.get("_module_preflight_stack") or ""), settings_for_failure) or None,
                "logs": mask_text(preflight_error, settings_for_failure),
                "screenshot_path": None,
                "trace_path": None,
                "response_path": None,
                "request_snapshot": json.dumps(mask_data(request_snapshot, settings_for_failure), ensure_ascii=False),
                "response_snapshot": None,
                "affects_health": False,
                **self._runner_metadata(settings_for_failure, failure_kind="preparation"),
            }
        max_attempts = self._max_attempts(check, settings)

        for attempt in range(1, max_attempts + 1):
            ctx = None
            retrying = False
            try:
                ctx = RunContext(
                    check,
                    run_id,
                    settings,
                    self.artifacts,
                    resources=self.resources,
                    module_bundle=check.get("_module_bundle") or check.get("module_bundle"),
                    attempt=attempt,
                )
                ctx.log(f"触发方式：{trigger}")
                ctx.log(f"任务入口：{check.get('entry_url')}")
                if max_attempts > 1:
                    ctx.log(f"执行尝试：{attempt}/{max_attempts}")
                duration_ms = await self._run_check_attempt(ctx, check, settings)
                status = "ok"
                error_message = None
                error_stack = None
                failure_kind = "none"
            except asyncio.CancelledError:
                raise
            except PreparationFailure as exc:
                duration_ms = 0
                status = "preparation_failed"
                error_message = str(exc)
                error_stack = traceback.format_exc()
                failure_kind = "preparation"
            except asyncio.TimeoutError as exc:
                duration_ms = self._task_duration_from_exception(exc)
                status = "timeout"
                error_message = str(exc).strip() or f"执行超过超时限制：{check.get('timeout_ms')}ms"
                error_stack = traceback.format_exc()
                failure_kind = "target"
            except RunnerEnvironmentFailure as exc:
                duration_ms = self._task_duration_from_exception(exc)
                status = "failed"
                error_message = str(exc)
                error_stack = traceback.format_exc()
                failure_kind = "runner"
            except RunFailure as exc:
                duration_ms = self._task_duration_from_exception(exc)
                status = "failed"
                error_message = str(exc)
                error_stack = traceback.format_exc()
                failure_kind = "target"
            except AssertionError as exc:
                duration_ms = self._task_duration_from_exception(exc)
                status = "failed"
                error_message = str(exc) or "断言失败"
                error_stack = traceback.format_exc()
                failure_kind = "target"
            except Exception as exc:
                duration_ms = self._task_duration_from_exception(exc)
                status = "failed"
                error_message = str(exc) or exc.__class__.__name__
                error_stack = traceback.format_exc()
                failure_kind = "runner" if self._is_system_exception(exc) else "target"
            finally:
                failed = status in {"failed", "timeout"}
                retrying = failed and failure_kind == "target" and attempt < max_attempts
                if ctx is not None:
                    if retrying and error_message:
                        ctx.log(f"本次尝试失败，立即重试：{error_message}")
                    try:
                        await ctx.close(failed and not retrying and failure_kind == "target")
                    except Exception as exc:
                        if status == "ok":
                            status = "failed"
                            error_message = f"运行清理失败：{exc}"
                            error_stack = traceback.format_exc()
                            failure_kind = "runner"
                            retrying = False
                        else:
                            ctx.log(f"运行清理失败：{exc}")
                    log_lines.extend(mask_text(line, ctx.settings) for line in ctx.logs)
                    if error_message is not None:
                        error_message = mask_text(error_message, ctx.settings)
                    if error_stack is not None:
                        error_stack = mask_text(error_stack, ctx.settings)
                    ctx.set_task_duration(duration_ms)

            if retrying:
                continue
            break

        finished = datetime.now().astimezone()
        safe_error_message = mask_text(error_message, settings) if error_message is not None else None
        safe_error_stack = mask_text(error_stack, settings) if error_stack is not None else None
        safe_logs = mask_text("\n".join(log_lines), settings)
        if ctx is not None and check.get("type") == "ui":
            from .ui_diagnostics import inspection_diagnostics

            diagnostics = inspection_diagnostics(ctx, error=RunFailure(error_message) if error_message else None)
            ctx.request_snapshot = {**(ctx.request_snapshot or {}), "diagnostics": diagnostics}
        elif ctx is not None and check.get("type") == "api":
            diagnostics = api_inspection_diagnostics(ctx, error=RunFailure(error_message) if error_message else None)
            request_snapshot = ctx.request_snapshot or {
                "type": "api",
                "url": check.get("entry_url") or "",
                "method": str(check.get("method") or "GET").upper(),
                "timeout_ms": ctx.timeout_ms,
            }
            ctx.request_snapshot = {**request_snapshot, "diagnostics": diagnostics}
        safe_request_snapshot = mask_data(ctx.request_snapshot, ctx.settings) if ctx and ctx.request_snapshot else None
        safe_response_snapshot = mask_data(ctx.response_snapshot, ctx.settings) if ctx and ctx.response_snapshot else None
        runner_metadata = self._runner_metadata(settings, ctx=ctx, failure_kind=failure_kind)
        runner_metadata["browser_type"] = (
            str(check.get("_browser_type") or runner_metadata.get("browser_type") or settings.get("browser_type") or "")
            if self._uses_browser(check)
            else ""
        )
        return {
            "status": status,
            **({"affects_health": False} if failure_kind == "preparation" else {}),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_ms": duration_ms,
            "error_message": safe_error_message,
            "error_stack": safe_error_stack,
            "logs": safe_logs,
            "screenshot_path": ctx.screenshot_path if ctx else None,
            "trace_path": ctx.trace_path if ctx else None,
            "response_path": ctx.response_path if ctx else None,
            "request_snapshot": json.dumps(safe_request_snapshot, ensure_ascii=False) if safe_request_snapshot else None,
            "response_snapshot": json.dumps(safe_response_snapshot, ensure_ascii=False) if safe_response_snapshot else None,
            **runner_metadata,
        }
    async def _run_check_attempt(self, ctx: RunContext, check: dict[str, Any], settings: dict[str, Any]) -> int:
        max_runtime_seconds = int(settings.get("max_task_runtime_seconds", 60))
        timeout_seconds = min(
            int(check.get("timeout_ms") or 15000) / 1000,
            max_runtime_seconds,
        )
        uses_structured_api = self._uses_structured_api_check(check)
        uses_structured_ui = self._uses_structured_ui_check(check)
        if check.get("type") == "api":
            script = str(check.get("script") or "")
            check_func = self._load_check_function(script, ctx) if script.strip() else None

            async def run_api_flow() -> None:
                response = None
                if check_func is not None:
                    ctx.api_stage = "script"
                    await check_func(ctx)
                    response = ctx.http.last_response
                    if response is None:
                        # Resolve the configured target after the script so a
                        # runtime variable set by the script is honored while
                        # still surfacing stale or missing task parameters.
                        try:
                            _ = ctx.entry_url
                        except Exception as exc:
                            raise RunFailure(str(exc)) from exc
                if uses_structured_api:
                    ctx.api_stage = "assertions"
                    if check_func is not None and response is None:
                        raise RunFailure("脚本未产生 HTTP 响应，无法执行接口校验项")
                    if response is None:
                        await run_structured_api_check(ctx)
                    else:
                        await run_structured_api_check(ctx, response=response)
                elif check_func is None:
                    raise RunFailure("请至少添加一个校验项，或填写高级脚本")

            return await self._measure_task_duration(asyncio.wait_for(run_api_flow(), timeout=timeout_seconds))
        from .ui_assertions import prepare_ui_prerequisites, navigate_ui_target

        setup_func = None
        setup_script = str(check.get("setup_script") or "")
        if setup_script.strip():
            try:
                setup_func = self._load_check_function(
                    setup_script,
                    ctx,
                    function_name="setup",
                    expected_signature="async def setup(ctx, page)",
                    script_label="前置脚本",
                    attempt=ctx.attempt,
                )
            except Exception as exc:
                raise PreparationFailure(str(exc)) from exc
        check_func = None if uses_structured_ui else self._load_check_function(
            check.get("script") or "", ctx, attempt=ctx.attempt,
        )
        page = None
        if uses_structured_ui or setup_func is not None:
            page = await prepare_ui_prerequisites(ctx, setup_func=setup_func)

        async def run_ui_target() -> None:
            if page is not None:
                await navigate_ui_target(ctx, page)
            if uses_structured_ui:
                await run_structured_ui_check(ctx, page=page)
            else:
                ctx.ui_stage = "script"
                await check_func(ctx)

        return await self._measure_task_duration(asyncio.wait_for(
            run_ui_target(), timeout=max_runtime_seconds if uses_structured_ui else timeout_seconds,
        ))

    @staticmethod
    async def _measure_task_duration(awaitable: Any) -> int:
        started = perf_counter()
        try:
            await awaitable
        except BaseException as exc:
            setattr(exc, _TASK_DURATION_ATTR, int((perf_counter() - started) * 1000))
            raise
        return int((perf_counter() - started) * 1000)

    @staticmethod
    def _task_duration_from_exception(exc: BaseException) -> int:
        try:
            return max(0, int(getattr(exc, _TASK_DURATION_ATTR, 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _max_attempts(check: dict[str, Any], settings: dict[str, Any]) -> int:
        check_type = str(check.get("type") or "")
        if check_type == "ui":
            retries = settings.get("ui_retry_attempts", 1)
        elif check_type == "api":
            retries = settings.get("api_retry_attempts", 1)
        else:
            retries = 0
        return 1 + max(0, min(3, int(retries or 0)))

    def _create_skipped_run(self, check: dict[str, Any], message: str, trigger: str) -> dict[str, Any]:
        settings = self._settings_for_check(check, storage.settings_for_project(storage.get_settings(), check.get("project_id")))
        metadata = {**run_metadata(trigger), "affects_health": False}
        if check.get("_library_validation"):
            metadata.update({"trigger": trigger, "observation_kind": "library_validation", "affects_health": False})
        runner_payload = self._runner_metadata(settings, failure_kind="runner")
        run = storage.create_run(
            self._with_runner_metadata(check, settings, runner_payload, trigger=trigger, run_metadata_payload=metadata),
            "skipped",
            message,
        )
        storage.update_run_notification(int(run["id"]), "not_required", channel=None, error=None, sent_at=None)
        return storage.get_run(int(run["id"])) or run

    def _finish_cancelled_job(self, job: RunJob) -> dict[str, Any]:
        if self._suppress_shutdown_records:
            storage.discard_incomplete_run(job.run_id)
            return self._deployment_paused_result(job.check, job.trigger, run_id=job.run_id)
        return self._finish_without_context(job, "skipped", "执行被取消，可能是服务关闭或运行设置刷新")

    def _finish_internal_error(self, job: RunJob, exc: Exception) -> dict[str, Any]:
        message = str(exc) or exc.__class__.__name__
        return self._finish_without_context(job, "failed", f"执行器内部错误：{message}", traceback.format_exc())

    def _ephemeral_job_error(self, job: RunJob, message: str, exc: Exception, status: str = "failed") -> dict[str, Any]:
        detail = str(exc).strip() or exc.__class__.__name__
        error_message = f"{message}：{detail}"
        finished = datetime.now().astimezone().isoformat(timespec="seconds")
        payload = {
            "status": status,
            "finished_at": finished,
            "duration_ms": 0,
            "error_message": error_message,
            "error_stack": traceback.format_exc(),
            "logs": error_message,
            "screenshot_path": None,
            "trace_path": None,
            "response_path": None,
            "request_snapshot": None,
            "response_snapshot": None,
            **({**(job.runner_metadata or {}), "failure_kind": "runner"}),
            "affects_health": False,
        }
        return self._ephemeral_runner_result(job.check, job.trigger, job.run_id, payload)

    def _finish_watchdog_timeout(self, job: RunJob) -> dict[str, Any]:
        settings = storage.get_settings()
        timeout_seconds = self._job_timeout_seconds(job.check, settings)
        message = f"执行超过单任务总时长限制：{int(timeout_seconds)} 秒，已强制释放调度槽"
        safe_message = mask_text(message, settings)
        finished = datetime.now().astimezone()
        existing_run = storage.get_run(job.run_id)
        if existing_run and str(existing_run.get("status") or "") not in {"pending", "running"}:
            return existing_run
        payload = {
            "status": "timeout",
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_ms": int(timeout_seconds * 1000),
            "error_message": safe_message,
            "error_stack": None,
            "logs": safe_message,
            "screenshot_path": None,
            "trace_path": None,
            "response_path": None,
            "request_snapshot": None,
            "response_snapshot": None,
            **({**(job.runner_metadata or self._runner_metadata(settings)), "failure_kind": "runner"}),
            "affects_health": False,
        }
        return self._finish_runner_result(job.check, job.trigger, job.run_id, payload)

    def _finish_without_context(
        self,
        job: RunJob,
        status: str,
        message: str,
        error_stack: str | None = None,
    ) -> dict[str, Any]:
        settings = storage.get_settings()
        safe_message = mask_text(message, settings)
        safe_error_stack = mask_text(error_stack, settings) if error_stack is not None else None
        finished = datetime.now().astimezone()
        payload = {
            "status": status,
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_ms": 0,
            "error_message": safe_message,
            "error_stack": safe_error_stack,
            "logs": safe_message,
            "screenshot_path": None,
            "trace_path": None,
            "response_path": None,
            "request_snapshot": None,
            "response_snapshot": None,
            **({**(job.runner_metadata or self._runner_metadata(settings)), "failure_kind": "runner"}),
            "affects_health": False,
        }
        return self._finish_runner_result(job.check, job.trigger, job.run_id, payload)

    @staticmethod
    def _is_runner_system_result(data: dict[str, Any]) -> bool:
        status = str(data.get("status") or "")
        return status in {"failed", "timeout", "skipped"} and str(data.get("failure_kind") or "") == "runner"

    @staticmethod
    def _is_system_exception(exc: Exception) -> bool:
        text = f"{exc.__class__.__module__}.{exc.__class__.__name__}: {exc}"
        return any(pattern in text for pattern in SYSTEM_ERROR_PATTERNS)

    @staticmethod
    def _settings_for_system_alert() -> dict[str, Any]:
        try:
            return storage.get_settings()
        except Exception:
            return {}

    async def _notify_system_result(
        self,
        check: dict[str, Any],
        trigger: str,
        run_id: int,
        result: dict[str, Any],
        settings: dict[str, Any],
        *,
        source: str,
    ) -> dict[str, Any]:
        outcome: dict[str, Any]
        try:
            notified = await notifier.notify_system_error(
                {
                    "source": source,
                    "severity": "error",
                    "time": result.get("finished_at") or result.get("started_at") or storage.now_iso(),
                    "message": result.get("error_message") or result.get("logs") or "系统异常",
                    "check_id": int(check.get("id") or 0),
                    "check_name": str(check.get("name") or ""),
                    "run_id": run_id,
                    "trigger": trigger,
                    "runner_id": result.get("runner_id"),
                    "runner_name": result.get("runner_name"),
                    "runner_region": result.get("runner_region"),
                },
                settings=settings,
            )
            outcome = notified if isinstance(notified, dict) else {}
        except Exception as exc:
            outcome = {
                "sent": False,
                "status": "failed",
                "error": mask_text(str(exc).strip() or exc.__class__.__name__, settings),
            }
        if str(result.get("failure_kind") or "") == "runner" and not bool(result.get("affects_health")):
            status, channel, error, sent_at = self._system_notification_record(outcome)
            try:
                storage.update_run_notification(run_id, status, channel=channel, error=error, sent_at=sent_at)
            except Exception:
                pass
            try:
                return storage.get_run(run_id) or result
            except Exception:
                return result
        return result

    @staticmethod
    def _system_notification_record(outcome: dict[str, Any]) -> tuple[str, str | None, str | None, str | None]:
        reason = str(outcome.get("reason") or "")
        if reason in {"disabled", "no_channels"}:
            return "disabled", None, None, None
        if reason == "settings_unavailable":
            return "failed", None, "system notification settings unavailable", None
        status = str(outcome.get("status") or "")
        if status not in {"sent", "failed"}:
            status = "sent" if outcome.get("sent") else "failed"
        channel = str(outcome.get("channel") or "").strip() or None
        error = str(outcome.get("error") or "").strip() or None
        sent_at = storage.now_iso() if outcome.get("sent") else None
        return status, channel, error, sent_at

    @staticmethod
    def _finish_runner_result(
        check: dict[str, Any],
        trigger: str,
        run_id: int,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {**data, "failure_kind": "runner", "affects_health": False}
        try:
            existing = storage.get_run(run_id)
        except Exception:
            existing = None
        if existing and str(existing.get("status") or "") not in {"pending", "running"}:
            return existing
        try:
            finished = storage.finish_run(run_id, payload)
        except Exception:
            return CheckRunner._ephemeral_runner_result(check, trigger, run_id, payload)
        if finished is None:
            return CheckRunner._ephemeral_runner_result(check, trigger, run_id, payload)
        try:
            storage.update_run_notification(run_id, "not_required", channel=None, error=None, sent_at=None)
        except Exception:
            pass
        try:
            return storage.get_run(run_id) or finished
        except Exception:
            return finished

    @staticmethod
    def _ephemeral_runner_result(
        check: dict[str, Any],
        trigger: str,
        run_id: int,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = str(data.get("finished_at") or storage.now_iso())
        return {
            "id": run_id,
            "check_id": int(check.get("id") or 0),
            "check_name": str(check.get("name") or ""),
            "check_type": str(check.get("type") or ""),
            "started_at": timestamp,
            "created_at": timestamp,
            "notification_status": "not_required",
            "notification_channel": None,
            "notification_error": None,
            "notification_sent_at": None,
            "trigger": trigger,
            "observation_kind": "runner",
            "affects_health": False,
            "run_group_id": str(check.get("_run_group_id") or ""),
            "consecutive_failures": 0,
            **data,
        }

    @staticmethod
    def _deployment_paused_result(check: dict[str, Any], trigger: str, run_id: int = 0) -> dict[str, Any]:
        timestamp = storage.now_iso()
        return {
            "id": run_id,
            "check_id": int(check.get("id") or 0),
            "check_name": str(check.get("name") or ""),
            "check_type": str(check.get("type") or ""),
            "status": "skipped",
            "started_at": timestamp,
            "finished_at": timestamp,
            "duration_ms": 0,
            "error_message": "系统正在部署维护，任务已暂停，未记录为运行结果",
            "error_stack": None,
            "logs": "系统正在部署维护，任务已暂停，未记录为运行结果",
            "screenshot_path": None,
            "trace_path": None,
            "response_path": None,
            "request_snapshot": None,
            "response_snapshot": None,
            "notification_status": "not_required",
            "notification_channel": None,
            "notification_error": None,
            "notification_sent_at": None,
            "trigger": trigger,
            "observation_kind": "deployment",
            "affects_health": False,
            "run_group_id": "",
            "created_at": timestamp,
            "consecutive_failures": 0,
            "failure_kind": "runner",
            "runner_id": storage.LOCAL_RUNNER_ID,
            "runner_name": "",
            "runner_address": "",
            "runner_region": "",
            "runner_browser_version": "",
            "browser_type": "",
        }

    @staticmethod
    def _runner_metadata(settings: dict[str, Any], ctx: RunContext | None = None, failure_kind: str = "none") -> dict[str, str]:
        name = str(settings.get("local_runner_name") or "local").strip() or "local"
        address = str(settings.get("local_runner_address") or "127.0.0.1").strip()
        region = str(settings.get("local_runner_region") or "local").strip() or "local"
        browser_version = str(getattr(ctx, "browser_version", None) or "").strip()
        normalized_failure_kind = failure_kind if failure_kind in {"none", "target", "runner", "preparation"} else "runner"
        return {
            "runner_id": storage.LOCAL_RUNNER_ID,
            "runner_name": name,
            "runner_address": address,
            "runner_region": region,
            "runner_browser_version": browser_version,
            "browser_type": str(settings.get("browser_type") or ""),
            "failure_kind": normalized_failure_kind,
        }

    def _with_runner_metadata(
        self,
        check: dict[str, Any],
        settings: dict[str, Any],
        runner_metadata: dict[str, Any] | None = None,
        trigger: str = "manual",
        run_metadata_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            **check,
            "_runner": runner_metadata or self._runner_metadata(settings, failure_kind="none"),
            "_run": run_metadata_payload or run_metadata(trigger),
        }

    def _resolve_check_runners(self, check: dict[str, Any]) -> list[dict[str, Any]]:
        mode = str(check.get("runner_selection_mode") or "selected_parallel")
        if mode != "round_robin_all" and not check.get("runner_ids"):
            local = storage.get_probe_runner(storage.LOCAL_RUNNER_ID) or self._local_runner_from_settings(storage.get_settings())
            return [local] if storage.can_schedule_runner(local) else []
        if mode == "round_robin_all":
            candidates = storage.list_schedulable_probe_runners()
            if not candidates:
                return []
            index = storage.next_runner_cursor(f"check:{int(check.get('id') or 0)}", len(candidates))
            return [candidates[index]]
        runners = storage.list_probe_runners_by_ids(check.get("runner_ids") or [storage.LOCAL_RUNNER_ID])
        schedulable = [runner for runner in runners if storage.can_schedule_runner(runner)]
        return schedulable

    def _resolve_browser_targets(self, check: dict[str, Any], runners: list[dict[str, Any]]) -> list[BrowserRunTarget]:
        if not self._uses_browser(check):
            return [BrowserRunTarget(runner=runner, browser_type="") for runner in runners]
        if not runners:
            return []
        settings = storage.get_settings()
        targets: list[BrowserRunTarget] = []
        for runner in runners:
            targets.extend(self._resolve_browser_targets_for_runner(check, runner, settings))
        return targets or [BrowserRunTarget(runner=self._local_runner_from_settings(settings), browser_type="", skip_reason="没有可执行的 browser type")]

    def _resolve_browser_targets_for_runner(
        self,
        check: dict[str, Any],
        runner: dict[str, Any],
        settings: dict[str, Any],
    ) -> list[BrowserRunTarget]:
        if not runner.get("available"):
            browser_type = ""
            try:
                browser_type = normalize_browser_types(check.get("browser_types"), default=[DEFAULT_BROWSER_TYPE])[0]
            except Exception:
                browser_type = DEFAULT_BROWSER_TYPE
            return [BrowserRunTarget(runner=runner, browser_type=browser_type, skip_reason="执行节点不可用")]
        enabled = set(enabled_browser_types(settings))
        installed = set(self._runner_installed_browser_types(runner))
        mode = normalize_browser_selection_mode(check.get("browser_selection_mode"))
        if mode == "round_robin_all":
            candidates = [browser_type for browser_type in enabled_browser_types(settings) if browser_type in installed]
            if not candidates:
                return [
                    BrowserRunTarget(
                        runner=runner,
                        browser_type="",
                        skip_reason="执行节点没有已启用且已安装的 browser type",
                    )
                ]
            index = storage.next_runner_cursor(
                f"check-browser:{int(check.get('id') or 0)}:{runner.get('runner_id') or storage.LOCAL_RUNNER_ID}",
                len(candidates),
            )
            return [BrowserRunTarget(runner=runner, browser_type=candidates[index])]

        selected = normalize_browser_types(check.get("browser_types"), default=[DEFAULT_BROWSER_TYPE])
        targets: list[BrowserRunTarget] = []
        for browser_type in selected:
            if browser_type not in enabled:
                targets.append(BrowserRunTarget(runner=runner, browser_type=browser_type, skip_reason=f"browser type 未启用：{browser_type}"))
            elif browser_type not in installed:
                targets.append(BrowserRunTarget(runner=runner, browser_type=browser_type, skip_reason=f"执行节点未安装 browser type：{browser_type}"))
            else:
                targets.append(BrowserRunTarget(runner=runner, browser_type=browser_type))
        return targets

    def _first_runnable_browser_type(self, check: dict[str, Any], runner: dict[str, Any], settings: dict[str, Any]) -> str:
        for target in self._resolve_browser_targets_for_runner(check, runner, settings):
            if not target.skip_reason and target.browser_type:
                return target.browser_type
        return DEFAULT_BROWSER_TYPE

    async def _run_distributed_check(self, check: dict[str, Any], targets: list[BrowserRunTarget], trigger: str) -> dict[str, Any]:
        check_id = int(check.get("id") or 0)
        run_group_id = f"rg_{uuid.uuid4().hex}"
        queued = False
        running = False

        async def run_started_group() -> dict[str, Any]:
            nonlocal queued, running
            async with self._lock:
                if queued:
                    self._queued_jobs = max(0, self._queued_jobs - 1)
                    queued = False
                self._running_jobs += 1
                running = True
            jobs = [self._dispatch_runner(check, target, trigger, run_group_id, capacity_managed=True) for target in targets]
            runs = await asyncio.gather(*jobs)
            return await self._finish_distributed_group(check, runs, trigger)

        async with self._lock:
            self._jobs = {job for job in self._jobs if not job.done()}
            if self._maintenance_active:
                return self._deployment_paused_result(check, trigger)
            if self._closing:
                return self._create_skipped_run(check, "执行器正在关闭，本次已跳过", trigger)
            if check_id > 0 and check_id in self._active_checks:
                return self._create_skipped_run(check, "同一任务上一次执行尚未结束或仍在排队，本次已跳过", trigger)
            if self._queued_jobs >= self._queue_limit:
                return self._create_skipped_run(check, "执行队列已满，本次已跳过", trigger)
            self._queued_jobs += 1
            queued = True
            if check_id > 0:
                self._active_checks.add(check_id)
        try:
            if self._uses_browser(check):
                async with self._ui_limiter.slot():
                    async with self._global_limiter.slot():
                        return await run_started_group()
            async with self._global_limiter.slot():
                return await run_started_group()
        finally:
            async with self._lock:
                if queued:
                    self._queued_jobs = max(0, self._queued_jobs - 1)
                if running:
                    self._running_jobs = max(0, self._running_jobs - 1)
                if check_id > 0:
                    self._active_checks.discard(check_id)

    async def _dispatch_runner(
        self,
        check: dict[str, Any],
        target: BrowserRunTarget,
        trigger: str,
        run_group_id: str,
        capacity_managed: bool = False,
    ) -> dict[str, Any]:
        runner = target.runner
        runner_id = str(runner.get("runner_id") or storage.LOCAL_RUNNER_ID)
        if runner_id != storage.LOCAL_RUNNER_ID:
            latest_runner = storage.get_probe_runner(runner_id)
            if latest_runner:
                runner = latest_runner
        check_payload = self._with_browser_type({**check, "_run_group_id": run_group_id}, target.browser_type)
        runner_payload = storage.runner_metadata(runner, browser_type=target.browser_type)
        metadata = {**run_metadata(trigger), "run_group_id": run_group_id}
        if check.get("_library_validation"):
            metadata.update({"trigger": trigger, "observation_kind": "library_validation", "affects_health": False})
        runner_system_metadata = {**metadata, "affects_health": False}
        if target.skip_reason:
            run = storage.create_run(
                self._with_runner_metadata(check_payload, storage.get_settings(), runner_payload, trigger=trigger, run_metadata_payload=runner_system_metadata),
                "pending",
            )
            unavailable = self._finish_runner_unavailable(int(run["id"]), check_payload, trigger, runner, target.skip_reason)
            if check.get("_library_validation"):
                return unavailable
            return await self._notify_system_result(
                check_payload,
                trigger,
                int(run["id"]),
                unavailable,
                self._settings_for_system_alert(),
                source="runner",
            )
        if not storage.can_schedule_runner(runner):
            run = storage.create_run(
                self._with_runner_metadata(check_payload, storage.get_settings(), runner_payload, trigger=trigger, run_metadata_payload=runner_system_metadata),
                "pending",
            )
            unavailable = self._finish_runner_unavailable(int(run["id"]), check, trigger, runner, "执行节点不可用")
            if not check.get("_library_validation"):
                unavailable = await self._notify_system_result(
                    check_payload,
                    trigger,
                    int(run["id"]),
                    unavailable,
                    self._settings_for_system_alert(),
                    source="runner",
                )
            await self._notify_runner_unavailable_if_needed(runner, check)
            return unavailable
        if runner_id != storage.LOCAL_RUNNER_ID and self._uses_mixed_api_flow(check) and not self._runner_supports_mixed_api_flow(runner):
            run = storage.create_run(
                self._with_runner_metadata(check_payload, storage.get_settings(), runner_payload, trigger=trigger, run_metadata_payload=runner_system_metadata),
                "pending",
            )
            unavailable = self._finish_runner_unavailable(
                int(run["id"]),
                check_payload,
                trigger,
                runner,
                "执行节点不支持脚本后接口校验，请先更新执行节点",
            )
            if check.get("_library_validation"):
                return unavailable
            return await self._notify_system_result(
                check_payload,
                trigger,
                int(run["id"]),
                unavailable,
                self._settings_for_system_alert(),
                source="runner",
            )
        if runner_id != storage.LOCAL_RUNNER_ID and self._uses_script_modules(check) and not self._runner_supports_script_modules(runner):
            run = storage.create_run(
                self._with_runner_metadata(check_payload, storage.get_settings(), runner_payload, trigger=trigger, run_metadata_payload=runner_system_metadata),
                "pending",
            )
            unavailable = self._finish_runner_unavailable(
                int(run["id"]),
                check_payload,
                trigger,
                runner,
                "执行节点不支持公共方法模块，请先更新执行节点",
            )
            if check.get("_library_validation"):
                return unavailable
            return await self._notify_system_result(
                check_payload,
                trigger,
                int(run["id"]),
                unavailable,
                self._settings_for_system_alert(),
                source="runner",
            )
        if runner_id == storage.LOCAL_RUNNER_ID:
            return await self._submit(
                check_payload,
                trigger,
                record_status=False,
                notify=True,
                runner_metadata=runner_payload,
                manage_active=False,
                manage_capacity=not capacity_managed,
            )
        run = storage.create_run(
            self._with_runner_metadata(check_payload, storage.get_settings(), runner_payload, trigger=trigger, run_metadata_payload=metadata),
            "pending",
        )
        run_id = int(run["id"])
        storage.start_run(run_id)
        try:
            result = await self._call_remote_runner(runner, check_payload, trigger, run_id)
        except Exception as exc:
            if not check.get("_library_validation"):
                storage.mark_probe_runner_unavailable(runner_id)
            unavailable = self._finish_runner_unavailable(run_id, check, trigger, runner, f"执行节点调用失败：{exc}")
            if not check.get("_library_validation"):
                unavailable = await self._notify_system_result(
                    check_payload,
                    trigger,
                    run_id,
                    unavailable,
                    self._settings_for_system_alert(),
                    source="runner",
                )
            latest_runner = storage.get_probe_runner(runner_id) or runner
            await self._notify_runner_unavailable_if_needed(latest_runner, check)
            return unavailable
        data = self._remote_result_to_finish_payload(result, run_id, runner)
        if self._is_runner_system_result(data):
            if check.get("_library_validation"):
                latest_runner = runner
            else:
                latest_runner = storage.mark_probe_runner_unavailable(runner_id, status="unhealthy") or storage.get_probe_runner(runner_id) or runner
            system_result = self._finish_runner_result(check_payload, trigger, run_id, data)
            if not check.get("_library_validation"):
                system_result = await self._notify_system_result(
                    check_payload,
                    trigger,
                    run_id,
                    system_result,
                    self._settings_for_system_alert(),
                    source="runner",
                )
            await self._notify_runner_unavailable_if_needed(latest_runner, check)
            return system_result
        if check.get("_library_validation"):
            data = {**data, "affects_health": False}
        finished = storage.finish_run(run_id, data)
        if check.get("_library_validation"):
            storage.update_run_notification(run_id, "not_required", channel=None, error=None, sent_at=None)
        return storage.get_run(run_id) or finished or run

    async def _call_remote_runner(self, runner: dict[str, Any], check: dict[str, Any], trigger: str, run_id: int) -> dict[str, Any]:
        try:
            storage.ensure_probe_runner_transport_allowed(runner)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        address = str(runner.get("address") or "").strip().rstrip("/")
        token = str(runner.get("_token") or "")
        if not address:
            raise RuntimeError("Runner address is empty")
        if not token:
            token = self._runner_token_from_row(runner)
        settings = storage.settings_for_project(storage.get_settings(), check.get("project_id"))
        payload = {
            "check": self._worker_check_payload(check),
            "trigger": trigger,
            "run_id": run_id,
            "settings": self._worker_settings(settings),
        }
        timeout_seconds = self._remote_runner_timeout_seconds(check, settings)
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    f"{address}/api/worker/run",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"执行节点调用超时，等待 {int(timeout_seconds)} 秒后未返回") from exc
        except httpx.RequestError as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            raise RuntimeError(f"执行节点请求失败：{detail}") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {self._response_error_detail(response)}")
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError(str(data.get("message") if isinstance(data, dict) else "invalid response"))
        return data

    def _remote_runner_timeout_seconds(self, check: dict[str, Any], settings: dict[str, Any]) -> float:
        return min(3600.0, max(30.0, self._job_timeout_seconds(check, settings)))

    def _job_timeout_seconds(self, check: dict[str, Any], settings: dict[str, Any]) -> float:
        check_timeout = max(0.5, float(check.get("timeout_ms") or 15000) / 1000)
        try:
            max_runtime = max(check_timeout, float(settings.get("max_task_runtime_seconds") or 60))
        except (TypeError, ValueError):
            max_runtime = max(check_timeout, 60.0)
        attempts = self._max_attempts(check, settings)
        preparation_budget = check_timeout if check.get("type") == "ui" and str(check.get("setup_script") or "").strip() else 0.0
        return min(3600.0, (max_runtime + preparation_budget) * attempts + 15.0)

    @staticmethod
    def _response_error_detail(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            text = response.text.strip()
        else:
            if isinstance(data, dict):
                text = str(data.get("detail") or data.get("message") or "").strip()
            else:
                text = str(data).strip()
        return text[:500] if text else "远程服务返回错误"

    def _remote_result_to_finish_payload(self, result: dict[str, Any], run_id: int, runner: dict[str, Any]) -> dict[str, Any]:
        data = result.get("run") if isinstance(result.get("run"), dict) else {}
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
        for field in ("screenshot_path", "trace_path", "response_path"):
            item = artifacts.get(field)
            if not isinstance(item, dict) or not item.get("content_base64"):
                continue
            try:
                saved = self.artifacts.save_uploaded_artifact(run_id, field, str(item["content_base64"]))
            except Exception as exc:
                logs = str(data.get("logs") or "")
                data["logs"] = f"{logs}\n远程证据保存失败：{field} {exc}".strip()
                continue
            data[field] = saved
        metadata = storage.runner_metadata(
            runner,
            failure_kind=str(data.get("failure_kind") or "none"),
            browser_version=str(data.get("runner_browser_version") or runner.get("browser_version") or ""),
            browser_type=str(data.get("browser_type") or ""),
        )
        return {
            "status": str(data.get("status") or "failed"),
            **({"affects_health": False} if data.get("status") == "preparation_failed" else {}),
            "finished_at": str(data.get("finished_at") or storage.now_iso()),
            "duration_ms": int(data.get("duration_ms") or 0),
            "error_message": data.get("error_message"),
            "error_stack": data.get("error_stack"),
            "logs": data.get("logs"),
            "screenshot_path": data.get("screenshot_path"),
            "trace_path": data.get("trace_path"),
            "response_path": data.get("response_path"),
            "request_snapshot": data.get("request_snapshot"),
            "response_snapshot": data.get("response_snapshot"),
            **metadata,
        }

    def _finish_runner_unavailable(
        self,
        run_id: int,
        check: dict[str, Any],
        trigger: str,
        runner: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        finished = storage.now_iso()
        payload = {
            "status": "skipped",
            "finished_at": finished,
            "duration_ms": 0,
            "error_message": mask_text(message, storage.get_settings()),
            "error_stack": None,
            "logs": mask_text(message, storage.get_settings()),
            "screenshot_path": None,
            "trace_path": None,
            "response_path": None,
            "request_snapshot": None,
            "response_snapshot": None,
            **storage.runner_metadata(runner, failure_kind="runner", browser_type=str(check.get("_browser_type") or "")),
            "affects_health": False,
        }
        return self._finish_runner_result(check, trigger, run_id, payload)

    async def _finish_distributed_group(self, check: dict[str, Any], runs: list[dict[str, Any]], trigger: str) -> dict[str, Any]:
        if not runs:
            skipped = self._create_skipped_run(check, "没有可执行节点，本次已跳过", trigger)
            if check.get("_library_validation"):
                return {**skipped, "runs": [skipped]}
            return skipped
        status_run = self._aggregate_group_run(runs)
        if check.get("_library_validation"):
            # Keep the complete node matrix for candidate review.  The single
            # status result remains useful to callers, while every child run is
            # returned under runs and has already been marked non-observational.
            return {
                **status_run,
                "check_id": int(check.get("id") or 0),
                "original_check_id": int(check.get("original_check_id") or check.get("id") or 0),
                "runs": runs,
                "affects_health": False,
                "observation_kind": "library_validation",
            }
        if status_run.get("affects_health") and status_run.get("failure_kind") != "runner":
            transition = storage.update_check_status(int(check["id"]), status_run)
            transition["trigger"] = trigger
            await notifier.maybe_notify(check, status_run, transition)
        return status_run

    @staticmethod
    def _aggregate_group_run(runs: list[dict[str, Any]]) -> dict[str, Any]:
        target_failures = [run for run in runs if run.get("failure_kind") == "target" and run.get("status") in {"failed", "timeout"}]
        if target_failures:
            return target_failures[0]
        non_successes = [run for run in runs if run.get("status") != "ok"]
        if non_successes:
            return non_successes[0]
        successes = [run for run in runs if run.get("status") == "ok"]
        if successes:
            return successes[0]
        return runs[0]

    async def _notify_runner_unavailable_if_needed(self, runner: dict[str, Any], check: dict[str, Any]) -> None:
        if check.get("_library_validation"):
            return
        latest = storage.get_probe_runner(str(runner.get("runner_id") or "")) or runner
        if not storage.should_notify_probe_runner_unavailable(latest):
            return
        await notifier.notify_runner_unavailable(latest, [check])

    @staticmethod
    def _script_module_references(check: dict[str, Any]) -> set[str]:
        references: set[str] = set()
        sources = (("check", check.get("script")), ("setup", check.get("setup_script")))
        for entry, raw in sources:
            source = str(raw or "")
            if not source.strip():
                continue
            analysis = analyze_script(source, entry_points=(entry,))
            references.update(str(item) for item in analysis.get("references") or [])
        return references

    @classmethod
    def _validate_module_references(cls, check: dict[str, Any], bundle: dict[str, Any]) -> None:
        static_errors: list[dict[str, Any]] = []
        for entry, raw in (("check", check.get("script")), ("setup", check.get("setup_script"))):
            source = str(raw or "")
            if not source.strip():
                continue
            analysis = analyze_script(source, entry_points=(entry,), module_bundle=bundle)
            static_errors.extend(item for item in analysis.get("diagnostics") or [] if item.get("level") == "error")
        if static_errors:
            first = static_errors[0]
            raise ValueError(str(first.get("message") or "公共方法引用校验失败"))

    @classmethod
    def _attach_frozen_module_bundle(
        cls,
        check: dict[str, Any],
        *,
        require_published: bool,
    ) -> dict[str, Any]:
        result = dict(check)
        project_id = int(result.get("project_id") or 0) or None
        explicit = result.get("_module_bundle")
        if explicit is not None:
            try:
                bundle = normalize_module_bundle(explicit, expected_project_id=project_id)
            except ModuleBundleError as exc:
                raise ValueError(str(exc)) from exc
            result["_module_bundle"] = bundle
            try:
                cls._validate_module_references(result, bundle)
            except ValueError as exc:
                raise ModulePreflightError(str(exc), bundle=bundle) from exc
            return result

        references = cls._script_module_references(result)
        if not references:
            return result
        bundle: Any = None
        if project_id is not None:
            bundle = storage.get_published_library(project_id)
        if bundle is None:
            if require_published:
                raise ValueError("任务引用公共方法，但当前项目没有已发布的方法版本")
            # Drafts with references must be supplied a captured candidate bundle
            # by the validation endpoint; ordinary debug payloads cannot inject one.
            return result
        try:
            normalized = normalize_module_bundle(bundle, expected_project_id=project_id)
        except ModuleBundleError as exc:
            raise ValueError(str(exc)) from exc
        result["_module_bundle"] = normalized
        try:
            cls._validate_module_references(result, normalized)
        except ValueError as exc:
            raise ModulePreflightError(str(exc), bundle=normalized) from exc
        return result

    @staticmethod
    def _worker_check_payload(check: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "id",
            "name",
            "type",
            "enabled",
            "interval_seconds",
            "timeout_ms",
            "entry_url",
            "viewport_mode",
            "method",
            "headers_json",
            "body",
            "assertions_json",
            "setup_script",
            "script",
            "tags",
            "alert_policy_json",
            "browser_selection_mode",
            "browser_types",
            "project_id",
            "original_check_id",
            "_library_validation",
        }
        payload = {key: check.get(key) for key in allowed if key in check}
        if check.get("_module_bundle") is not None:
            # This is the frozen transport snapshot captured by the coordinator;
            # a child never resolves module source from its own project database.
            payload["module_bundle"] = check.get("_module_bundle")
        return payload

    @staticmethod
    def _uses_mixed_api_flow(check: dict[str, Any]) -> bool:
        return (
            str(check.get("type") or "") == "api"
            and bool(str(check.get("script") or "").strip())
            and has_enabled_api_assertions(check.get("assertions_json"))
        )

    @staticmethod
    def _runner_supports_mixed_api_flow(runner: dict[str, Any]) -> bool:
        metadata = runner.get("metadata") if isinstance(runner.get("metadata"), dict) else {}
        capabilities = metadata.get("capabilities") if isinstance(metadata, dict) else None
        return isinstance(capabilities, list) and API_SCRIPT_ASSERTIONS_CAPABILITY in capabilities

    @classmethod
    def _uses_script_modules(cls, check: dict[str, Any]) -> bool:
        return bool(check.get("_module_bundle") or check.get("module_bundle")) and bool(cls._script_module_references(check))

    @staticmethod
    def _runner_supports_script_modules(runner: dict[str, Any]) -> bool:
        metadata = runner.get("metadata") if isinstance(runner.get("metadata"), dict) else {}
        capabilities = metadata.get("capabilities") if isinstance(metadata, dict) else None
        return isinstance(capabilities, list) and SCRIPT_MODULES_CAPABILITY in capabilities

    def _worker_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "max_task_runtime_seconds",
            "browser_headless",
            "browser_type",
            "enabled_browser_types",
            "prewarmed_browser_types",
            "browser_pool_sizes",
            "browser_recycle_after_runs",
            "browser_proxy",
            "browser_viewport",
            "trace_artifacts_enabled",
            "success_response_artifacts_enabled",
            "api_pool_size",
            "api_retry_attempts",
            "ui_retry_attempts",
            "environment_variables",
        }
        return {key: settings.get(key) for key in allowed}

    @staticmethod
    def _with_browser_type(check: dict[str, Any], browser_type: str) -> dict[str, Any]:
        if not browser_type:
            return dict(check)
        return {**check, "_browser_type": browser_type, "browser_type": browser_type}

    @staticmethod
    def _settings_for_check(check: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        browser_type = str(check.get("_browser_type") or check.get("browser_type") or settings.get("browser_type") or DEFAULT_BROWSER_TYPE)
        return settings_for_browser_type(settings, browser_type)

    @staticmethod
    def _runner_installed_browser_types(runner: dict[str, Any]) -> list[str]:
        if str(runner.get("runner_id") or "") == storage.LOCAL_RUNNER_ID:
            installed = browser_capabilities(storage.get_settings()).get("installed_browser_types") or []
            if installed:
                return normalize_browser_types(installed, default=[], allow_empty=True)
        installed = runner.get("installed_browser_types")
        if isinstance(installed, list) and installed:
            return normalize_browser_types(installed, default=[], allow_empty=True)
        browser_version = str(runner.get("browser_version") or "")
        inferred = [browser_type for browser_type in ("chromium", "firefox", "webkit") if browser_type in browser_version]
        if inferred:
            return inferred
        if str(runner.get("runner_id") or "") == storage.LOCAL_RUNNER_ID:
            return [DEFAULT_BROWSER_TYPE]
        return []

    @staticmethod
    def _runner_token_from_row(runner: dict[str, Any]) -> str:
        runner_id = str(runner.get("runner_id") or "")
        if not runner_id:
            return ""
        try:
            return storage.get_probe_runner_token(runner_id)
        except Exception:
            return ""

    @staticmethod
    def _local_runner_from_settings(settings: dict[str, Any]) -> dict[str, Any]:
        capabilities = browser_capabilities(settings)
        return {
            "runner_id": storage.LOCAL_RUNNER_ID,
            "name": str(settings.get("local_runner_name") or "local"),
            "address": str(settings.get("local_runner_address") or "127.0.0.1"),
            "network_region": str(settings.get("local_runner_region") or "local"),
            "browser_version": "",
            "installed_browser_types": capabilities["installed_browser_types"] or [DEFAULT_BROWSER_TYPE],
            "available_browser_types": capabilities["available_browser_types"] or [DEFAULT_BROWSER_TYPE],
            "browser_type_status": capabilities["browser_type_status"],
            "status": "ok",
            "enabled": True,
            "role": "local",
            "available": True,
        }

    @staticmethod
    def _uses_browser(check: dict[str, Any]) -> bool:
        return check.get("type") == "ui"

    @staticmethod
    def _uses_structured_api_check(check: dict[str, Any]) -> bool:
        return check.get("type") == "api" and has_enabled_api_assertions(check.get("assertions_json"))

    @staticmethod
    def _uses_structured_ui_check(check: dict[str, Any]) -> bool:
        return check.get("type") == "ui" and has_enabled_ui_assertions(check.get("assertions_json"))

    def _load_check_function(
        self,
        script: str,
        ctx: RunContext,
        function_name: str = "check",
        expected_signature: str = "async def check(ctx)",
        script_label: str = "脚本",
        *,
        attempt: int | None = None,
    ) -> Any:
        return compile_check_function(
            script,
            ctx,
            function_name,
            expected_signature,
            script_label,
            module_bundle=getattr(ctx, "module_bundle", None),
            attempt=attempt,
        )

    @staticmethod
    def _max_concurrency(settings: dict[str, Any] | None = None) -> int:
        try:
            resolved_settings = settings or storage.get_settings()
            return max(1, int(resolved_settings.get("max_concurrency", 2)))
        except Exception:
            return 2

    @staticmethod
    def _max_ui_concurrency(settings: dict[str, Any] | None = None) -> int:
        try:
            resolved_settings = settings or storage.get_settings()
            return max(1, int(resolved_settings.get("max_ui_concurrency", 1)))
        except Exception:
            return 1

    @staticmethod
    def _max_queue_size(settings: dict[str, Any] | None = None) -> int:
        try:
            resolved_settings = settings or storage.get_settings()
            return max(1, int(resolved_settings.get("max_queue_size", 50)))
        except Exception:
            return 50

    @staticmethod
    def _api_pool_size(settings: dict[str, Any] | None = None) -> int:
        try:
            resolved_settings = settings or storage.get_settings()
            return max(1, int(resolved_settings.get("api_pool_size", 5)))
        except Exception:
            return 5

    @staticmethod
    def _browser_pool_sizes(settings: dict[str, Any] | None = None) -> dict[str, int]:
        try:
            resolved_settings = settings or storage.get_settings()
            return browser_pool_sizes(resolved_settings)
        except Exception:
            return {"chromium": 5, "firefox": 5, "webkit": 5}

    @staticmethod
    def _refresh_local_runner_browser_capabilities(settings: dict[str, Any]) -> None:
        capabilities = browser_capabilities(settings)
        storage.mark_probe_runner_available(
            storage.LOCAL_RUNNER_ID,
            {
                "status": "ok",
                "browser_version": "",
                "installed_browser_types": capabilities["installed_browser_types"],
                "available_browser_types": capabilities["available_browser_types"],
                "metadata": {"browser_type_status": capabilities["browser_type_status"]},
            },
        )

    async def ensure_enabled_browser_types_installed(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved_settings = settings or storage.get_settings()
        return await ensure_browser_types_installed(enabled_browser_types(resolved_settings))
