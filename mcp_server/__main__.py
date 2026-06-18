"""MCP server entry point: `python -m mcp_server`.

Initializes ChromaDB, indexes all papers in the vault, then enters the
JSON-RPC stdin/stdout loop. The file watcher runs concurrently to keep
the index in sync with vault changes.

See server.py for the JSON-RPC dispatcher and tool implementations.
"""
from mcp_server.server import run

if __name__ == "__main__":
    run()
