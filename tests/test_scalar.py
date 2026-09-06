"""Correlated scalar RPCs, including execution counts and shared HTTP/OAuth routing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_remote
from test_integration import FIXTURE, connect, events, quote


class ScalarIntegration(unittest.TestCase):
    def setUp(self):
        root = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-scalar-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "context.duckdb"
        self.log = Path(self.temp.name) / "events.jsonl"
        self.c = connect(self.db)
        self.addCleanup(self.c.close)
        args = json.dumps([str(FIXTURE), str(self.log)])
        self.c.execute(f"PRAGMA mcp_register('demo', {quote(sys.executable)}, {quote(args)})")

    def calls(self):
        return [e for e in events(self.log) if e.get("method") == "tools/call"]

    def test_dynamic_arguments_and_recursive_chain(self):
        rows = self.c.execute("""
            SELECT mcp_tool_json(s, t, json_object('query', q))->>'$.items[0].title'
            FROM (VALUES ('demo', 'search', 'first'), ('demo', 'search', 'second')) v(s,t,q)
        """).fetchall()
        self.assertEqual(rows, [("first issue 1",), ("second issue 1",)])
        rows = self.c.execute("""
            WITH RECURSIVE chain(n, result) AS (
                SELECT 1, mcp_tool_json('demo', 'search', '{"query":"seed"}')
                UNION ALL
                SELECT n+1, mcp_tool_json('demo', 'search',
                    json_object('query', result->>'$.items[0].title'))
                FROM chain WHERE n < 3
            ) SELECT n, result->>'$.items[0].title' FROM chain ORDER BY n
        """).fetchall()
        self.assertEqual(rows, [(1, "seed issue 1"), (2, "seed issue 1 issue 1"),
                                (3, "seed issue 1 issue 1 issue 1")])
        self.assertEqual(len(self.calls()), 5)

    def test_volatile_prepared_explain_null_and_empty(self):
        sql = "SELECT mcp_tool_json('demo', 'raw', '{}') FROM range(3)"
        self.c.execute("EXPLAIN " + sql).fetchall()
        self.c.execute("PREPARE rpc AS " + sql)
        self.assertEqual(events(self.log), [])
        for _ in range(2):
            self.assertEqual([json.loads(r[0]) for r in self.c.execute("EXECUTE rpc").fetchall()],
                             [[{"hello": "world"}]] * 3)
        self.assertEqual(len(self.calls()), 6)
        self.assertEqual(self.c.execute("""
            SELECT mcp_tool_json(s,t,a) FROM (VALUES
                (NULL,'raw','{}'), ('demo',NULL,'{}'), ('demo','raw',NULL)) v(s,t,a)
        """).fetchall(), [(None,)] * 3)
        self.c.execute("SELECT mcp_tool_json('demo','raw','{}') FROM range(0)").fetchall()
        self.assertEqual(len(self.calls()), 6)

    def test_validation_and_no_retry(self):
        for expression, message in [
            ("'demo','raw','[]'", "JSON object"),
            ("'demo','search','{}'", "Missing required"),
            ("'demo','search','{\"query\":1}'", "requires string"),
            ("'missing','raw','{}'", "Unknown MCP server"),
            ("'demo','missing','{}'", "Unknown MCP tool"),
            ("'demo','raw','not json'", "Malformed JSON"),
        ]:
            with self.assertRaisesRegex(Exception, message):
                self.c.execute(f"SELECT mcp_tool_json({expression})").fetchall()
        self.assertEqual(self.calls(), [])
        with self.assertRaisesRegex(Exception, "isError"):
            self.c.execute("SELECT mcp_tool_json('demo','fail','{}') FROM range(3)").fetchall()
        self.assertEqual(len(self.calls()), 1)
        self.c.execute("SELECT mcp_tool_json('demo','raw','{}')").fetchall()
        self.assertEqual(len(self.calls()), 2)

    def test_multiple_chunks_and_selected_rows(self):
        rows = self.c.execute("""
            SELECT i, mcp_tool_json('demo','echo',
                json_object('values',list_value(i)))->>'$.values[0]'
            FROM range(4100) t(i) WHERE i % 2 = 0 ORDER BY i
        """).fetchall()
        self.assertEqual(rows, [(i, str(i)) for i in range(0, 4100, 2)])
        self.assertEqual(len(self.calls()), 2050)

    def test_materialized_response_reuse_and_json_type(self):
        self.c.execute("PRAGMA mcp_discover('demo')")
        rows = self.c.execute("""
            WITH responses AS MATERIALIZED (
                SELECT mcp_tool_json('demo','search',json_object('query',title)) AS r
                FROM demo.search(query := 'seed')
            ) SELECT typeof(r), r->>'$.items[0].title', r->>'$.items[1].title'
            FROM responses
        """).fetchall()
        self.assertEqual(rows, [
            ("JSON", "seed issue 1 issue 1", "seed issue 1 issue 2"),
            ("JSON", "seed issue 2 issue 1", "seed issue 2 issue 2"),
        ])
        self.assertEqual(len(self.calls()), 3)

    def test_varying_servers_and_tools(self):
        args = json.dumps([str(FIXTURE), str(self.log)])
        self.c.execute(f"PRAGMA mcp_register('other',{quote(sys.executable)},{quote(args)})")
        rows = self.c.execute("""
            SELECT mcp_tool_json(s,t,a) FROM (VALUES
                ('demo','echo','{"nullable":null}'),
                ('other','raw','{}'), ('demo','root_array','{}')) v(s,t,a)
        """).fetchall()
        self.assertEqual(json.loads(rows[0][0]), {"nullable": None})
        self.assertEqual(json.loads(rows[1][0]), [{"hello": "world"}])
        self.assertEqual(json.loads(rows[2][0])[0]["title"], "array issue 1")
        self.assertEqual(len(self.calls()), 3)

    def test_external_access_checked_at_execution(self):
        self.c.execute("PREPARE rpc AS SELECT mcp_tool_json('demo','raw','{}')")
        self.c.execute("SET enable_external_access=false")
        with self.assertRaisesRegex(Exception, "enable_external_access"):
            self.c.execute("EXECUTE rpc").fetchall()
        self.assertEqual(events(self.log), [])

    def test_stock_cli_correlated_calls(self):
        cli = os.environ.get("DUCKDB_CLI")
        if not cli:
            self.skipTest("Set DUCKDB_CLI to the stock DuckDB 1.5.5 executable")
        from test_integration import EXTENSION
        self.c.close()
        result = subprocess.run(
            [cli, "-unsigned", "-readonly", "-csv", "-noheader", str(self.db)],
            input=f"LOAD {quote(EXTENSION)}; SELECT mcp_tool_json('demo','echo',"
                  "json_object('values',list_value(i)))->>'$.values[0]' FROM range(2) t(i);",
            text=True, capture_output=True, check=True, timeout=30)
        self.assertEqual(result.stdout.strip().splitlines(), ["0", "1"])
        self.assertEqual(len(self.calls()), 2)


class ScalarRemoteIntegration(unittest.TestCase):
    setUp = test_remote.RemoteIntegration.setUp

    def test_oauth_reopen_and_cross_transport_chain(self):
        with test_remote.connect(self.db, self.secrets) as c:
            test_remote.register(c, self.fixture.url)
            test_remote.login(c)
            c.execute("PRAGMA mcp_discover('remote')")
            args = json.dumps([str(FIXTURE)])
            c.execute(f"PRAGMA mcp_register('demo',{quote(sys.executable)},{quote(args)})")
            c.execute("PRAGMA mcp_discover('demo')")
        with test_remote.connect(self.db, self.secrets, readonly=True) as c:
            rows = c.execute("""
                WITH first AS MATERIALIZED (
                    SELECT mcp_tool_json('demo','search',json_object('query', i::VARCHAR)) AS r
                    FROM range(2) t(i)
                ) SELECT mcp_tool_json('remote','search',
                    json_object('query',r->>'$.items[0].title'))->>'$[0].title' FROM first
            """).fetchall()
            self.assertEqual(rows, [("0 issue 1 from remote",), ("1 issue 1 from remote",)])
            before = len([e for e in self.fixture.events if e.get("method") == "tools/call"])
            with self.assertRaisesRegex(Exception, "503"):
                c.execute("SELECT mcp_tool_json('remote','search','{\"query\":\"http_error\"}')").fetchall()
            after = len([e for e in self.fixture.events if e.get("method") == "tools/call"])
            self.assertEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main()
