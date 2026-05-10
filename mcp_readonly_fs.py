#!/usr/bin/env python3
"""
Read-only MCP filesystem server.

Exposes two tools to MCP clients:
  - list_directory(path)  -- list files and subdirectories under a path
  - read_file(path)       -- return the contents of a file

All paths are resolved relative to REPO_ROOT and must remain within it.
No write operations are exposed.

Dependencies (no Anthropic packages):
  pip install starlette uvicorn

Usage:
  REPO_ROOT=/path/to/repo python mcp_readonly_fs.py [--host HOST] [--port PORT]

  Defaults: host=127.0.0.1  port=9000
  For use from a Vagrant VM, bind to the host-only interface, e.g.:
    REPO_ROOT=/home/user/projects/MMMobile python mcp_readonly_fs.py --host 192.168.56.1
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(os.environ.get("REPO_ROOT", ".")).resolve()
log.info("REPO_ROOT: %s", REPO_ROOT)

SERVER_INFO = {"name": "mcp-readonly-fs", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def safe_resolve(raw: str) -> Path:
    """
    Resolve a client-supplied path to an absolute path inside REPO_ROOT.
    Raises ValueError if the path escapes the root.
    """
    # Strip leading slash so Path(root) / "/etc/passwd" doesn't escape
    stripped = raw.lstrip("/")
    resolved = (REPO_ROOT / stripped).resolve()
    if not resolved.is_relative_to(REPO_ROOT):
        raise ValueError(f"Path {raw!r} is outside the allowed root")
    return resolved

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_list_directory(args: dict) -> dict:
    raw = args.get("path", "")
    try:
        target = safe_resolve(raw)
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}

    if not target.exists():
        return {"content": [{"type": "text", "text": f"Path does not exist: {raw}"}], "isError": True}
    if not target.is_dir():
        return {"content": [{"type": "text", "text": f"Not a directory: {raw}"}], "isError": True}

    entries = []
    for entry in sorted(target.iterdir()):
        rel = entry.relative_to(REPO_ROOT)
        kind = "dir" if entry.is_dir() else "file"
        size = "" if entry.is_dir() else f"  ({entry.stat().st_size} bytes)"
        entries.append(f"{kind}  {rel}{size}")

    text = "\n".join(entries) if entries else "(empty directory)"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def tool_read_file(args: dict) -> dict:
    raw = args.get("path", "")
    try:
        target = safe_resolve(raw)
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}

    if not target.exists():
        return {"content": [{"type": "text", "text": f"File does not exist: {raw}"}], "isError": True}
    if not target.is_file():
        return {"content": [{"type": "text", "text": f"Not a file: {raw}"}], "isError": True}

    size = target.stat().st_size
    if size > 1_000_000:
        return {
            "content": [{"type": "text", "text": f"File too large ({size} bytes). Max 1 MB."}],
            "isError": True,
        }

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {
            "content": [{"type": "text", "text": f"File is not valid UTF-8: {raw}"}],
            "isError": True,
        }

    return {"content": [{"type": "text", "text": text}], "isError": False}

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {
    "list_directory": {
        "meta": {
            "name": "list_directory",
            "description": (
                "List the contents of a directory within the project repository. "
                "Pass an empty string or '.' for the root."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the repo (e.g. 'Design' or 'Design/decisions')",
                    }
                },
                "required": [],
            },
        },
        "fn": tool_list_directory,
    },
    "read_file": {
        "meta": {
            "name": "read_file",
            "description": (
                "Read the contents of a file within the project repository. "
                "Returns the raw text content."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file within the repo (e.g. 'Design/model.json')",
                    }
                },
                "required": ["path"],
            },
        },
        "fn": tool_read_file,
    },
}

# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------

def jsonrpc_response(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def jsonrpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def dispatch(msg: dict) -> dict | None:
    """
    Handle one JSON-RPC message. Returns a dict to send back, or None for
    notifications (which must not receive a response).
    """
    id_ = msg.get("id")          # None for notifications
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # Notifications have no id and need no response
    if id_ is None:
        log.debug("Notification received: %s", method)
        return None

    if method == "initialize":
        return jsonrpc_response(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}},
        })

    if method == "tools/list":
        return jsonrpc_response(id_, {
            "tools": [t["meta"] for t in TOOLS.values()]
        })

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name not in TOOLS:
            return jsonrpc_error(id_, -32602, f"Unknown tool: {name!r}")
        result = TOOLS[name]["fn"](arguments)
        return jsonrpc_response(id_, result)

    if method == "ping":
        return jsonrpc_response(id_, {})

    return jsonrpc_error(id_, -32601, f"Method not found: {method!r}")

# ---------------------------------------------------------------------------
# Starlette HTTP endpoint
# Implements Streamable HTTP transport (single /mcp endpoint, POST)
# ---------------------------------------------------------------------------

async def mcp_endpoint(request: Request) -> Response:
    try:
        body = await request.body()
        msg = json.loads(body)
    except Exception:
        payload = jsonrpc_error(None, -32700, "Parse error")
        return JSONResponse(payload, status_code=400)

    log.debug("-> %s", msg)
    response = dispatch(msg)

    if response is None:
        # Notification: no body response
        return Response(status_code=204)

    log.debug("<- %s", response)
    return JSONResponse(response)


app = Starlette(routes=[
    Route("/mcp", mcp_endpoint, methods=["POST"]),
])

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only MCP filesystem server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9000,
                        help="Port to listen on (default: 9000)")
    args = parser.parse_args()

    log.info("Starting MCP read-only filesystem server on %s:%d", args.host, args.port)
    log.info("Serving repo: %s", REPO_ROOT)
    log.info("Tools: %s", ", ".join(TOOLS))

    uvicorn.run(app, host=args.host, port=args.port)
