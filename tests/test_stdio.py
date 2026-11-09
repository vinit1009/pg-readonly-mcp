"""End to end over stdio, the transport Claude Desktop uses: spawn the server, list tools, call them."""
import asyncio
import json
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


def test_stdio_session(shop, tmp_path):
    audit = tmp_path / "audit.jsonl"
    params = StdioServerParameters(command=sys.executable, args=["-m", "pg_readonly_mcp"], env={
        "MCP_PG_DSN": shop["readonly"], "MCP_PG_ALLOWED_TABLES": "public.orders, public.customers",
        "MCP_PG_AUDIT_LOG": str(audit), "MCP_PG_ROW_LIMIT": "10"})

    async def session():
        async with Client(params) as client:
            tools = (await client.list_tools()).tools
            assert sorted(t.name for t in tools) == ["describe_table", "explain_query", "list_tables", "run_query"]
            assert all(t.annotations and t.annotations.read_only_hint for t in tools)

            listed = await client.call_tool("list_tables", {})
            assert not listed.is_error
            assert [t["table"] for t in listed.structured_content["tables"]] == ["public.customers", "public.orders"]

            ok = await client.call_tool("run_query", {"sql": "SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY status"})
            assert not ok.is_error
            assert ok.structured_content["rows"] == [["paid", 600], ["pending", 200], ["refunded", 200]]

            capped = await client.call_tool("run_query", {"sql": "SELECT id FROM orders"})
            assert capped.structured_content["row_count"] == 10 and capped.structured_content["truncated"]

            refused = await client.call_tool("run_query", {"sql": "SELECT secret FROM api_keys"})
            assert refused.is_error and "allow-list" in refused.content[0].text

    asyncio.run(session())
    statuses = [json.loads(line)["status"] for line in audit.read_text().splitlines()]
    assert statuses == ["ok", "ok", "ok", "rejected"]
