#!/usr/bin/env python3
"""
MCP staging server for AI-proposed file changesets.

Exposes three tools to MCP clients:
  - begin_changeset(description)  -- wipe staging, start a new changeset
  - write_file(path, content)     -- stage a file in the current changeset
  - end_changeset()               -- finalise and log the changeset summary

All staged files are written under ~/.claude-staging/<repo-name>/proposed/.
Nothing can be written outside that directory.

Transports
----------
  POST /mcp          Streamable-HTTP (single-request/response)
  GET  /sse          SSE stream — client connects here first; server sends
                     an 'endpoint' event pointing to /messages
  POST /messages     JSON-RPC messages from SSE clients; responses are
                     delivered over the open SSE stream

Root discovery (in priority order)
-----------------------------------
  1. Positional argument: mcp_stage_update.py /path/to/root
  2. Walk up from cwd to find the nearest .git directory
  3. Fall back to cwd if no .git found

Configuration
-------------
  An optional .mcp-serve.json file at the served root can override defaults:
    {
      "stage_host": "127.0.0.1",   // default: 127.0.0.1
      "stage_port": 9001           // default: 9001
    }
  CLI arguments always override .mcp-serve.json values.

Dependencies:
  Only starlette + uvicorn — both available as Debian bookworm packages:
    sudo apt install python3-uvicorn python3-starlette
    pip install --user starlette uvicorn

Usage:
  mcp_stage_update.py [root_path] [--host HOST] [--port PORT]
"""

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SERVER_INFO = {"name": "mcp-stage-update", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"

MANIFEST_FILENAME = ".manifest.json"

# Set in main() before the app starts
SERVED_ROOT: Path
STAGING_ROOT: Path
PROPOSED_ROOT: Path
CURRENT_ROOT: Path

# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------

def find_git_root(start: Path) -> Path | None:
    """Walk up from start until a .git directory is found."""
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

def safe_stage_resolve(raw: str) -> Path:
    """
    Resolve a client-supplied path to an absolute path inside PROPOSED_ROOT.
    Raises ValueError if the path escapes the proposed root.
    """
    stripped = raw.lstrip("/")
    resolved = (PROPOSED_ROOT / stripped).resolve()
    if not resolved.is_relative_to(PROPOSED_ROOT):
        raise ValueError(f"Path {raw!r} is outside the allowed staging root")
    if resolved.name == MANIFEST_FILENAME:
        raise ValueError(f"Path {raw!r} is reserved for internal use")
    return resolved

# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def read_manifest() -> dict:
    manifest_path = STAGING_ROOT / MANIFEST_FILENAME
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_manifest(manifest: dict) -> None:
    manifest_path = STAGING_ROOT / MANIFEST_FILENAME
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_begin_changeset(args: dict) -> dict:
    description = args.get("description", "").strip()
    if not description:
        return {"content": [{"type": "text", "text": "description is required"}], "isError": True}

    # Wipe proposed and current subdirs, leave manifest at staging root
    for subdir in (PROPOSED_ROOT, CURRENT_ROOT):
        if subdir.exists():
            shutil.rmtree(subdir)
    PROPOSED_ROOT.mkdir(parents=True)

    manifest = {
        "status": "open",
        "description": description,
        "started": datetime.now(timezone.utc).isoformat(),
        "repo": str(SERVED_ROOT),
        "files": [],
    }
    write_manifest(manifest)

    log.info("Changeset begun: %s", description)
    return {
        "content": [{"type": "text", "text": f"Changeset started: {description}"}],
        "isError": False,
    }


def tool_write_file(args: dict) -> dict:
    raw = args.get("path", "").strip()
    content = args.get("content")

    if not raw:
        return {"content": [{"type": "text", "text": "path is required"}], "isError": True}
    if content is None:
        return {"content": [{"type": "text", "text": "content is required"}], "isError": True}

    manifest = read_manifest()
    if not manifest:
        return {
            "content": [{"type": "text", "text": "No active changeset. Call begin_changeset first."}],
            "isError": True,
        }
    if manifest.get("status") != "open":
        return {
            "content": [{"type": "text", "text": f"Changeset is not open (status: {manifest.get('status')!r}). Call begin_changeset to start a new one."}],
            "isError": True,
        }

    try:
        target = safe_stage_resolve(raw)
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}

    # Refuse overwrite
    if target.exists():
        return {
            "content": [{"type": "text", "text": f"File already staged: {raw}. Overwrite not permitted within a changeset."}],
            "isError": True,
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    size = target.stat().st_size
    manifest["files"].append({"path": raw, "size": size})
    write_manifest(manifest)

    log.info("Staged: %s (%d bytes)", raw, size)
    return {
        "content": [{"type": "text", "text": f"Staged: {raw} ({size} bytes)"}],
        "isError": False,
    }


def tool_end_changeset(args: dict) -> dict:
    manifest = read_manifest()
    if not manifest:
        return {
            "content": [{"type": "text", "text": "No active changeset."}],
            "isError": True,
        }
    if manifest.get("status") != "open":
        return {
            "content": [{"type": "text", "text": f"Changeset is not open (status: {manifest.get('status')!r})."}],
            "isError": True,
        }

    manifest["status"] = "complete"
    manifest["completed"] = datetime.now(timezone.utc).isoformat()
    write_manifest(manifest)

    files = manifest.get("files", [])
    total = sum(f["size"] for f in files)
    lines = [
        f"Changeset complete: {manifest['description']}",
        f"{len(files)} file(s), {total} bytes total:",
    ] + [f"  {f['path']} ({f['size']} bytes)" for f in files]

    summary = "\n".join(lines)
    log.info("%s", summary)

    return {
        "content": [{"type": "text", "text": summary}],
        "isError": False,
    }

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {
    "begin_changeset": {
        "meta": {
            "name": "begin_changeset",
            "description": (
                "Start a new changeset. Wipes any previously staged files "
                "and any in-progress merge working directory. "
                "Must be called before write_file."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Brief description of this changeset (used as git commit message suggestion)",
                    }
                },
                "required": ["description"],
            },
        },
        "fn": tool_begin_changeset,
    },
    "write_file": {
        "meta": {
            "name": "write_file",
            "description": (
                "Stage a file in the current changeset. "
                "Path is relative to the repo root. "
                "Refuses to overwrite a file already staged in this changeset."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the repo root (e.g. 'scripts/foo' or 'Design/Architecture.md')",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full UTF-8 text content of the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
        "fn": tool_write_file,
    },
    "end_changeset": {
        "meta": {
            "name": "end_changeset",
            "description": (
                "Finalise the current changeset. Logs a summary of staged files. "
                "The changeset is then ready for review with project-merge-update."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        "fn": tool_end_changeset,
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
        description="MCP staging server for AI-proposed file changesets",
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
        help="Repo root to serve (default: nearest git root above cwd, or cwd)",
    )
    parser.add_argument("--host", default=None, help="Interface to bind")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    cli = parser.parse_args()

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

    STAGING_ROOT = Path.home() / ".claude-staging" / SERVED_ROOT.name
    PROPOSED_ROOT = STAGING_ROOT / "proposed"
    CURRENT_ROOT  = STAGING_ROOT / "current"

    config = load_config(SERVED_ROOT)
    host = cli.host or config.get("stage_host", "127.0.0.1")
    port = cli.port or config.get("stage_port", 9001)

    log.info("Repo root:    %s", SERVED_ROOT)
    log.info("Staging root: %s", STAGING_ROOT)
    log.info("Proposed:     %s", PROPOSED_ROOT)
    log.info("Current:      %s", CURRENT_ROOT)
    log.info("Listening:    %s:%d", host, port)
    log.info("Tools:        %s", ", ".join(TOOLS))
    log.info("Transports:   POST /mcp  |  GET /sse + POST /messages")

    uvicorn.run(app, host=host, port=port)
