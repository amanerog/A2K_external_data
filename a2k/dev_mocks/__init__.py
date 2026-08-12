"""Local-only stand-ins for CALA's and Sayari's own MCP servers.

Nothing here ships as part of a2k-box. It exists to validate one specific
architectural question before real vendor MCP docs/credentials exist: can
a2k-box's engine (gateway/engine.py) consume CALA/Sayari data over MCP
instead of REST, with zero changes to the engine, audit, or
conflict-detection code? See `run_local_test.py` for the answer.

Once CALA/Sayari hand over real MCP endpoints, `mcp_client_adapter.py`'s
`McpClientAdapter` is the shape to adapt (swap the mock tool names/response
schema for the real ones) -- the rest of this package (the two mock
servers) gets deleted.
"""
