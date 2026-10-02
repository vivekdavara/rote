"""The capability catalog over MCP: approved capabilities become tools an agent can call.

One server instance serves one tenant (least privilege: an agent working for
Harbor never sees Summit's tools). Each *approved* capability becomes a tool:

* name: the capability id with dots replaced (``coreone__member__get_savings_balance``)
* input schema: the contract's inputs; irreversible capabilities add ``mode``
  (preview | commit), ``commit_token`` and ``idempotency_key``
* description: the summary, the side effects, and the business outcomes the
  caller must handle
* annotations: read-only, or destructive for irreversible capabilities

A call is a deterministic replay. No model is involved: the agent decides
*what* to do, and rote does it the way a reviewer approved. Business outcomes
are not errors (``is_error`` is false for ``business_outcome``): "no such
member" is an answer.

    rote mcp serve --tenant harbor        # stdio; point an MCP client at it
"""

from __future__ import annotations

import json
from typing import Any

import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from playwright.async_api import Browser, async_playwright

from rote.registry.approvals import find_approval, load_approvals
from rote.registry.store import Workspace
from rote.replay.engine import ReplayOptions, replay
from rote.schema.capability import Capability
from rote.schema.overlay import OverlayError
from rote.schema.result import RunResult

_JSON_TYPES = {"string": "string", "integer": "integer", "decimal": "string", "boolean": "boolean"}


def tool_name(capability_id: str) -> str:
    return capability_id.replace(".", "__")


def input_schema(capability: Capability) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, spec in capability.inputs.items():
        prop: dict[str, Any] = {"type": _JSON_TYPES[spec.type]}
        if spec.description:
            prop["description"] = spec.description
        if spec.pattern:
            prop["pattern"] = spec.pattern
        if spec.enum:
            prop["enum"] = spec.enum
        if spec.type == "decimal":
            prop["pattern"] = r"^-?\d+(\.\d+)?$"
        properties[name] = prop
    required = list(capability.inputs)
    if capability.side_effects == "irreversible":
        properties["mode"] = {"type": "string", "enum": ["preview", "commit"], "default": "preview",
                              "description": "preview first; commit only with the token the preview returned"}
        properties["commit_token"] = {"type": "string", "description": "From a preview of exactly these inputs."}
        properties["idempotency_key"] = {"type": "string",
                                         "description": "Unique per intended commit; a repeat returns the result."}
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def output_schema(capability: Capability) -> dict[str, Any]:
    statuses = ["succeeded", "business_outcome", "failed", "rejected", "preview"]
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": statuses},
            "outputs": {"type": ["object", "null"],
                        "properties": {name: {"description": out.description or out.type}
                                       for name, out in capability.outputs.items()}},
            "outcome": {"type": ["object", "null"], "description": "code is one of: "
                        + ", ".join(capability.outcomes) if capability.outcomes else "none declared"},
            "preview": {"type": ["object", "null"]},
            "error": {"type": ["object", "null"]},
            "commit_state": {"type": "string", "enum": ["none", "committed", "unknown"]},
        },
        "required": ["status"],
    }


def description(capability: Capability) -> str:
    parts = [capability.summary]
    if capability.side_effects == "irreversible":
        parts.append("IRREVERSIBLE: call with mode=preview first and show the returned review values to the "
                     "member; then call with mode=commit, the commit_token, and a fresh idempotency_key.")
    else:
        parts.append("Read-only.")
    if capability.outcomes:
        outcomes = "; ".join(f"{code} ({o.description or 'declared outcome'})" for code, o in capability.outcomes.items())
        parts.append(f"Business outcomes to handle (status=business_outcome, not an error): {outcomes}.")
    parts.append("Runs a reviewed, approved recording deterministically; no model is involved.")
    return " ".join(parts)


class Catalog:
    def __init__(self, workspace: Workspace, tenant: str) -> None:
        self.workspace = workspace
        self.tenant = tenant
        self.browser: Browser | None = None

    def approved(self) -> list[Capability]:
        found: list[Capability] = []
        for path in sorted((self.workspace.root / "capabilities").glob("*/*.yaml")):
            capability_id = f"{path.parent.name}.{path.stem}"
            try:
                _effective, base, overlay = self.workspace.effective(capability_id, self.tenant)
            except (OverlayError, ValueError):
                continue
            approval = find_approval(load_approvals(self.workspace.root, base.id), base, tenant=self.tenant,
                                     overlay_hash=overlay.content_hash() if overlay else None)
            if approval is not None:
                found.append(base)
        return found

    def tools(self) -> list[types.Tool]:
        return [
            types.Tool(
                name=tool_name(c.id),
                description=description(c),
                input_schema=input_schema(c),
                output_schema=output_schema(c),
                annotations=types.ToolAnnotations(
                    title=c.summary,
                    read_only_hint=c.side_effects == "none",
                    destructive_hint=c.side_effects == "irreversible",
                    idempotent_hint=c.side_effects == "none",
                ),
            )
            for c in self.approved()
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> RunResult:
        capability = next((c for c in self.approved() if tool_name(c.id) == name), None)
        if capability is None:
            raise LookupError(f"no approved capability {name!r} for tenant {self.tenant}")
        arguments = dict(arguments)
        mode = arguments.pop("mode", None)
        token = arguments.pop("commit_token", None)
        key = arguments.pop("idempotency_key", None)
        if capability.side_effects == "irreversible":
            mode = "commit" if mode == "commit" else "preview"
        else:
            mode = "run"
        options = ReplayOptions(mode=mode, commit_token=token, idempotency_key=key)  # type: ignore[arg-type]
        assert self.browser is not None
        return await replay(self.workspace, capability.id, self.tenant, arguments, options, browser=self.browser)


def summarize(result: RunResult) -> str:
    if result.status == "succeeded":
        return f"succeeded: {json.dumps(result.outputs)}"
    if result.status == "preview" and result.preview:
        return f"preview (nothing committed): {json.dumps(result.preview.values)}. Commit token returned."
    if result.status == "business_outcome" and result.outcome:
        return f"business outcome {result.outcome.code}: {result.outcome.message or ''} {result.outcome.messages}"
    error = result.error
    return f"{result.status}: {error.code if error else ''} {error.message if error else ''}"


async def serve(workspace: Workspace, tenant: str) -> None:
    catalog = Catalog(workspace, tenant)

    async def list_tools(_ctx: Any, _params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=catalog.tools())

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            result = await catalog.call(params.name, params.arguments or {})
        except LookupError as exc:
            return types.CallToolResult(content=[types.TextContent(text=str(exc))], is_error=True)
        structured = result.model_dump(mode="json", exclude_none=True)
        return types.CallToolResult(
            content=[types.TextContent(text=summarize(result))],
            structured_content=structured,
            is_error=result.status in ("failed", "rejected"),
        )

    server: Server[Any] = Server("rote", version="0.1.0", instructions=(
        f"Capabilities for tenant {tenant}. Each tool replays an approved recording of a back-office task. "
        "Treat business_outcome results as answers, not failures. Irreversible tools need a preview, then a "
        "commit with the preview's token and an idempotency key."),
        on_list_tools=list_tools, on_call_tool=call_tool)
    async with async_playwright() as pw:
        catalog.browser = await pw.chromium.launch(headless=True)
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            await catalog.browser.close()
