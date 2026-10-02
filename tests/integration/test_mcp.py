"""The capability catalog as an agent sees it: `rote mcp serve` over stdio, a real MCP client."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from rote.devserver import MockServer
from rote.registry.approvals import record_approval
from rote.registry.store import Workspace

from .conftest import BALANCE, FIXTURES, PASSWORD

pytestmark = pytest.mark.integration
SUB = "coreone.member.open_sub_account"


@pytest.fixture
def catalog_workspace(workspace: Workspace) -> Workspace:
    shutil.copy(FIXTURES / "artifacts" / "open_sub_account.authored.yaml", workspace.capability_path(SUB))
    record_approval(workspace.root, workspace.capability(BALANCE), reviewer="Reviewer One")
    return workspace  # open_sub_account stays unapproved


def server_params(workspace: Workspace, mock: MockServer) -> StdioServerParameters:
    env = {**os.environ, "ROTE_HOME": str(workspace.root), "ROTE_BASE_URL_HARBOR": mock.base_url("harbor"),
           "COREONE_USERNAME": "svc_rote", "COREONE_PASSWORD": PASSWORD, "ROTE_REDACTION_KEY": "test-key"}
    rote = str(Path(sys.executable).with_name("rote"))
    return StdioServerParameters(command=rote, args=["mcp", "serve", "--tenant", "harbor"], env=env,
                                 cwd=str(workspace.root))


async def session_calls(workspace: Workspace, mock: MockServer, calls: list[tuple[str, dict[str, Any]]]) -> tuple[
        list[Any], list[Any]]:
    async with stdio_client(server_params(workspace, mock)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        results = [await session.call_tool(name, arguments) for name, arguments in calls]
    return tools, results


async def test_agent_sees_only_approved_capabilities_and_gets_typed_results(
    catalog_workspace: Workspace, mock: MockServer
) -> None:
    name = "coreone__member__get_savings_balance"
    tools, (found, missing, unapproved) = await session_calls(catalog_workspace, mock, [
        (name, {"member_id": "100234"}),
        (name, {"member_id": "999999"}),
        ("coreone__member__open_sub_account", {"member_id": "100234"}),
    ])
    assert [t.name for t in tools] == [name]  # open_sub_account is not approved, so it is not offered
    tool = tools[0]
    assert tool.input_schema["required"] == ["member_id"] and tool.annotations.read_only_hint is True
    assert "MEMBER_NOT_FOUND" in tool.description

    assert found.is_error is False and found.structured_content["status"] == "succeeded"
    assert found.structured_content["outputs"] == {"savings_balance": {"amount": "1234.56", "currency": "USD"}}
    assert missing.is_error is False  # a business outcome is an answer, not an error
    assert missing.structured_content["outcome"]["code"] == "MEMBER_NOT_FOUND"
    assert unapproved.is_error is True


async def test_irreversible_tool_previews_then_commits_once(catalog_workspace: Workspace, mock: MockServer) -> None:
    record_approval(catalog_workspace.root, catalog_workspace.capability(SUB), reviewer="Reviewer One")
    name = "coreone__member__open_sub_account"
    inputs = {"member_id": "100517", "share_type": "Money Market", "nickname": "Rainy Day", "deposit": "40.00",
              "funding_suffix": "S10"}
    async with stdio_client(server_params(catalog_workspace, mock)) as (read, write), \
            ClientSession(read, write) as session:
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert tools[name].annotations.destructive_hint is True
        assert {"mode", "commit_token", "idempotency_key"} <= set(tools[name].input_schema["properties"])
        preview = await session.call_tool(name, {**inputs, "mode": "preview"})
        assert preview.structured_content["status"] == "preview" and mock.state()["harbor"]["commits"] == 0
        token = preview.structured_content["preview"]["commit_token"]
        commit = {**inputs, "mode": "commit", "commit_token": token, "idempotency_key": "agent-req-7"}
        first = await session.call_tool(name, commit)
        again = await session.call_tool(name, commit)
    assert first.structured_content["status"] == "succeeded"
    assert first.structured_content["commit_state"] == "committed"
    assert again.structured_content["idempotent_replay"] is True
    assert mock.state()["harbor"]["commits"] == 1
