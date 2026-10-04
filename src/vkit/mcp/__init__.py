"""The MCP tool surface for one project root.

`vkit mcp serve --project <root>` binds a `Server` to that root for the life of
the process. Nothing in this package can be asked for a different root.

## What the transport is pinned to

`mcp` is an optional dependency, installed by `pip install 'vkit[mcp]'`, and the
binding below is written against the 2.x server API. That line is a rewrite, not
an increment: handlers are constructor arguments rather than decorators, `run`
takes an `InitializationOptions` the SDK builds from the registered handlers, and
a tool call returns a `CallToolResult` carrying `isError`. A bump inside 2.x has
not been verified here, so
`tests/test_mcp_stdio.py` drives the real subprocess over real JSON-RPC frames,
which puts a contract change where a test fails rather than at a user's host.

Two requirements the binding encodes, both protocol rather than preference. stdout
carries protocol frames only, so every diagnostic goes to stderr. And a refused
request is answered with content plus `isError` rather than an exception, because
a refusal is a legitimate protocol answer. An *unexpected* exception is the
opposite case and is deliberately not caught: the SDK reports it as a protocol
error, keeps serving, and writes the traceback to stderr, so an internal fault can
never come back wearing the shape of a verdict.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .. import __version__
from ._tools import (
    BY_NAME,
    DEFAULT_LOG_BYTES,
    DEFAULT_PAGE,
    MAX_LOG_BYTES,
    MAX_PAGE,
    OPS,
    TOOLS,
    TOOL_NAMES,
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
    "INSTRUCTIONS",
    "MCPUnavailable",
    "OPS",
    "TOOLS",
    "TOOL_NAMES",
    "Server",
    "ToolResult",
    "ToolSpec",
    "ToolSurface",
    "protocol_tools",
    "serve_stdio",
    "tool_definitions",
]

INSTRUCTIONS = (
    "Verification evidence for one Git repository, already bound to this process. "
    "Start with project_inspect to learn which checks are registered and runnable, "
    "then task_begin to open an attempt, check_start to run registered checks, "
    "run_get for a run's recorded outcome, and task_finalize for the verdict. "
    "Check ids are manifest ids; there is no way to pass a command."
)


class MCPUnavailable(Exception):
    """The `mcp` SDK is not installed in this environment.

    A missing optional dependency, not a broken project. It is raised rather than
    printed because stdout is the protocol channel and there is no client to read
    it, so the caller owns the exit code.
    """


def _sdk() -> Any:
    """The SDK's pieces, or a refusal naming the fix.

    Imported at call time rather than at module scope so every other vkit command
    keeps working in an environment that never installed the transport. A missing
    optional dependency must not take down the CLI, the console or the hook.
    """
    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server as SdkServer
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise MCPUnavailable(
            f"the MCP SDK is not available in this environment ({exc}); "
            "install it with: pip install 'vkit[mcp]'"
        ) from exc
    return types, SdkServer, stdio_server


def protocol_tools(tools: Server) -> list[Any]:
    """The six tool specs as the protocol's `Tool` models.

    Built from the same table `Server.list_tools` publishes, so a tool cannot be
    callable without being listed. The annotations go through the SDK's model
    rather than being passed as a dict, because the wire names are camelCase and
    the field names are not, and a dict lands as extra unvalidated fields.
    """
    types, _SdkServer, _stdio_server = _sdk()
    return [
        types.Tool(
            name=spec.name,
            description=spec.description,
            inputSchema=spec.input_schema,
            annotations=types.ToolAnnotations.model_validate(spec.annotations()),
        )
        for spec in tools.list_tools()
    ]


def _as_call_result(types: Any, result: ToolResult) -> Any:
    """One `ToolResult` as the protocol's answer to a tool call.

    `is_error` becomes `isError`, and that mapping is the load-bearing part. A
    check that FAILED is a recorded verdict, so it arrives with `isError` false
    and `"result": "FAIL"` in the body; a refused request arrives with `isError`
    true. Collapsing the two would let a stored FAIL read as a failed call, and
    neither verdict is recomputed here.
    """
    body = json.dumps(result.content, indent=2, ensure_ascii=False)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=body)],
        isError=result.is_error,
    )


def serve_stdio(root: str | Path) -> int:
    """Serve the six tools over stdio using the official `mcp` SDK.

    Returns 0 once the client has disconnected. Raises `MCPUnavailable` when the
    SDK is not installed, so the caller owns the exit code.
    """
    import anyio

    types, SdkServer, stdio_server = _sdk()
    tools = Server(root)

    async def _list_tools(_ctx: Any, _params: Any) -> Any:
        return types.ListToolsResult(tools=protocol_tools(tools))

    async def _call_tool(_ctx: Any, params: Any) -> Any:
        return _as_call_result(types, tools.call_tool(params.name, params.arguments))

    sdk = SdkServer(
        name=tools.project.root.name,
        version=__version__,
        instructions=INSTRUCTIONS,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )

    async def _main() -> None:
        print(f"vkit mcp serving {tools.project.root}", file=sys.stderr, flush=True)
        async with stdio_server() as (read_stream, write_stream):
            await sdk.run(read_stream, write_stream, sdk.create_initialization_options())

    anyio.run(_main)
    return 0


def schemas() -> dict[str, dict[str, Any]]:
    """The frozen input schemas, keyed by tool name. Tests assert on this."""
    return {spec.name: spec.input_schema for spec in TOOLS}
