from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Settings


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ApiError(Exception):
    """A safe, actionable error; never includes response bodies or credentials."""


class PulseGuardClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    def check_project(self, project_id: int) -> None:
        if type(project_id) is not int or project_id <= 0:
            raise ApiError("project_id must be a positive integer")
        if self.settings.project_ids is not None and project_id not in self.settings.project_ids:
            raise ApiError("Project is outside this MCP server's configured scope")

    async def request(
        self, method: str, path: str, *, project_id: int | None = None,
        params: dict[str, Any] | None = None, image: bool = False,
    ) -> Any:
        if project_id is not None:
            self.check_project(project_id)
        mutation = method != "GET"
        if mutation and not self.settings.allow_execution:
            raise ApiError("Execution is disabled in this MCP server configuration")
        headers = {"Accept": "image/png" if image else "application/json"}
        if project_id is not None:
            headers["X-PulseGuard-Project-Id"] = str(project_id)
        if self.settings.api_token:
            headers["Authorization"] = f"Bearer {self.settings.api_token}"
        timeout = self.settings.execution_timeout if mutation else self.settings.request_timeout
        try:
            # Keep proxy / CA environment inheritance. Never follow an upstream redirect
            # with credentials. A fresh context also closes on MCP cancellation.
            async with httpx.AsyncClient(
                transport=self.transport, timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False,
            ) as client:
                async with client.stream(
                    method, self.settings.base_url + path, headers=headers,
                    params={k: v for k, v in (params or {}).items() if v is not None},
                ) as response:
                    if response.status_code != 200:
                        if mutation and not 400 <= response.status_code < 500:
                            raise ApiError(_unknown_outcome(project_id))
                        messages = {
                            401: "API authentication failed; check the configured gateway credential",
                            403: "The API denied this operation",
                            404: "Resource not found in the selected project, or the API route is unavailable",
                            409: "The server rejected the operation because its current state does not allow it",
                        }
                        raise ApiError(messages.get(response.status_code, f"Unexpected API HTTP status {response.status_code}"))
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ApiError(_unknown_outcome(project_id) if mutation else "API response exceeds 8 MiB; narrow the query or use the console")
                    if image:
                        if not bytes(body).startswith(b"\x89PNG\r\n\x1a\n"):
                            raise ApiError("The recorded screenshot is not a PNG image")
                        return bytes(body)
                    try:
                        payload = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise ApiError(_unknown_outcome(project_id) if mutation else "API returned invalid JSON; check the service URL and gateway") from None
                    if not isinstance(payload, (dict, list)):
                        raise ApiError(_unknown_outcome(project_id) if mutation else "API returned an unexpected response shape")
                    return payload
        except httpx.RequestError:
            raise ApiError(
                _unknown_outcome(project_id) if mutation
                else "Unable to read the PulseGuard API; check connectivity, TLS, proxy settings and timeout"
            ) from None


def _unknown_outcome(project_id: int | None) -> str:
    return (
        f"Execution outcome unknown for project {project_id}. The server may still be running the check. "
        "Do not retry automatically. Query list_runs for the check and inspect its timestamps and status. "
        "A disconnected request does not prove cancellation or probe failure."
    )
