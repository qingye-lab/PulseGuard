from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    base_url: str
    project_ids: frozenset[int] | None
    allow_execution: bool = False
    api_token: str = field(default="", repr=False)
    request_timeout: int = 30
    execution_timeout: int = 300

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        base_url = env.get("PULSEGUARD_MCP_BASE_URL", "http://127.0.0.1:8787").strip().rstrip("/")
        try:
            url = urlsplit(base_url)
            _ = url.port
        except ValueError:
            raise ValueError("PULSEGUARD_MCP_BASE_URL must be an HTTP(S) service URL") from None
        if (
            url.scheme not in {"http", "https"} or not url.hostname
            or url.username is not None or url.password is not None or url.query or url.fragment
            or any(char.isspace() for char in base_url) or "\\" in base_url
        ):
            raise ValueError("PULSEGUARD_MCP_BASE_URL must not contain credentials, query, or fragment")
        raw_scope = env.get("PULSEGUARD_MCP_PROJECT_IDS", "").strip()
        if not raw_scope:
            raise ValueError("Set PULSEGUARD_MCP_PROJECT_IDS to comma-separated project IDs, or explicitly '*' for all")
        scope = None
        if raw_scope != "*":
            try:
                parts = raw_scope.split(",")
                if any(not part.strip().isascii() or not part.strip().isdecimal() for part in parts):
                    raise ValueError
                scope = frozenset(int(part.strip()) for part in parts)
                if any(item <= 0 for item in scope):
                    raise ValueError
            except ValueError:
                raise ValueError("PULSEGUARD_MCP_PROJECT_IDS must contain positive integers or '*'") from None
        execution = env.get("PULSEGUARD_MCP_ALLOW_EXECUTION", "false").strip().lower()
        if execution not in {"true", "false"}:
            raise ValueError("PULSEGUARD_MCP_ALLOW_EXECUTION must be true or false")
        token = env.get("PULSEGUARD_MCP_API_TOKEN", "").strip()
        if "\r" in token or "\n" in token:
            raise ValueError("PULSEGUARD_MCP_API_TOKEN must be a single line")
        if token and url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Use HTTPS when sending PULSEGUARD_MCP_API_TOKEN to a remote service")
        return cls(
            base_url=base_url, project_ids=scope, allow_execution=execution == "true", api_token=token,
            request_timeout=_seconds(env, "PULSEGUARD_MCP_REQUEST_TIMEOUT", 30, 120),
            execution_timeout=_seconds(env, "PULSEGUARD_MCP_EXECUTION_TIMEOUT", 300, 1800),
        )


def _seconds(env: Mapping[str, str], key: str, default: int, maximum: int) -> int:
    try:
        value = int(env.get(key, str(default)))
        if not 1 <= value <= maximum:
            raise ValueError
        return value
    except ValueError:
        raise ValueError(f"{key} must be an integer between 1 and {maximum}") from None
