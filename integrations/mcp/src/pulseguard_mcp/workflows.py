"""Durable batches and staged complete check editing. No direct DB or runner access."""
from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .output import project_fields

Id = Annotated[int, Field(gt=0, strict=True)]
Key = Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
IDEMPOTENT_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)


def register_workflows(server, settings, request, clean, owned, read, execute):
    async def project_request(method, path, project_id, **kwargs):
        return clean(owned(await request(method, path, project_id=project_id, **kwargs), project_id))

    @server.tool(annotations=read)
    async def get_execution_batch(project_id: Id, batch_id: UUID | None = None,
                                   idempotency_key: Key | None = None) -> dict[str, Any]:
        """Read durable batch receipt, item outcomes and correlated run IDs. Supply exactly one lookup key.
        unknown means external effects may have happened; never submit those items under a fresh key automatically.
        """
        if (batch_id is None) == (idempotency_key is None):
            raise ToolError("Supply exactly one of batch_id and idempotency_key")
        return await project_request("GET", f"/api/execution-batches/{batch_id}" if batch_id else "/api/execution-batches",
                                     project_id, params={"idempotency_key": idempotency_key})

    if settings.allow_execution:
        @server.tool(annotations=IDEMPOTENT_WRITE)
        async def submit_execution_batch(project_id: Id, check_ids: Annotated[list[Id], Field(min_length=1, max_length=100)],
                                          idempotency_key: Key) -> dict[str, Any]:
            """Submit authorized real probes and return a durable receipt immediately. May write targets/send alerts.
            Choose one key per user-approved batch; retry only the identical key and set, never a fresh key after timeout.
            Queued items survive restart; interrupted running items stay unknown, without automatic replay.
            """
            return await project_request("POST", "/api/execution-batches", project_id,
                json_body={"idempotency_key": idempotency_key, "check_ids": check_ids})

        @server.tool(annotations=IDEMPOTENT_WRITE)
        async def cancel_execution_batch(project_id: Id, batch_id: UUID) -> dict[str, Any]:
            """Cancel only not-yet-dispatched items. Running requests/effects are not cancelled or rolled back."""
            return await project_request("POST", f"/api/execution-batches/{batch_id}/cancel", project_id)

    if settings.allow_configuration:
        @server.tool(annotations=read)
        async def get_check_edit_schema(project_id: Id) -> dict[str, Any]:
            """Get all editable task fields and validation types. Configuration access required."""
            return await project_request("GET", "/api/check-edit-schema", project_id)

        @server.tool(annotations=read)
        async def get_check_edit_context(project_id: Id, check_id: Id) -> dict[str, Any]:
            """Get current definition fingerprint and non-sensitive fields. Opaque values are NOT editable values.
            Submit only intended changed fields; omitted scripts, headers, bodies, assertions and alerts are preserved.
            """
            return await project_request("GET", f"/api/checks/{check_id}/edit-context", project_id)

        @server.tool(annotations=read)
        async def list_check_versions(project_id: Id, check_id: Id,
                                      limit: Annotated[int, Field(ge=1, le=100)] = 50) -> dict[str, Any]:
            """List saved version IDs for restore previews, without exporting historical scripts or credentials."""
            rows = await request("GET", f"/api/checks/{check_id}/versions", project_id=project_id, params={"limit": limit})
            return clean({"project_id": project_id, "items": [project_fields(owned(r, project_id),
                ("id", "project_id", "check_id", "action", "created_at")) for r in rows], "possibly_truncated": len(rows) == limit})

        @server.tool(annotations=execute)
        async def preview_check_change(project_id: Id, changes: dict[str, Any] | None = None, check_id: Id | None = None,
                                       expected_hash: Digest | None = None, version_id: Id | None = None) -> dict[str, Any]:
            """Validate and stage a complete task create/partial edit/version restore for 30 minutes; no probe runs.
            Existing tasks require fresh edit-context hash. Restore requires version_id and empty changes.
            New tasks default disabled. Review normalized diff, including enable/schedule changes, before apply.
            """
            return await project_request("POST", "/api/check-changes/preview", project_id, json_body={
                "changes": changes or {}, "check_id": check_id, "expected_hash": expected_hash, "version_id": version_id})

        @server.tool(annotations=read)
        async def get_check_change(project_id: Id, change_id: UUID) -> dict[str, Any]:
            """Read staged/applied/expired/cancelled change receipt; contains no raw script or credential snapshot."""
            return await project_request("GET", f"/api/check-changes/{change_id}", project_id)

        @server.tool(annotations=IDEMPOTENT_WRITE)
        async def apply_check_change(project_id: Id, change_id: UUID, expected_digest: Digest) -> dict[str, Any]:
            """Apply the user-authorized preview atomically with version history/audit. Enabling starts scheduling.
            Rejects concurrent edits. Retry the SAME change ID/digest after querying its receipt, never recreate blindly.
            """
            return await project_request("POST", f"/api/check-changes/{change_id}/apply", project_id,
                                         json_body={"expected_digest": expected_digest})

        @server.tool(annotations=IDEMPOTENT_WRITE)
        async def cancel_check_change(project_id: Id, change_id: UUID) -> dict[str, Any]:
            """Discard a pending preview and its stored candidate. Does not roll back an applied change."""
            return await project_request("POST", f"/api/check-changes/{change_id}/cancel", project_id)
