"""python examples/mcp_read.py /absolute/private/profile [READ_ONLY_TOOL]"""
import asyncio
import json
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    profile = Path(sys.argv[1]).resolve()
    params = StdioServerParameters(command=sys.executable, args=[str(profile / "server.py")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            definitions = (await session.list_tools()).tools
            print(json.dumps([t.name for t in definitions], ensure_ascii=False))
            if len(sys.argv) > 2:
                name = sys.argv[2]
                tool = next(t for t in definitions if t.name == name)
                if not tool.annotations or tool.annotations.readOnlyHint is not True:
                    raise SystemExit("This example only calls reviewed read-only tools.")
                result = await session.call_tool(name, {})
                for block in result.content:
                    if block.type == "text":
                        print(block.text)
                if result.isError:
                    raise SystemExit(1)

if __name__ == "__main__":
    asyncio.run(main())
