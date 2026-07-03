"""MCP stdio client helpers used by all three runner scripts."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .config import MCPServerConfig


@asynccontextmanager
async def mcp_session(cfg: MCPServerConfig) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=cfg.command[0],
        args=cfg.command[1:],
        env=cfg.env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> str:
    result = await session.call_tool(name, arguments)
    parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    return "\n".join(parts) or "(no output)"


async def list_tools(session: ClientSession) -> list[dict[str, Any]]:
    tools = await session.list_tools()
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "parameters": t.inputSchema,
        }
        for t in tools.tools
    ]
