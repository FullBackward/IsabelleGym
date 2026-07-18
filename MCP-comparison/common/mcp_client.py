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
    try:
        result = await session.call_tool(name, arguments)
        parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        text = "\n".join(parts) or "(no output)"
        # Surface the MCP-level error flag: servers like I/Q return failures as
        # isError=true with a JSON payload (e.g. {"text":"command write not
        # implemented"}); flattening that away made errors look like success.
        if getattr(result, "isError", False):
            return f"MCP tool error ({name}): {text}"
        return text
    except Exception as e:
        # The MCP library occasionally raises TypeError("catching classes
        # that do not inherit from BaseException") when the stdio transport
        # encounters a malformed or dropped message.  Return a structured
        # error string instead of letting the exception propagate.
        # NOTE: Exception, not BaseException — swallowing CancelledError here
        # broke asyncio.wait_for tool timeouts (the cancel never landed, and
        # the MCP session was left desynchronized).
        return f"MCP tool error ({name}): {type(e).__name__}: {str(e)}"


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
