"""MCP tools for one project root. Agents ask what is known, run registered checks, and read the gate."""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Callable

from jsonschema import ValidationError, validate

from ..categories import CATEGORIES, INFRASTRUCTURE_TOOLS
from .. import __version__, evidence, jobs, query
from ..manifest import ManifestError
from ..paths import Project, open_project
from ..settings import load_settings

MAX_WAIT_SECONDS = 60
INSTRUCTIONS = (
    "Call help for enabled categories, help with category or query for tool summaries, "
    "and help with name for one exact input contract. In compact mode, use invoke with that name and arguments. "
    "Verification evidence concerns one Git repository. Registered checks come from the manifest; "
    "no tool accepts an arbitrary command, and only a human can accept a check definition. "
    "Each check describes its scope and possible outcomes. Results record which accepted checker ran, its "
    "inputs, artifacts, and assurance level; use them only for the claim they establish."
)


class MCPUnavailable(Exception):
    """The `mcp` SDK is not installed."""


class Refusal(Exception):
    """A request this server will not honour; returned to the client as an error result."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    handler: Callable[["Server", dict[str, Any]], Any]
    read_only: bool = True
    required: tuple[str, ...] = ()
    category: str | None = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.name == "help" and self.category is None:
            return
        if self.name not in INFRASTRUCTURE_TOOLS and self.category in CATEGORIES:
            return
        raise ValueError(f"invalid tool category {self.category!r}")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": list(self.required),
                "additionalProperties": False}


def _check(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise Refusal("arguments must be an object")
    unknown = sorted(set(arguments) - set(schema["properties"]))
    if unknown:
        raise Refusal(f"unknown argument(s): {', '.join(unknown)}")
    try:
        validate(arguments, schema)
    except ValidationError as exc:
        raise Refusal(exc.message) from exc


@dataclass
class Server:
    root: Path
    exposure: str = "compact"
    categories: tuple[str, ...] | None = None

    @classmethod
    def for_mcp(cls, root: Path, exposure: str | None = None,
                categories: list[str] | None = None) -> "Server":
        server = cls(root)
        settings = load_settings(server.project)
        mode = settings.mcp_exposure if exposure is None else exposure
        scope = settings.mcp_categories if categories is None else (
            None if categories == ["all"] else tuple(categories))
        if mode not in ("compact", "named") or (scope is not None and set(scope) - CATEGORIES.keys()):
            raise ValueError("invalid MCP exposure or category scope")
        server.exposure, server.categories = mode, scope
        return server

    @cached_property
    def project(self) -> Project:
        return open_project(self.root)

    def context(self) -> query.Context:
        return query.open_context(self.root)

    def tools(self) -> tuple[Tool, ...]:
        settings = load_settings(self.project)
        return tuple(tool for tool in BY_NAME.values() if settings.allows(tool.name, tool.category, self.categories))

    def definitions(self) -> list[dict[str, Any]]:
        if self.exposure == "named":
            return tool_definitions(self.tools())
        return [*tool_definitions((BY_NAME["help"],)), _INVOKE]

    def call_mcp(self, name: str, arguments: dict[str, Any] | None) -> tuple[Any, bool]:
        if self.exposure == "compact" and name == "invoke":
            try:
                arguments = {} if arguments is None else arguments
                _check(arguments, _INVOKE["inputSchema"])
                if arguments["name"] in INFRASTRUCTURE_TOOLS:
                    raise Refusal("unknown or unavailable tool")
            except Refusal as exc:
                return {"error": str(exc)}, True
            return self.call(arguments["name"], arguments["arguments"])
        if self.exposure == "compact" and name != "help":
            return {"error": "unknown or unavailable tool"}, True
        return self.call(name, arguments)

    def call(self, name: str, arguments: dict[str, Any] | None) -> tuple[Any, bool]:
        arguments = {} if arguments is None else arguments
        try:
            tool = BY_NAME.get(name)
            if tool is None or not load_settings(self.project).allows(name, tool.category, self.categories):
                return {"error": "unknown or unavailable tool"}, True
            _check(arguments, tool.input_schema)
            return tool.handler(self, arguments), False
        except (OSError, Refusal, ManifestError, ValueError) as exc:
            return {"error": str(exc)}, True


def _status(server: Server, args: dict[str, Any]) -> Any:
    return query.status(server.context(), args.get("paths", ()))


def _gate(server: Server, args: dict[str, Any]) -> Any:
    return query.status(server.context())["gate"]


def _features(server: Server, args: dict[str, Any]) -> Any:
    report = query.status(server.context())
    if report["feature_error"]:
        raise Refusal(report["feature_error"])
    words = str(args.get("query", "")).lower().split()
    return {"features": [f for f in report["features"]
                         if all(w in json.dumps(f).lower() for w in words)]}


def _help(server: Server, args: dict[str, Any]) -> Any:
    tools = tuple(tool for tool in server.tools() if tool.name != "help")
    if "name" in args:
        if len(args) != 1:
            raise Refusal("name cannot be combined with other help selectors")
        tool = next((tool for tool in tools if tool.name == args["name"]), None)
        if tool is None:
            raise Refusal("unknown or unavailable tool")
        return {"contract_version": 2, "tools": [{**tool_definitions((tool,))[0],
                "category": tool.category, "contract_version": 1}]}
    counts = Counter(tool.category for tool in tools)
    if "category" in args and not counts.get(args["category"]):
        raise Refusal("unknown or unavailable category")
    if args.get("all") and "category" in args:
        raise Refusal("all cannot be combined with category")
    if not any(key in args for key in ("category", "query", "all")):
        if args:
            raise Refusal("pagination requires a category, query, or all selector")
        return {"contract_version": 2, "categories": [
            {"id": category, "label": label, "count": counts[category]}
            for category, label in CATEGORIES.items() if counts[category]]}
    words = args.get("query", "").lower().split()
    matches = []
    for tool in tools:
        if "category" in args and tool.category != args["category"]:
            continue
        if words:
            text = (tool.name + " " + tool.description + " " + json.dumps(tool.input_schema)).lower()
            if not all(word in text for word in words):
                continue
        matches.append(tool)
    matches.sort(key=lambda tool: (tool.category, tool.name))
    offset, limit = args.get("offset", 0), args.get("limit", 10)
    page = matches[offset:offset + limit]
    return {"contract_version": 2, "tools": [
        {"name": tool.name, "category": tool.category, "description": tool.description,
         "annotations": {"readOnlyHint": tool.read_only}} for tool in page],
        "total": len(matches), "next_offset": offset + limit if offset + limit < len(matches) else None}


def _summary_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("job_id", "run_id", "check_id", "state", "outcome",
                                            "assurance", "claim_outcome", "receipt")}


def _check_run(server: Server, args: dict[str, Any]) -> Any:
    ctx = server.context()
    manifest = ctx.require_manifest()
    report = query.status(ctx)
    active = {c["id"]: c["running"][0] for c in report["checks"] if c["running"]}
    if args.get("needed"):
        wanted = report["needs_run"]
    else:
        wanted = args.get("check_ids") or []
        if not wanted:
            raise Refusal("pass check_ids, or needed=true to run every stale or missing check")
        for check_id in wanted:
            manifest.require(check_id)
    run_ids = []
    for check_id in wanted:
        run_id = active.get(check_id)
        if run_id is None:
            run_id = jobs.submit(ctx.project, manifest, check_id, store=ctx.store)["run_id"]
        run_ids.append(run_id)
    deadline = time.monotonic() + min(max(int(args.get("wait_seconds", 20)), 0), MAX_WAIT_SECONDS)
    records = []
    for run_id in run_ids:
        records.append(jobs.wait(ctx.store, run_id, max(0, deadline - time.monotonic())))
    summaries = [_summary_record(record) for record in records]
    return {"runs": summaries,
            "note": "poll run_get for runs still queued or running" if any(
                record["state"] in ("queued", "running") for record in records) else ""}


def _run_get(server: Server, args: dict[str, Any]) -> Any:
    if "run_id" not in args:
        raise Refusal("run_id is required")
    view = query.run_view(server.context(), args["run_id"], log=args.get("log", "stdout"),
                          offset=args.get("log_offset", 0), limit=min(args.get("log_limit", 16_000), 64_000))
    if view is None:
        raise Refusal(f"no run {args['run_id']!r}")
    return view


def _run_wait(server: Server, args: dict[str, Any]) -> Any:
    ctx = server.context()
    timeout = min(args.get("wait_seconds", 20), MAX_WAIT_SECONDS)
    jobs.wait(ctx.store, args["run_id"], timeout)
    view = query.run_view(ctx, args["run_id"])
    if view is None:
        raise Refusal(f"no run {args['run_id']!r}")
    return view


def _evidence_export(server: Server, args: dict[str, Any]) -> Any:
    ctx = server.context()
    return evidence.export(ctx.store, args["run_id"], Path(args["output"]))


def _evidence_replay(server: Server, args: dict[str, Any]) -> Any:
    ctx = server.context()
    record = jobs.replay(ctx.project, ctx.require_manifest(), Path(args["bundle"]), store=ctx.store)
    if "wait_seconds" in args:
        record = jobs.wait(ctx.store, record["run_id"], min(args["wait_seconds"], MAX_WAIT_SECONDS))
    return _summary_record(record)


def _propose(server: Server, args: dict[str, Any]) -> Any:
    from ..proposals import ProposalError, submit

    ctx = server.context()
    try:
        proposal = submit(ctx.project, ctx.store, args)
    except ProposalError as exc:
        raise Refusal(str(exc)) from exc
    return {"proposal": proposal.digest, "next": f"a human reviews it with `vkit proposals` and applies it with "
                                                  f"`vkit accept --proposal {proposal.digest[:12]}`"}


def _run_cancel(server: Server, args: dict[str, Any]) -> Any:
    if "run_id" not in args:
        raise Refusal("run_id is required")
    ctx = server.context()
    record = ctx.store.record(args["run_id"])
    if record is None:
        raise Refusal(f"no run {args['run_id']!r}")
    if record["state"] not in ("queued", "running"):
        return {"run_id": args["run_id"], "cancelled": False, "state": record["state"]}
    ctx.store.request_cancel(args["run_id"])
    return {"run_id": args["run_id"], "cancelled": True}


_STR = {"type": "string"}
_PATHS = {"type": "array", "items": _STR, "description": "repository-relative paths"}
_BASE_TOOLS = (
    Tool("help", "Discover enabled repository tools. Use query for summaries, all=true for the paged list, "
         "or name for one exact input contract before invocation.",
         {"query": _STR, "category": _STR, "name": _STR, "all": {"type": "boolean"},
          "offset": {"type": "integer", "minimum": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10}}, _help, category=None),
    Tool("status", "Freshness of every registered check (fresh_pass, fresh_fail, fresh_blocked, stale, missing, "
         "not_approved), what each evidence category establishes and does not, the gate, and the features. Pass "
         "paths to see only the checks and features that read them, and which paths no check reads.",
         {"paths": _PATHS}, _status, category="repository"),
    Tool("features", "The feature map: what users can do, how they reach it, which checks cover it, and known "
         "gaps. Filter with a free-text query.", {"query": _STR}, _features, category="repository"),
    Tool("check_run", "Submit durable jobs for registered checks by id, or needed=true for every stale or missing "
         "one. Waits up to wait_seconds (max 60) and returns each run's state and outcome. A PASS is only the "
         "accepted checker's claim for its recorded inputs and stated assurance.",
         {"check_ids": {"type": "array", "items": _STR}, "needed": {"type": "boolean"},
          "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS}},
         _check_run, read_only=False, category="repository"),
    Tool("run_get", "One run: state, outcome, obligations, inputs, and a window of its stdout or stderr.",
         {"run_id": _STR, "log": {"type": "string", "enum": ["stdout", "stderr"]},
          "log_offset": {"type": "integer", "description": "negative counts from the end"}, "log_limit": {"type": "integer"}}, _run_get, category="repository"),
    Tool("run_cancel", "Stop a running check and everything it started.", {"run_id": _STR}, _run_cancel,
         read_only=False, category="repository"),
    Tool("run_wait", "Wait up to wait_seconds (max 60) for a durable job to finish, then return its run record "
         "and stdout window. This reports the checker result and its stated assurance; it does not independently "
         "upgrade that claim.", {"run_id": _STR, "wait_seconds": {"type": "integer", "minimum": 0,
          "maximum": MAX_WAIT_SECONDS}}, _run_wait, required=("run_id",), category="repository"),
    Tool("evidence_export", "Export an intact completed run as an evidence bundle. Replay requires matching current "
         "inputs and accepted tool identity. The receipt binds the "
         "recorded inputs, checker identity, outcome, assurance, and artifact digests.",
         {"run_id": _STR, "output": {"type": "string", "description": "destination bundle directory"}},
         _evidence_export, read_only=False, required=("run_id", "output"), category="repository"),
    Tool("evidence_replay", "Submit a durable replay job for a bundle using the currently accepted checker. "
         "Imported PASS flags are not trusted; the checker runs again. Optionally wait for its result.",
         {"bundle": {"type": "string", "description": "source bundle directory"},
          "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS}},
         _evidence_replay, read_only=False, required=("bundle",), category="repository"),
    Tool("propose", "Suggest new or changed checks (manifest v3 entries), feature map entries, and new files they "
         "need, with a rationale. Nothing runs or changes until a human accepts it. Use it to move a lesson up "
         "the trust ladder: a recurring mistake becomes a static rule or a scenario check.",
         {"checks": {"type": "array", "items": {"type": "object"}}, "features": {"type": "array",
          "items": {"type": "object"}}, "files": {"type": "object", "description": "new path -> text"},
          "rationale": _STR}, _propose, read_only=False, category="repository"),
    Tool("gate", "READY only when every registered check passed against the current inputs; REJECTED when one "
         "failed; otherwise BLOCKED with the checks that lack fresh evidence.", {}, _gate, category="repository"),
)
_INVOKE = {
    "name": "invoke", "description": "Run one enabled tool using its discovered name and arguments. "
    "Validates the installed input contract. Some tools submit durable checks, export evidence, or stage proposals.",
    "inputSchema": {"type": "object", "properties": {"name": _STR, "arguments": {"type": "object"}},
                    "required": ["name", "arguments"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False},
}
TOOLS = _BASE_TOOLS
if len({tool.name for tool in TOOLS}) != len(TOOLS):
    raise ValueError("duplicate MCP tool name")
BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_definitions(tools: tuple[Tool, ...] = TOOLS) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema,
             "annotations": {"readOnlyHint": t.read_only}} for t in tools]


def serve_stdio(root: str | Path, *, exposure: str | None = None,
                categories: list[str] | None = None) -> int:
    try:
        import anyio
        import mcp.types as types
        from mcp.server.lowlevel import Server as SdkServer
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise MCPUnavailable(f"the MCP SDK is not installed ({exc}); install vkit[mcp]") from exc
    server = Server.for_mcp(Path(root), exposure, categories)

    async def list_tools(_ctx: Any, _params: Any) -> Any:
        return types.ListToolsResult(tools=[
            types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"],
                       annotations=types.ToolAnnotations(readOnlyHint=t["annotations"]["readOnlyHint"]))
            for t in server.definitions()])

    async def call_tool(_ctx: Any, params: Any) -> Any:
        body, is_error = await anyio.to_thread.run_sync(server.call_mcp, params.name, params.arguments)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body, indent=2))],
                                    isError=is_error)

    sdk = SdkServer(name=Path(root).name, version=__version__, instructions=INSTRUCTIONS,
                    on_list_tools=list_tools, on_call_tool=call_tool)

    async def main() -> None:
        print(f"vkit mcp serving {root}", file=sys.stderr, flush=True)
        async with stdio_server() as (read_stream, write_stream):
            await sdk.run(read_stream, write_stream, sdk.create_initialization_options())

    anyio.run(main)
    return 0
