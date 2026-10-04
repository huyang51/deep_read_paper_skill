"""MCP server entry point: `python -m mcp_server`.

Enters the JSON-RPC stdin/stdout loop immediately; the store is NOT built here.
The first index-backed tool call pays the one-time init (embedder load, vault
scan) inside a worker thread, so a cold start never blocks the handshake and a
model download cannot make the tools fail to register. The file watcher starts
alongside the loop and keeps the index in sync with vault edits.

See server.py for the JSON-RPC dispatcher and tool implementations.
"""
from mcp_server.server import run

if __name__ == "__main__":
    run()
