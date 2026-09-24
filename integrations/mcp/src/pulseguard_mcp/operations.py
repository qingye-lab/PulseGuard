"""Project operations and explicitly enabled platform/configuration capabilities."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .output import CHECK_FIELDS, project_fields, run_summary

PositiveId = Annotated[int, Field(gt=0, strict=True)]
Limit = Annotated[int, Field(ge=1, le=100, strict=True)]
CheckType = Literal["ui", "api"]


def validate_dates(start: str | None, end: str | None) -> None:
    parsed = []
    for value in (start, end):
        if value is not None:
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if date.tzinfo is None:
                    raise ValueError
                parsed.append(date)
            except ValueError:
                raise ToolError("Use ISO 8601 dates with an explicit timezone") from None
    if len(parsed) == 2 and parsed[0] >= parsed[1]:
        raise ToolError("start must be earlier than end")


def register_operations(server, settings, request, clean, owned, read, execute):
    @server.tool(annotations=read)
    async def get_project_overview(project_id: PositiveId) -> dict[str, Any]:
        """Current project health, stale/unknown tasks and recent target failures. Not platform health."""
        result = await request("GET", "/api/overview", project_id=project_id)
        payload = project_fields(result, (
            "ui_count", "api_count", "healthy_count", "failing_count", "suspected_count",
            "unknown_count", "stale_count", "today_runs", "latest_recovered",
        ))
        payload["project_id"] = project_id
        payload["latest_run"] = run_summary(owned(result["latest_run"], project_id)) if result.get("latest_run") else None
        payload["recent_failures"] = [run_summary(owned(row, project_id)) for row in result.get("recent_failures", [])]
        return clean(payload)

    @server.tool(annotations=read)
    async def get_run_statistics(
        project_id: PositiveId, start: str, end: str, check_type: CheckType | None = None,
        observation_kind: Literal["observation", "verification", "draft"] | None = None,
        check_id: PositiveId | None = None, runner_id: str | None = None, include_archived: bool = True,
    ) -> dict[str, Any]:
        """Execution counts by day/type/status including eligible archives, [start,end).
        Report sources, coverage and warnings. total includes skipped/preparation failures;
        executed_terminal_count counts ok/failed/timeout. Legacy gaps cannot be reconstructed.
        """
        validate_dates(start, end)
        result = await request("GET", "/api/run-statistics", project_id=project_id, params={
            "start": start, "end": end, "type": check_type, "observation_kind": observation_kind,
            "check_id": check_id, "runner_id": runner_id, "include_archived": include_archived,
        })
        return clean(owned(result, project_id))

    @server.tool(annotations=read)
    async def get_monitoring_trends(project_id: PositiveId, start: str, end: str,
                                    check_type: CheckType | None = None, page: PositiveId = 1,
                                    page_size: Annotated[int, Field(ge=1, le=50)] = 12) -> dict[str, Any]:
        """Health-observation success rate and latency trends, excluding runner failures and skipped runs.
        NOT all execution attempts. Bucketed boundaries may extend beyond requested instants.
        """
        validate_dates(start, end)
        result = await request("GET", "/api/monitoring-trends", project_id=project_id, params={
            "period": "custom", "start": start, "end": end, "type": check_type, "page": page, "page_size": page_size,
        })
        return clean({"project_id": project_id, "metric_scope": "health_observations", **result})

    @server.tool(annotations=read)
    async def list_incidents(project_id: PositiveId, status: Literal["open", "resolved"] | None = None,
                             limit: Limit = 50) -> dict[str, Any]:
        """Recent project anomaly cycles and recovery timestamps; bounded list, not all-time incident total."""
        items = await request("GET", "/api/anomaly-cycles", project_id=project_id, params={"status": status, "limit": limit})
        fields = ("id", "project_id", "check_id", "check_name", "check_type", "status", "opened_at",
                  "resolved_at", "first_run_id", "last_run_id", "failure_count", "failure_kind", "summary")
        return clean({"items": [project_fields(owned(item, project_id), fields) for item in items],
                      "limit": limit, "possibly_truncated": len(items) == limit})

    @server.tool(annotations=read)
    async def list_audit_events(project_id: PositiveId, limit: Limit = 50) -> dict[str, Any]:
        """Recent changes in this project. Omits raw configuration payloads and global events."""
        items = await request("GET", "/api/audit-events", project_id=project_id, params={"limit": limit})
        fields = ("id", "project_id", "action", "entity_type", "entity_id", "entity_name", "summary", "created_at")
        return clean({"items": [project_fields(item, fields) for item in items if item.get("project_id") == project_id],
                      "limit": limit, "possibly_truncated": len(items) == limit})

    if settings.allow_platform_read:
        @server.tool(annotations=read)
        async def get_platform_status() -> dict[str, Any]:
            """Instance-wide scheduler/queue/maintenance and storage pressure. Requires platform-read permission."""
            runtime = await request("GET", "/api/runtime")
            database = await request("GET", "/api/database-health")
            return clean({"runtime": project_fields(runtime, (
                "queue", "workers", "browser", "active_checks", "closing", "maintenance_active", "scheduler", "deployment", "node_role",
            )), "database": project_fields(database, (
                "status", "file_size_bytes", "wal_size_bytes", "estimated_used_bytes", "reclaimable_bytes", "maintenance_recommended",
            ))})

        @server.tool(annotations=read)
        async def list_runners(limit: Limit = 100) -> dict[str, Any]:
            """Instance-wide execution-node health; not project-scoped. Omits tokens and provisioning commands."""
            items = await request("GET", "/api/runners", params={"limit": limit})
            fields = ("id", "runner_id", "name", "status", "enabled", "network_region", "role", "connection_mode",
                      "last_seen_at", "relay_last_seen_at", "worker_health_last_success_at", "last_disconnect_reason",
                      "installed_browser_types", "available_browser_types", "browser_version", "available")
            return clean({"items": [project_fields(item, fields) for item in items], "limit": limit,
                          "possibly_truncated": len(items) == limit})

    if settings.allow_configuration:
        @server.tool(annotations=execute)
        async def set_check_enabled(project_id: PositiveId, check_id: PositiveId, enabled: bool) -> dict[str, Any]:
            """Enable/disable a saved task, affecting scheduled monitoring. Requires user-authorized configuration.
            On unknown outcome read get_check before deciding whether another action is needed.
            """
            result = await request("POST", f"/api/checks/{check_id}/{'enable' if enabled else 'disable'}", project_id=project_id)
            if result.get("id") != check_id:
                raise ToolError("Configuration outcome unknown; read get_check to verify the saved state")
            return clean(project_fields(owned(result, project_id), CHECK_FIELDS))

    if settings.allow_execution:
        @server.tool(annotations=execute)
        async def confirm_recovery(project_id: PositiveId, check_id: PositiveId) -> dict[str, Any]:
            """Run a real recovery confirmation, possibly updating health and sending alerts. No automatic retry."""
            result = await request("POST", f"/api/checks/{check_id}/confirm-recovery", project_id=project_id)
            if result.get("check_id") != check_id or "status" not in result:
                raise ToolError("Execution outcome unknown; query list_runs to inspect the result")
            return clean({"project_id": project_id, "passed": result["status"] == "ok",
                          "run": run_summary(owned(result, project_id))})
