"""Additive automation API. Shares project dependencies and validation with the console."""
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import automation_jobs as jobs, check_changes as changes, storage
from .schemas import CheckCreate
from .config import NODE_ROLE

Id = Annotated[int, Field(gt=0, strict=True)]
Key = Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class BatchSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: Key
    check_ids: list[Id] = Field(min_length=1, max_length=100)


class ChangePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: Id | None = None
    expected_hash: str | None = Field(default=None, min_length=64, max_length=64)
    version_id: Id | None = None
    changes: dict[str, Any] = Field(default_factory=dict)


class ChangeApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_digest: str = Field(min_length=64, max_length=64)


def register(app, project_context, active_project_context, validate):
    def required(value):
        if value is None:
            raise HTTPException(404, "当前项目中不存在此回执或资源")
        return value

    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValidationError:
            # Pydantic input/ctx can echo scripts and credentials. Never expose it.
            raise HTTPException(422, "任务定义校验失败；请检查字段类型、必填字段、断言与脚本入口") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/execution-batches")
    def submit(payload: BatchSubmit, project_id: int = Depends(active_project_context)):
        if NODE_ROLE == "worker":
            raise HTTPException(409, "执行批次只允许提交到主节点")
        return invoke(jobs.submit, project_id, payload.idempotency_key, payload.check_ids)

    @app.get("/api/execution-batches")
    def lookup(idempotency_key: Key = Query(), project_id: int = Depends(project_context)):
        return required(jobs.get_batch(project_id, key=idempotency_key))

    @app.get("/api/execution-batches/{batch_id}")
    def batch(batch_id: str, project_id: int = Depends(project_context)):
        return required(jobs.get_batch(project_id, batch_id))

    @app.post("/api/execution-batches/{batch_id}/cancel")
    def cancel_batch(batch_id: str, project_id: int = Depends(project_context)):
        return required(jobs.cancel(project_id, batch_id))

    @app.get("/api/check-edit-schema")
    def schema(project_id: int = Depends(project_context)):
        return {"project_id": project_id, "schema": CheckCreate.model_json_schema()}

    @app.get("/api/checks/{check_id}/edit-context")
    def edit_context(check_id: int, project_id: int = Depends(project_context)):
        return required(changes.context(project_id, check_id))

    @app.post("/api/check-changes/preview")
    def preview(payload: ChangePreview, project_id: int = Depends(active_project_context)):
        return invoke(changes.prepare, project_id, payload.changes, check_id=payload.check_id,
                      expected_hash=payload.expected_hash, version_id=payload.version_id, validate=validate)

    @app.get("/api/check-changes/{change_id}")
    def change(change_id: str, project_id: int = Depends(project_context)):
        return required(changes.get_change(project_id, change_id))

    @app.post("/api/check-changes/{change_id}/apply")
    def apply(change_id: str, payload: ChangeApply, request: Request, project_id: int = Depends(active_project_context)):
        result = required(invoke(changes.apply, project_id, change_id, payload.expected_digest, validate=validate))
        # A retry repairs a missed scheduler sync without applying the definition again.
        request.app.state.scheduler.sync_check(result["result"]["check_id"])
        return result

    @app.post("/api/check-changes/{change_id}/cancel")
    def cancel_change(change_id: str, project_id: int = Depends(project_context)):
        return required(changes.cancel(project_id, change_id))
