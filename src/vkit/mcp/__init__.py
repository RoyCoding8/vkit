"""The MCP tool surface for one project root.

`vkit mcp serve --project <root>` binds a `Server` to that root for the life of
the process. Nothing in this package can be asked for a different root.

## The SDK binding is written but not executed

`mcp` is not installed in this build, so the `ToolSurface` Protocol in
`_tools.py` is what a transport must implement, and `serve_stdio` below is the
adapter that has not been run. Plan 03's acceptance asks for an actual SDK
client over stdio; that check belongs to Plan 09, which has to verify it before
this surface can be called protocol-compliant. Nothing here reports a handshake,
a negotiated version or a tool listing that was observed on the wire, because
nothing observed one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ._tools import (
    BY_NAME,
    DEFAULT_LOG_BYTES,
    DEFAULT_PAGE,
    MAX_LOG_BYTES,
    MAX_PAGE,
    OPS,
    TOOL_NAMES,
    TOOLS,
    Server,
    ToolResult,
    ToolSpec,
    ToolSurface,
    tool_definitions,
)

__all__ = [
    "BY_NAME",
    "DEFAULT_LOG_BYTES",
    "DEFAULT_PAGE",
    "MAX_LOG_BYTES",
    "MAX_PAGE",
    "OPS",
    "TOOLS",
    "TOOL_NAMES",
    "Server",
    "ToolResult",
    "ToolSpec",
    "ToolSurface",
    "serve_stdio",
    "tool_definitions",
]


def serve_stdio(root: str | Path) -> int:
    """Serve the six tools over stdio using the official `mcp` SDK.

    UNEXECUTED. The SDK is not installed in this build, so this function has
    never run. It is written to the SDK's low-level server API and must be
    verified against a pinned release before Plan 03's protocol acceptance can
    be claimed.

    Two requirements it encodes, both protocol rather than preference. stdout
    carries protocol frames only, so the startup banner goes to stderr. And a
    tool call is answered with content plus an `isError` flag rather than an
    exception, because a refused request is a legitimate protocol answer and
    raising would read as a transport failure.
    """
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import ServerCapabilities, TextContent, Tool

    tools = Server(root)
    sdk = Server(tools.project.root.name)

    @sdk.list_tools()
    async def _list() -> list[Tool]:
        return [
            Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
                annotations=spec.annotations(),
            )
            for spec in tools.list_tools()
        ]

    @sdk.call_tool()
    async def _call(name: str, arguments: dict[str, Any]):
        import json

        result = tools.call_tool(name, arguments or {})
        body = json.dumps(result.content, indent=2, ensure_ascii=False)
        # The SDK's return type for a call_tool handler has changed across
        # releases (a bare list, or a CallToolResult carrying `isError`). Plan
        # 09 pins the version and settles this line against it; the failure
        # direction is deliberate, because a protocol error must be visible
        # rather than silently reported as a successful call.
        return [TextContent(type="text", text=body)], result.is_error

    async def _main() -> None:
        print(f"vkit mcp serving {tools.project.root}", file=sys.stderr, flush=True)
        async with stdio_server() as (read_stream, write_stream):
            await sdk.run(
                read_stream,
                write_stream,
                ServerCapabilities(tools={}),
            )

    import anyio

    anyio.run(_main)
    return 0


def schemas() -> dict[str, dict[str, Any]]:
    """The frozen input schemas, keyed by tool name. Tests assert on this."""
    return {spec.name: spec.input_schema for spec in TOOLS}
