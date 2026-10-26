"""Statistics over retained executions and disjoint legacy daily archives.

Do not use health trends as execution counts: they intentionally exclude runner
failures, skipped runs and other non-health observations.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from . import storage


def time_window(start: str, end: str) -> tuple[datetime, datetime]:
    try:
        first, last = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (start, end))
        if first.tzinfo is None or last.tzinfo is None:
            raise ValueError
        first, last = first.astimezone(), last.astimezone()
        if first >= last or (last - first).days > 3660:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("需要带时区的 start/end，范围为 [start, end)，最长 3660 天") from None
    return first, last


def get_run_statistics(project_id: int, start: str, end: str, *, check_type: str | None = None,
                       observation_kind: str | None = None, check_id: int | None = None,
                       runner_id: str | None = None, include_archived: bool = True) -> dict[str, Any]:
    first, last = time_window(start, end)
    first_text, last_text = (v.isoformat() for v in (first, last))
    clauses, params = storage._run_filter_clauses({
        "project_id": project_id, "check_type": check_type, "observation_kind": observation_kind,
        "check_id": check_id, "runner_id": runner_id,
    })
    clauses += ["started_at >= ?", "started_at < ?", "check_id > 0"]
    params += [first_text, last_text]
    warnings: list[str] = []
    with storage._connect(read_only=True) as conn:
        as_of = storage.now_iso()
        retained = [dict(row) for row in conn.execute(
            f"""SELECT substr(started_at, 1, 10) AS date, check_type, status, COUNT(*) AS count,
                       SUM(COALESCE(deduplicated_count, 0)) AS legacy_merged_count
                FROM run_records WHERE {' AND '.join(clauses)} GROUP BY date, check_type, status""", params)]
        archive_clauses = ["project_id = ?", "archive_date >= ?", "archive_date <= ?"]
        archive_params: list[Any] = [project_id, first.date().isoformat(), last.date().isoformat()]
        if check_type:
            archive_clauses.append("check_type = ?")
            archive_params.append(check_type)
        available_archives = [dict(row) for row in conn.execute(
            f"SELECT archive_date AS date, check_type, status, run_count AS count FROM run_archives WHERE {' AND '.join(archive_clauses)}",
            archive_params)]
        installed = conn.execute("SELECT installed_at FROM run_history_policy WHERE id = 1").fetchone()
        oldest = conn.execute("SELECT MIN(started_at) FROM run_records WHERE project_id = ?", (project_id,)).fetchone()[0]
    archives = []
    unsupported = bool(observation_kind or check_id or runner_id)
    excluded_archive_count = 0
    for row in available_archives:
        # Midnight end is exclusive; no rows from that day belong to the window.
        if row["date"] == last.date().isoformat() and last.time() == time.min:
            continue
        partial = ((row["date"] == first.date().isoformat() and first.time() != time.min)
                   or (row["date"] == last.date().isoformat() and last.time() != time.min))
        if not include_archived or unsupported or partial:
            excluded_archive_count += row["count"]
        else:
            archives.append(row)
    if excluded_archive_count:
        warnings.append("部分归档无法纳入：归档仅保留日/项目/类型/状态，不支持任务、节点、运行来源或日内边界筛选，或调用者关闭了归档。")
    legacy_merged = sum(row.pop("legacy_merged_count") or 0 for row in retained)
    history_verified = bool(installed and first >= datetime.fromisoformat(installed[0]))
    if not history_verified:
        warnings.append("查询包含新保留策略启用前的历史；旧版同错合并、删除或迁移可能已丢失明细，不能宣称全历史精确总量。")
    if legacy_merged:
        warnings.append("存在旧版合并次数，因缺少各次执行时间，不分配到本次时间范围或再次计入总数。")
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for row in retained + archives:
        for target, key in ((by_type, row["check_type"]), (by_status, row["status"]), (by_day, row["date"])):
            target[key] = target.get(key, 0) + row["count"]
    total = sum(by_status.values())
    return {
        "project_id": project_id, "start": first_text, "end": last_text, "as_of": as_of,
        "interval": "[start,end)", "timezone": str(first.tzinfo),
        "filters": {"check_type": check_type, "observation_kind": observation_kind, "check_id": check_id, "runner_id": runner_id},
        "total": total, "by_type": by_type, "by_status": by_status, "by_day": by_day,
        "executed_terminal_count": sum(by_status.get(k, 0) for k in ("ok", "failed", "timeout")),
        "sources": {"retained": sum(r["count"] for r in retained), "archived": sum(r["count"] for r in archives)},
        "coverage": {"complete": history_verified and not excluded_archive_count and not legacy_merged,
                     "oldest_retained_at": oldest, "policy_since": installed[0] if installed else None,
                     "excluded_archive_count": excluded_archive_count, "legacy_merged_count": legacy_merged},
        "warnings": warnings,
    }
