"""Preview/CAS/apply workflow for complete check definitions; no probe execution."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from . import storage
from .automation_jobs import definition_hash, digest
from .schemas import CheckCreate
from .variables import mask_data

OPAQUE_FIELDS = {"headers_json", "body", "script", "setup_script", "assertions_json", "alert_policy_json"}


def init_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS check_change_drafts (
        id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), check_id INTEGER,
        base_hash TEXT, candidate_json TEXT, preview_json TEXT NOT NULL, digest TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        result_json TEXT)""")
    _expire(conn)


def _safe_definition(definition: dict, project_id: int) -> dict:
    settings = storage.settings_for_project(storage.get_settings(), project_id)
    return mask_data({k: ("[content omitted; omit this field to preserve it]" if k in OPAQUE_FIELDS else v)
                      for k, v in definition.items()}, settings)


def context(project_id: int, check_id: int) -> dict | None:
    check = storage.get_check(check_id, refresh_stale=False, project_id=project_id)
    if not check:
        return None
    return {"project_id": project_id, "check_id": check_id, "definition_hash": definition_hash(check),
            "definition": _safe_definition(storage._check_snapshot(check), project_id), "opaque_fields": sorted(OPAQUE_FIELDS)}


def prepare(project_id: int, changes: dict[str, Any], *, check_id: int | None = None,
            expected_hash: str | None = None, version_id: int | None = None,
            validate: Callable[[dict], dict]) -> dict:
    unknown = set(changes) - set(CheckCreate.model_fields)
    if unknown:
        raise ValueError("包含不支持的配置字段")
    if len(json.dumps(changes, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
        raise ValueError("任务定义超过 2 MiB")
    with storage.write_transaction():
        before = None
        if check_id:
            before = storage.get_check(check_id, refresh_stale=False, project_id=project_id)
            if not before or not expected_hash or definition_hash(before) != expected_hash:
                raise ValueError("任务已变化或不在当前项目，请刷新编辑上下文")
        elif expected_hash or version_id:
            raise ValueError("版本恢复和基线校验必须指定现有任务")
        if version_id:
            if changes:
                raise ValueError("版本恢复不能同时提交字段修改")
            version = storage.get_check_version(version_id, project_id=project_id)
            if not version or version["check_id"] != check_id:
                raise ValueError("版本不属于当前任务或项目")
            candidate = version["snapshot"]
        else:
            candidate = {**(storage._check_snapshot(before) if before else {"enabled": False}), **changes}
        # Never execute user code in a configuration preview.
        candidate = validate(CheckCreate.model_validate(candidate).model_dump())
        for field in ("script", "setup_script"):
            if candidate.get(field):
                try:
                    compile(candidate[field], f"<check:{field}>", "exec")
                except SyntaxError as exc:
                    raise ValueError(f"{field} 语法错误，行 {exc.lineno}") from None
        before_data = storage._check_snapshot(before) if before else {}
        changed = {k: {"before": before_data.get(k), "after": v} for k, v in candidate.items() if before_data.get(k) != v}
        for field in OPAQUE_FIELDS & changed.keys():
            changed[field] = {"before": "[content omitted]", "after": "[content omitted]"}
        settings = storage.settings_for_project(storage.get_settings(), project_id)
        preview = {"operation": "restore" if version_id else "update" if before else "create", "version_id": version_id,
                   "changes": mask_data(changed, settings), "enabled_after": candidate["enabled"],
                   "warnings": ["应用启用状态的任务会更新定时调度；保存本身不立即执行探活。", "未提供的字段保持不变；脱敏或省略值不可回填。"]}
        change_id = str(uuid.uuid4())
        confirmation = digest({"id": change_id, "project_id": project_id, "check_id": check_id,
                               "base_hash": expected_hash, "candidate": candidate})
        with storage._connect() as conn:
            _expire(conn)
            count = conn.execute("SELECT COUNT(*) FROM check_change_drafts WHERE state='pending'").fetchone()[0]
            if count >= 100:
                raise ValueError("待确认变更过多，请先取消或应用现有变更")
            conn.execute("""INSERT INTO check_change_drafts(id,project_id,check_id,base_hash,candidate_json,
                preview_json,digest,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (change_id, project_id, check_id, expected_hash, json.dumps(candidate, ensure_ascii=False), json.dumps(preview, ensure_ascii=False),
                 confirmation, storage.now_iso(), (datetime.now().astimezone() + timedelta(minutes=30)).isoformat(timespec="seconds")))
        return get_change(project_id, change_id)


def _expire(conn) -> None:
    conn.execute("UPDATE check_change_drafts SET candidate_json=NULL,state='expired' WHERE state='pending' AND expires_at<=?", (storage.now_iso(),))


def get_change(project_id: int, change_id: str) -> dict | None:
    with storage._connect(read_only=True) as conn:
        row = conn.execute("SELECT * FROM check_change_drafts WHERE project_id=? AND id=?", (project_id, change_id)).fetchone()
    if not row:
        return None
    state = "expired" if row["state"] == "pending" and row["expires_at"] <= storage.now_iso() else row["state"]
    return {"id": row["id"], "project_id": project_id, "check_id": row["check_id"], "state": state,
            "digest": row["digest"], "expires_at": row["expires_at"], "preview": json.loads(row["preview_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None}


def apply(project_id: int, change_id: str, expected_digest: str, *, validate: Callable[[dict], dict]) -> dict | None:
    with storage.write_transaction():
        with storage._connect() as conn:
            row = conn.execute("SELECT * FROM check_change_drafts WHERE project_id=? AND id=?", (project_id, change_id)).fetchone()
            if not row:
                return None
            if expected_digest != row["digest"]:
                raise ValueError("变更摘要不匹配；必须确认当前预览")
            if row["state"] == "applied":
                return get_change(project_id, change_id)
            if row["state"] != "pending" or row["expires_at"] <= storage.now_iso():
                raise ValueError("变更已取消或过期，请重新预览")
            project = storage.get_project(project_id)
            if not project or project["status"] != "active" or storage.is_deployment_window_active():
                raise ValueError("项目只读或处于部署维护中")
            before = storage.get_check(row["check_id"], refresh_stale=False, project_id=project_id) if row["check_id"] else None
            if row["check_id"] and (not before or definition_hash(before) != row["base_hash"]):
                raise ValueError("预览后任务发生变化；未覆盖现有配置，请重新预览")
            candidate = validate(CheckCreate.model_validate(json.loads(row["candidate_json"])).model_dump())
            if before:
                storage.record_check_version(before, "mcp-before-update")
                saved = storage.update_check(before["id"], candidate, project_id=project_id)
            else:
                saved = storage.create_check(candidate, project_id=project_id)
            storage.record_check_version(saved, "mcp-applied")
            receipt = {"check_id": saved["id"], "definition_hash": definition_hash(saved), "enabled": saved["enabled"]}
            storage.record_audit_event("applied", "check_change", saved["id"], saved["name"], "应用任务配置预览",
                                       {"change_id": change_id, "fields": list(json.loads(row["preview_json"])["changes"])}, project_id)
            conn.execute("UPDATE check_change_drafts SET state='applied',candidate_json=NULL,result_json=? WHERE id=?",
                         (json.dumps(receipt), change_id))
            return get_change(project_id, change_id)


def cancel(project_id: int, change_id: str) -> dict | None:
    with storage._connect() as conn:
        conn.execute("UPDATE check_change_drafts SET state='cancelled',candidate_json=NULL WHERE project_id=? AND id=? AND state='pending'",
                     (project_id, change_id))
    return get_change(project_id, change_id)
