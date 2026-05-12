#!/usr/bin/env python3
"""
Read-only MCP filesystem server.

Exposes two tools to MCP clients:
  - list_directory(path)  -- list files and subdirectories under a path
  - read_file(path)       -- return the contents of a file

All paths are resolved relative to the served root and must remain within it.
No write operations are exposed.

Transports
----------
  POST /mcp          Streamable-HTTP (single-request/response)
  GET  /sse          SSE stream — client connects here first; server sends
                     an 'endpoint' event pointing to /messages
  POST /messages     JSON-RPC messages from SSE clients; responses are
                     delivered over the open SSE stream

Root discovery (in priority order)
-----------------------------------
  1. Positional argument: mcp_readonly_fs.py /path/to/root
  2. Walk up from cwd to find the nearest .git directory
  3. Fall back to cwd if no .git found

Configuration
-------------
  An optional .mcp-serve.json file at the served root can override defaults:
    {
      "host": "192.168.56.1",   // default: 192.168.56.1
      "port": 9000              // default: 9000
    }
  CLI arguments always override .mcp-serve.json values.

Dependencies:
  Only starlette + uvicorn — both available as Debian bookworm packages,
  no extra pip installs required:
    sudo apt install python3-uvicorn python3-starlette   # Debian bookworm
    pip install --user starlette uvicorn                 # or via pip

Usage:
  mcp_readonly_fs.py [root_path] [--host HOST] [--port PORT]

  To expose via HTTPS, pass --public-host and certificate paths.  Caddy will
  be started as a reverse proxy and torn down when the server exits:
    mcp_readonly_fs.py \
        --public-host vhost.x.mc0e.net \
        --public-port 9000 \
        --cert /etc/letsencrypt/live/vhost.x.mc0e.net/fullchain.pem \
        --key  /etc/letsencrypt/live/vhost.x.mc0e.net/privkey.pem
  Caddy listens on 192.168.56.1:<public-port> (HTTPS) and forwards to
  127.0.0.1:<port> (HTTP).  Requires caddy >= 2.0 in $PATH.

  For use from a Vagrant VM, bind to the host-only interface:
    mcp_readonly_fs.py --host 192.168.56.1
  or set "host" in .mcp-serve.json at the repo root.

Installation:
  chmod +x mcp_readonly_fs.py
  ln -s $(pwd)/mcp_readonly_fs.py ~/.local/bin/mcp-serve
  # ensure ~/.local/bin is in $PATH (add to ~/.bashrc if not):
  #   export PATH="$HOME/.local/bin:$PATH"

Notes:
  - With --public-host, uvicorn binds to 127.0.0.1 only (not 192.168.56.1).
  - Without --public-host, behaviour is unchanged from previous versions.
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SERVER_INFO = {"name": "mcp-readonly-fs", "version": "0.3.0"}
PROTOCOL_VERSION = "2024-11-05"

# Set in main() before the app starts
SERVED_ROOT: Path

# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------

def find_git_root(start: Path) -> Path | None:
    """Walk up from start until a .git directory is found. Returns None if not found."""
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(git_root: Path) -> dict:
    """Load .mcp-serve.json from the git root if present; return {} otherwise."""
    config_path = git_root / ".mcp-serve.json"
    if not config_path.exists():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        log.info("Loaded config from %s", config_path)
        return config
    except Exception as e:
        log.warning("Could not read %s: %s", config_path, e)
        return {}

# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def safe_resolve(raw: str) -> Path:
    """
    Resolve a client-supplied path to an absolute path inside SERVED_ROOT.
    Raises ValueError if the path escapes the root.
    """
    stripped = raw.lstrip("/")
    resolved = (SERVED_ROOT / stripped).resolve()
    if not resolved.is_relative_to(SERVED_ROOT):
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
        rel = entry.relative_to(SERVED_ROOT)
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
                "List the contents of a directory within the served root. "
                "Pass an empty string or '.' for the root."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the root (e.g. 'src' or 'src/lib')",
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
                "Read the contents of a file within the served root. "
                "Returns the raw text content."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file within the root (e.g. 'README.md')",
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
    id_ = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

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
# Streamable-HTTP transport  POST /mcp
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
        return Response(status_code=204)

    log.debug("<- %s", response)
    return JSONResponse(response)

# ---------------------------------------------------------------------------
# SSE transport  GET /sse  +  POST /messages
#
# Implemented with raw Starlette StreamingResponse — no sse-starlette needed.
#
# Protocol:
#   1. Client opens GET /sse → receives event: endpoint pointing to /messages
#   2. Client POSTs JSON-RPC to /messages?session_id=...
#   3. Server pushes JSON-RPC response over the open SSE stream
# ---------------------------------------------------------------------------

_sse_queues: dict[str, asyncio.Queue] = {}


def _sse_format(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


async def sse_endpoint(request: Request):
    session_id = str(id(request))
    queue: asyncio.Queue = asyncio.Queue()
    _sse_queues[session_id] = queue
    log.info("SSE client connected, session=%s", session_id)

    async def event_stream():
        yield _sse_format("endpoint", f"/messages?session_id={session_id}")
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse_format("message", json.dumps(payload))
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _sse_queues.pop(session_id, None)
            log.info("SSE client disconnected, session=%s", session_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def messages_endpoint(request: Request) -> Response:
    session_id = request.query_params.get("session_id")
    queue = _sse_queues.get(session_id)
    if queue is None:
        return JSONResponse({"error": "Unknown or expired session_id"}, status_code=400)

    try:
        body = await request.body()
        msg = json.loads(body)
    except Exception:
        await queue.put(jsonrpc_error(None, -32700, "Parse error"))
        return Response(status_code=204)

    log.debug("SSE -> %s", msg)
    response = dispatch(msg)
    if response is not None:
        log.debug("SSE <- %s", response)
        await queue.put(response)

    return Response(status_code=204)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Starlette(routes=[
    Route("/mcp",      mcp_endpoint,      methods=["POST"]),
    Route("/sse",      sse_endpoint,      methods=["GET"]),
    Route("/messages", messages_endpoint, methods=["POST"]),
])

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read-only MCP filesystem server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Root discovery: walks up from cwd to find the nearest .git directory;\n"
            "falls back to cwd if no .git is found.\n"
            "Config:         .mcp-serve.json at the served root can set host/port defaults.\n"
            "CLI args always override .mcp-serve.json."
        ),
    )
    parser.add_argument(
        "root_path",
        nargs="?",
        default=None,
        help="Directory to serve (default: nearest git root above cwd, or cwd)",
    )
    parser.add_argument("--host", default=None, help="Interface to bind")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    parser.add_argument("--public-host", default=None, metavar="HOSTNAME",
                        help="Public hostname for Caddy HTTPS reverse proxy")
    parser.add_argument("--public-port", type=int, default=None, metavar="PORT",
                        help="Public HTTPS port for Caddy (default: same as --port)")
    parser.add_argument("--cert", default=None, metavar="PATH",
                        help="TLS certificate file for Caddy (PEM, required with --public-host)")
    parser.add_argument("--key", default=None, metavar="PATH",
                        help="TLS private key file for Caddy (PEM, required with --public-host)")
    cli = parser.parse_args()

    # Resolve served root
    if cli.root_path:
        SERVED_ROOT = Path(cli.root_path).resolve()
        if not SERVED_ROOT.is_dir():
            log.error("Not a directory: %s", SERVED_ROOT)
            sys.exit(1)
    else:
        cwd = Path.cwd()
        git_root = find_git_root(cwd)
        if git_root:
            SERVED_ROOT = git_root
        else:
            log.warning("No .git directory found above %s; serving cwd", cwd)
            SERVED_ROOT = cwd.resolve()

    config = load_config(SERVED_ROOT)

    # With --public-host, uvicorn binds loopback only; Caddy faces the network.
    # Without it, fall back to the host-only interface as before.
    if cli.public_host:
        default_host = "127.0.0.1"
    else:
        default_host = "192.168.56.1"

    host = cli.host or config.get("host", default_host)
    port = cli.port or config.get("port", 9000)
    public_port = cli.public_port or port
    log.info("Serving:    %s", SERVED_ROOT)
    log.info("Listening:  %s:%d", host, port)
    log.info("Tools:      %s", ", ".join(TOOLS))
    log.info("Transports: POST /mcp  |  GET /sse + POST /messages")

    caddy_proc = None
    if cli.public_host:
        missing = [flag for flag, val in [("--cert", cli.cert), ("--key", cli.key)] if not val]
        if missing:
            log.error("--public-host requires %s", " and ".join(missing))
            sys.exit(1)
        caddyfile = (
            f"{cli.public_host}:{public_port} {{\n"
            f"    bind 192.168.56.1\n"
            f"    tls {cli.cert} {cli.key}\n"
            f"    reverse_proxy 127.0.0.1:{port}\n"
            f"}}\n"
        )
        log.info("Starting Caddy: %s:%d -> 127.0.0.1:%d", cli.public_host, public_port, port)
        log.debug("Caddyfile:\n%s", caddyfile)
        caddy_proc = subprocess.Popen(
            ["caddy", "run", "--config", "-", "--adapter", "caddyfile"],
            stdin=subprocess.PIPE,
        )
        caddy_proc.stdin.write(caddyfile.encode())
        caddy_proc.stdin.close()

    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        if caddy_proc is not None:
            log.info("Stopping Caddy (pid=%d)", caddy_proc.pid)
            caddy_proc.terminate()
            try:
                caddy_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("Caddy did not exit cleanly; killing")
                caddy_proc.kill()
