"""MCP stdio client helpers used by all three runner scripts."""
from __future__ import annotations

import asyncio
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
            init_result = await session.initialize()
            # Vendor guidance shipped via the MCP initialize handshake (e.g.
            # Isabelle-MCP's instructions.py): attach it so runners can forward
            # it to the model as the "guided" prompt variant.
            session.vendor_instructions = getattr(init_result, "instructions", None)
            yield session


@asynccontextmanager
async def mcp_session_startup_retry(
    cfg: MCPServerConfig,
    retries: int = 2,
    delay_s: float = 3.0,
    on_retry: Any = None,
) -> AsyncIterator[ClientSession]:
    """mcp_session with startup-only retries.

    A fresh stdio MCP server occasionally fails to come up (e.g. right after
    the previous attempt's teardown); the nested asynccontextmanagers then
    surface only "generator didn't yield" with no cause and the attempt dies
    at setup. Retry the STARTUP only — exceptions from the agent-loop body
    propagate immediately without retrying.
    """
    attempt = 0
    while True:
        cm = mcp_session(cfg)
        try:
            session = await cm.__aenter__()
        except Exception:
            if attempt + 1 >= retries:
                raise
            attempt += 1
            if on_retry is not None:
                on_retry(attempt)
            await asyncio.sleep(delay_s)
            continue
        try:
            yield session
        finally:
            await cm.__aexit__(None, None, None)
        return


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
