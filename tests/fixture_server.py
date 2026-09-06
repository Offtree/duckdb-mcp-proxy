#!/usr/bin/env python3
"""Dependency-free MCP stdio fixture. stdout is exclusively JSON-RPC.

Optional argv[1] is an event log used by the subprocess integration tests.
"""
import json
import sys


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


ISSUE = object_schema({
    "id": {"type": "integer"},
    "title": {"type": "string"},
    "author": {"type": "string"},
    "open": {"type": "boolean"},
    "score": {"type": "number"},
    "labels": {"type": "array", "items": {"type": "string"}},
    "detail": {"type": "object"},
    "note": {"type": ["string", "null"]},
}, ["id", "title", "author"])
SEARCH_INPUT = object_schema({
    "query": {"type": "string"},
    "limit": {"type": "integer"},
    "include_closed": {"type": "boolean"},
}, ["query"])
SEARCH_INPUT["additionalProperties"] = False
TOOLS = [
    {"name": "search", "description": "Search fixture issues", "inputSchema": SEARCH_INPUT,
     "outputSchema": object_schema({"items": {"type": "array", "items": ISSUE}}, ["items"])},
    {"name": "raw", "description": "No schema: JSON fallback", "inputSchema": object_schema({})},
    {"name": "fail", "description": "Tool-level error", "inputSchema": object_schema({})},
    {"name": "bad_output", "inputSchema": object_schema({}), "outputSchema": ISSUE},
    {"name": "root_array", "inputSchema": object_schema({}),
     "outputSchema": {"type": "array", "items": ISSUE}},
    {"name": "echo", "inputSchema": object_schema({
        "obj": {"type": "object"}, "values": {"type": "array", "items": {"type": "integer"}},
        "nullable": {"type": ["string", "null"]},
    }), "outputSchema": object_schema({"obj": {"type": "object"},
        "values": {"type": "array"}, "nullable": {"type": ["string", "null"]}})},
]


def event(data):
    if len(sys.argv) > 1:
        with open(sys.argv[1], "a") as f:
            f.write(json.dumps(data) + "\n")


def issue(i, query):
    return {"id": i + 1, "title": f"{query} issue {i + 1}",
            "author": "alice" if i % 2 == 0 else "bob", "open": i % 2 == 0,
            "score": i + 0.5, "labels": ["context", "sql"], "detail": {"rank": i},
            "note": None}


initialized = False
event({"event": "start"})
for line in sys.stdin:
    req = json.loads(line)
    method = req["method"]
    event(req)
    if method == "notifications/initialized":
        initialized = True
        continue
    if "id" not in req:
        continue
    error = None
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "context-fixture", "version": "1"}}
    elif not initialized:
        error = {"code": -32000, "message": "initialize first"}
    elif method == "tools/list":
        if req.get("params", {}).get("cursor") == "page2":
            result = {"tools": TOOLS[2:]}
        else:
            result = {"tools": TOOLS[:2], "nextCursor": "page2"}
    elif method == "tools/call":
        params = req["params"]
        name, args = params["name"], params.get("arguments", {})
        if name == "fail":
            result = {"isError": True, "content": [{"type": "text", "text": "fixture failure"}]}
        elif name == "bad_output":
            result = {"structuredContent": {"id": "not an integer", "title": "bad", "author": "alice"}}
        elif name == "raw":
            result = {"content": [{"type": "text", "text": '[{"hello":"world"}]'}]}
        elif name == "echo":
            result = {"structuredContent": args}
        elif name == "root_array":
            result = {"content": [{"type": "text", "text": json.dumps([issue(0, "array")])}]}
        elif name == "search":
            count = args.get("limit", 2)
            result = {"structuredContent": {"items": [issue(i, args["query"]) for i in range(count)]}}
        else:
            error = {"code": -32602, "message": "unknown tool"}
    else:
        error = {"code": -32601, "message": "unknown method"}
    response = {"jsonrpc": "2.0", "id": req["id"]}
    response["error" if error else "result"] = error if error else result
    print(json.dumps(response), flush=True)
