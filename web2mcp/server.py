import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ToolAnnotations

from .client import CallError
from .product import Product


async def run(profile):
    product = Product(profile)
    server = Server("web2mcp-local")

    @server.list_tools()
    async def list_tools():
        return [Tool(name=op['name'], description=op['description'], inputSchema=op['input_schema'],
                     annotations=ToolAnnotations(readOnlyHint=op['read_only'], destructiveHint=not op['read_only'], openWorldHint=True))
                for op in product.tools()]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            value = await asyncio.to_thread(product.call, name, arguments)
            return [TextContent(type="text", text=json.dumps(value, ensure_ascii=False))]
        except (CallError, FileNotFoundError) as e:
            # SDK converts an exception into an isError tool result.
            raise ValueError(json.dumps(e.as_dict(), ensure_ascii=False) if isinstance(e, CallError) else 'Preview file not found') from None

    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        product.close()


def serve(profile):
    asyncio.run(run(profile))
