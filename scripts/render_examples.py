#!/usr/bin/env python3
"""Render location-independent SQL templates; no database or MCP client logic."""
import json
import argparse
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--remote-url", help="Also render HTTP/OAuth fixture examples")
args = parser.parse_args()
out = root / "build/examples"
out.mkdir(parents=True, exist_ok=True)


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


values = {
    "@EXTENSION@": literal(root / "build/extension/mcp_context/mcp_context.duckdb_extension"),
    "@PYTHON@": literal(sys.executable),
    "@FIXTURE_ARGS@": literal(json.dumps([str(root / "tests/fixture_server.py")])),
}
names = ["setup.sql", "reopen.sql"]
if args.remote_url:
    values["@REMOTE_URL@"] = literal(args.remote_url)
    names += ["remote_setup.sql", "remote_reopen.sql"]
for name in names:
    sql = (root / "examples" / name).read_text()
    for key, value in values.items():
        sql = sql.replace(key, value)
    (out / name).write_text(sql)
    print(out / name)
