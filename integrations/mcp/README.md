# PulseGuard MCP

Local stdio adapter for an existing PulseGuard service. Uses the official MCP Python
SDK and calls the HTTP API; it never opens the PulseGuard database or starts a probe
runner. Install with `uv sync --locked --project integrations/mcp` from the repository.

See [setup, tools, permissions and verification](../../docs/mcp.md).
