#!/usr/bin/env python3
"""Live MCP demo: act as an MCP *client*, launch the Attestari MCP server over
stdio, discover its tools, and call them — exactly what an AI agent does.

    .venv/bin/python examples/mcp_client_demo.py
"""

from __future__ import annotations

import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")


def _result_text(result) -> str:
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured:
        return str(structured)
    return " ".join(getattr(c, "text", str(c)) for c in result.content)


async def main() -> None:
    env = {**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")}
    env.pop("ATTESTARI_DATABASE_URL", None)  # use the in-memory engine for the demo
    env.pop("ATTESTARI_KEK", None)

    params = StdioServerParameters(command=PY, args=["-m", "attestari.mcp"], env=env)

    # The client launches the server as a subprocess and talks to it over stdio.
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print("=== tools the agent discovers ===")
            tools = await session.list_tools()
            for t in tools.tools:
                print(f"  - {t.name}: {t.description}")

            print("\n=== calling tools ===")
            r1 = await session.call_tool(
                "add_memory",
                {"text": "Hi, my name is Dana. I live in Delhi.", "subject_id": "u1"},
            )
            print("add_memory    ->", _result_text(r1))

            r2 = await session.call_tool(
                "search_memory",
                {"query": "where does the user live", "subject_id": "u1"},
            )
            print("search_memory ->", _result_text(r2))


if __name__ == "__main__":
    asyncio.run(main())
