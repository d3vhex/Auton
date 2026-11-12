"""End-to-end test over the real MCP stdio transport."""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = pathlib.Path(__file__).resolve().parent.parent / "agent_server.py"

EXPECTED = {
    "list_directory", "read_file", "write_file", "edit_file", "apply_patch",
    "grep", "glob", "bash", "job", "run_tests", "git_status", "git_diff",
    "fs", "todo_write",
    # Kubernetes control
    "k8s_get", "k8s_describe", "k8s_logs", "k8s_snapshot", "k8s_diagnose",
    "k8s_events", "k8s_wait", "k8s_apply", "k8s_patch", "k8s_scale",
    "k8s_rollout", "k8s_delete", "k8s_exec",
    # n8n control
    "n8n_workflows", "n8n_activate", "n8n_executions", "n8n_webhook",
    "n8n_create_agent_pipeline", "n8n_create_ops_pipeline",
}


def test_server_lists_tools_and_serves_calls(ws):
    async def go():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env=os.environ.copy(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {t.name for t in listed.tools}
                assert EXPECTED <= names, EXPECTED - names
                assert all((t.description or "").strip() for t in listed.tools)

                result = await session.call_tool(
                    "write_file", {"path": "via_mcp.txt", "content": "hello\n"}
                )
                text = "\n".join(
                    block.text for block in result.content if hasattr(block, "text")
                )
                assert "Wrote" in text

                read_back = await session.call_tool(
                    "read_file", {"path": "via_mcp.txt"}
                )
                text = "\n".join(
                    block.text for block in read_back.content
                    if hasattr(block, "text")
                )
                assert "hello" in text

    asyncio.run(go())
