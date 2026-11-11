"""Offer the analysis tools to other local programs over the Model Context Protocol.

    python -m loan_analyst mcp --snapshot latest

An MCP client (an editor, a desktop assistant, another agent) starts this
command as a child process and exchanges one JSON-RPC message per line over
standard input and output. It can list the tools and call them. Nothing else
is offered.

Safety boundaries, the same ones the local analyst has:

* Only the allowlisted tools from ``calculations.build_registry`` exist, and
  ``ToolRegistry.call`` validates every argument.
* Tools compute over one saved snapshot held in memory. This server never
  connects to MySQL and accepts no SQL, paths, hosts or credentials.
* The scenario (cutoffs, cohort, any expansion assumptions) comes from the
  command line of whoever starts the server, never from the client.
* It opens no network port. Standard output carries protocol messages only;
  progress goes to standard error.

Whatever program connects receives the calculated facts and aggregate results,
so only connect clients you trust with them: this server cannot control what a
client does with the text it is given.

This implements the handshake-based protocol revisions listed in
PROTOCOL_VERSIONS (tools only). Reference: https://modelcontextprotocol.io/specification
"""

import json
import sys

from . import prompts
from .calculations import build_registry
from .settings import SettingsError
from .support import read_json
from .tools import ToolContext
from .workflow import obtain_snapshot

# Newest first. A client asking for another revision is offered the newest one here.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "loan-analyst", "title": "Loan analyst tools", "version": "1.0.0"}
INSTRUCTIONS = ("Read-only analyses of historical Lending Club aggregates from one saved snapshot. Results are "
                "historical comparisons, not forecasts or lending recommendations. Quote numbers only from the "
                "returned facts.")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


class ProtocolError(Exception):
    """A request this server must answer with a JSON-RPC error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def tool_list(registry):
    """The registry's tools in MCP shape; every tool only reads the snapshot."""
    tools = []
    for definition in registry.definitions():
        function = definition["function"]
        tools.append({
            "name": function["name"],
            "description": function["description"],
            "inputSchema": function["parameters"],
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        })
    return tools


def call_tool(registry, context, params):
    """Run one tool; calculation problems are tool results, unknown tools are protocol errors."""
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        raise ProtocolError(INVALID_PARAMS, "tools/call needs a tool name")
    arguments = params.get("arguments", {})

    envelope = registry.call(params["name"], arguments, context)
    if not envelope["ok"]:
        error = envelope["error"]
        if error["code"] == "unknown_tool":
            raise ProtocolError(INVALID_PARAMS, error["message"])
        # Already sanitized by the registry: never raw exception text.
        return {"content": [{"type": "text", "text": f"{error['code']}: {error['message']}"}], "isError": True}

    output = envelope["output"]
    return {
        "content": [{"type": "text", "text": f"{output.title}\n{prompts.fact_sheet(output.facts)}"}],
        "structuredContent": {"title": output.title, "arguments": envelope["arguments"],
                              "facts": output.facts, "result": output.result},
        "isError": False,
    }


def handle(message, registry, context):
    """Answer one request. Returns the result, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(
            message.get("method"), str):
        raise ProtocolError(INVALID_REQUEST, "Expected one JSON-RPC 2.0 request object")
    method, params = message["method"], message.get("params", {})

    if "id" not in message:
        return None  # notifications (e.g. notifications/initialized) need no reply

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        return {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_list(registry)}
    if method == "tools/call":
        return call_tool(registry, context, params)
    raise ProtocolError(METHOD_NOT_FOUND, f"Method not supported: {method[:80]}")


def serve(registry, context, reader, writer):
    """Read one JSON message per line until the client closes the stream."""

    def send(payload):
        writer.write(json.dumps({"jsonrpc": "2.0", **payload}, ensure_ascii=False, allow_nan=False) + "\n")
        writer.flush()

    for line in reader:
        line = line.lstrip("﻿")  # some shells put a byte-order mark before the first message
        if not line.strip():
            continue

        request_id = None
        try:
            try:
                message = read_json(line)
            except ValueError:
                raise ProtocolError(PARSE_ERROR, "Message is not valid JSON") from None
            if isinstance(message, dict):
                request_id = message.get("id")
            result = handle(message, registry, context)
        except ProtocolError as error:
            send({"id": request_id, "error": {"code": error.code, "message": str(error)}})
            continue

        if result is not None:
            send({"id": request_id, "result": result})


def run_mcp(options, services, reader=None, writer=None):
    """The `mcp` command: load the saved snapshot, then serve until the client disconnects."""
    if options.snapshot is None:
        raise SettingsError("mcp serves a saved snapshot and never reads MySQL: add --snapshot latest "
                            "or --snapshot PATH")
    snapshot = obtain_snapshot(options.database, options.snapshot, services)
    registry = build_registry()
    context = ToolContext(snapshot, options.scenario)

    if reader is None:
        # Protocol messages are UTF-8 whatever the terminal's code page is.
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        reader, writer = sys.stdin, sys.stdout
    services.log(f"Serving {len(registry.names())} read-only tools over MCP (stdio) from snapshot "
                 f"{snapshot.snapshot_id}. Waiting for a client; press Ctrl+C to stop.")
    serve(registry, context, reader, writer)
    return 0
