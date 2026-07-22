"""Entry points.

    python -m a2k.api          # REST transport (uvicorn)
    python -m a2k.mcp_server   # MCP transport (stdio)
"""

from __future__ import annotations


def run_api() -> None:
    import uvicorn

    from .config import config

    uvicorn.run("a2k.api.rest:app", host=config.host, port=config.port, reload=False)


def run_mcp() -> None:
    from .mcp_server.server import main

    main()
