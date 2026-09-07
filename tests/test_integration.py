"""Run with .venv/bin/python tests/test_integration.py.

Each worker is a new OS process, not merely another DuckDB connection.
"""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = Path(os.environ.get("MCP_EXTENSION", ROOT / "build/extension/mcp_context/mcp_context.duckdb_extension"))
FIXTURE = ROOT / "tests/fixture_server.py"


def quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def connect(db, read_only=False):
    import duckdb
    c = duckdb.connect(str(db), read_only=read_only, config={"allow_unsigned_extensions": "true"})
    c.execute("LOAD " + quote(EXTENSION))
    return c


def events(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def worker(mode, db, log):
    if mode == "no_load":
        import duckdb
        before = len(events(log))
        with duckdb.connect(str(db), read_only=True) as c:
            try:
                c.execute("SELECT * FROM demo.search(query := 'no load')").fetchall()
            except duckdb.CatalogException as exc:
                assert "mcp_tool" in str(exc)
            else:
                raise AssertionError("An unknown local extension cannot autoload")
        assert len(events(log)) == before
        return
    c = connect(db, read_only=mode == "reopen")
    if mode == "setup":
        assert c.execute("SELECT * FROM mcp_servers()").fetchall() == []
        args = json.dumps([str(FIXTURE), str(log)])
        c.execute(f"PRAGMA mcp_register('demo', {quote(sys.executable)}, {quote(args)})")
        assert not log.exists(), "Registration must be offline"
        assert c.execute("SELECT connection_status FROM mcp_servers()").fetchone() == ("disconnected",)
        assert c.execute("SELECT count(*) FROM mcp_tool(server := 'demo',tool := 'search', "
                         "args := {'query':'before discovery'})").fetchone() == (2,)
        assert c.execute("SELECT count(*) FROM mcp_tools('demo')").fetchone() == (6,)
        initialization = next(e for e in events(log) if e.get("method") == "initialize")
        assert initialization["params"]["protocolVersion"] == "2025-06-18"
        assert initialization["params"]["capabilities"] == {}
        c.execute("PRAGMA mcp_discover('demo')")
        assert c.execute("SELECT count(*) FROM _mcp.tools").fetchone() == (6,)
        assert c.execute("SELECT last_discovery IS NOT NULL FROM mcp_servers()").fetchone() == (True,)
        assert c.execute("SELECT title FROM demo.search(query := 'duckdb') ORDER BY id").fetchall() == [
            ("duckdb issue 1",), ("duckdb issue 2",)]
        c.execute("CREATE TABLE people(name VARCHAR, github_username VARCHAR)")
        c.execute("INSERT INTO people VALUES ('Alice Example','alice')")
        c.execute("CREATE VIEW live_issues AS SELECT * FROM demo.search(query := 'view')")
    elif mode == "reopen":
        before = len(events(log))
        assert c.execute("SELECT connection_status FROM mcp_servers()").fetchone() == ("disconnected",)
        c.execute("EXPLAIN SELECT * FROM demo.search(query := 'explain')").fetchall()
        assert len(events(log)) == before, "Persisted schema should bind offline"
        assert c.execute("SELECT p.name,i.title FROM people p JOIN demo.search(query := 'again') i "
                         "ON p.github_username=i.author").fetchall() == [("Alice Example", "again issue 1")]
        assert c.execute("SELECT count(*) FROM live_issues").fetchone() == (2,)
        new_events = events(log)[before:]
        assert not any(e.get("method") == "tools/list" for e in new_events), "Reopen should reuse discovery snapshot"
        assert sum(e.get("event") == "start" for e in new_events) == 1
        assert sum(e.get("method") == "tools/call" for e in new_events) == 2
    c.close()


class Integration(unittest.TestCase):
    def test_stock_cli_discovery_error_is_readable_and_recoverable(self):
        cli = os.environ.get("DUCKDB_CLI") or shutil.which("duckdb")
        if not cli:
            self.skipTest("Set DUCKDB_CLI to the stock DuckDB 1.5.5 executable")
        result = subprocess.run(
            [cli, "-unsigned", "-csv", "-noheader", ":memory:"],
            input=(f"LOAD {quote(EXTENSION)};\n"
                   "PRAGMA mcp_register_http('known', 'https://example.com/mcp');\n"
                   "PRAGMA mcp_discover('nope');\nSELECT 'still alive';\n"),
            text=True, capture_output=True, timeout=30,
        )
        self.assertGreaterEqual(result.returncode, 0, result.stderr)
        self.assertIn("Unknown MCP server: nope", result.stderr)
        self.assertNotIn("Unknown exception", result.stderr)
        self.assertIn("still alive", result.stdout)

    def setUp(self):
        temp_root = os.environ.get("TMPDIR") or ("/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp-context-", dir=temp_root)
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "context.duckdb"
        self.log = Path(self.tmp.name) / "events.jsonl"
        self.run_worker("setup")

    def run_worker(self, mode):
        subprocess.run([sys.executable, __file__, "--worker", mode, str(self.db), str(self.log)],
                       check=True, timeout=45)

    def test_process_restart_and_read_only_composition(self):
        self.run_worker("no_load")
        self.run_worker("reopen")

    def test_runtime_shared_within_database_and_isolated_between_databases(self):
        before = len(events(self.log))
        other_log = Path(self.tmp.name) / "other.jsonl"
        with connect(self.db) as c:
            with c.cursor() as second_connection:
                c.execute("SELECT count(*) FROM demo.search(query := 'first')").fetchall()
                second_connection.execute("SELECT count(*) FROM demo.search(query := 'second')").fetchall()
            with connect(Path(self.tmp.name) / "other.duckdb") as other:
                self.assertEqual(other.execute("SELECT * FROM mcp_servers()").fetchall(), [])
                args = json.dumps([str(FIXTURE), str(other_log)])
                other.execute(f"PRAGMA mcp_register('demo',{quote(sys.executable)},{quote(args)})")
                other.execute("SELECT * FROM mcp_tool(server := 'demo',tool := 'raw')").fetchall()
            self.assertEqual(sum(e.get("event") == "start" for e in events(self.log)[before:]), 1)
            self.assertEqual(sum(e.get("event") == "start" for e in events(other_log)), 1)

    def test_mapping_and_execution(self):
        with connect(self.db) as c:
            before = len(events(self.log))
            rows = c.execute("SELECT * FROM mcp_tool(server := 'demo', tool := 'search', "
                             "args := {'query':'typed', 'limit':3, 'include_closed':true})").fetchall()
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0][:5], (1, "typed issue 1", "alice", True, 0.5))
            self.assertEqual(json.loads(rows[0][5]), ["context", "sql"])
            self.assertEqual(json.loads(rows[0][6]), {"rank": 0})
            self.assertIsNone(rows[0][7])
            self.assertEqual(c.execute("SELECT count(*) FROM demo.search(query := 'big', \"limit\" := 5000)").fetchone(), (5000,))
            self.assertEqual(c.execute("SELECT count(*) FROM demo.search(query := 'empty', \"limit\" := 0)").fetchone(), (0,))
            self.assertEqual(json.loads(c.execute("SELECT * FROM demo.raw()").fetchone()[0]), [{"hello": "world"}])
            self.assertEqual(c.execute("SELECT id FROM demo.root_array()").fetchone(), (1,))
            echo = c.execute("SELECT * FROM mcp_tool(server := 'demo',tool := 'echo', "
                             "args := '{\"obj\":{\"x\":1},\"values\":[1,2],\"nullable\":null}')").fetchone()
            self.assertEqual(json.loads(echo[0]), {"x": 1})
            self.assertEqual(json.loads(echo[1]), [1, 2])
            self.assertIsNone(echo[2])
            new_events = events(self.log)[before:]
            self.assertEqual(sum(e.get("event") == "start" for e in new_events), 1)
            self.assertEqual(sum(e.get("method") == "tools/call" for e in new_events), 6)

    def test_errors_do_not_invoke_or_retry(self):
        with connect(self.db) as c:
            before = len(events(self.log))
            for sql, message in [
                ("SELECT * FROM demo.search()", "Missing required"),
                ("SELECT * FROM demo.search(query := 42)", "requires string"),
                ("SELECT * FROM mcp_tool(server := 'missing', tool := 'x')", "Unknown MCP server"),
                ("SELECT * FROM demo.search(query := 'x',\"limit\" := 'bad')", "requires integer"),
                ("SELECT * FROM mcp_tool(server := 'demo',tool := 'search',args := '{\"query\":null}')", "does not allow null"),
                ("SELECT * FROM people p, LATERAL demo.search(query := p.github_username)", "column"),
            ]:
                with self.assertRaisesRegex(Exception, message):
                    c.execute(sql).fetchall()
            self.assertEqual(len(events(self.log)), before)
            with self.assertRaisesRegex(Exception, "fixture failure"):
                c.execute("SELECT * FROM demo.fail()").fetchall()
            calls = [e for e in events(self.log)[before:] if e.get("method") == "tools/call"]
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(Exception, "requires integer"):
                c.execute("SELECT * FROM demo.bad_output()").fetchall()
            self.assertEqual(c.execute("SELECT count(*) FROM demo.search(query := 'recovered')").fetchone(), (2,))

    def test_unavailable_server_is_lazy_and_rollback(self):
        with connect(self.db) as c:
            c.execute("BEGIN")
            c.execute("PRAGMA mcp_register('rolled_back','/no/such/program','[]')")
            c.execute("ROLLBACK")
            self.assertEqual(c.execute("SELECT count(*) FROM mcp_servers()").fetchone(), (1,))
            c.execute("PRAGMA mcp_register('offline','/no/such/program','[]')")
            self.assertEqual(c.execute("SELECT connection_status FROM mcp_servers() WHERE name='offline'").fetchone(), ("disconnected",))
            with self.assertRaises(Exception):
                c.execute("SELECT * FROM mcp_tools('offline')").fetchall()
            c.execute("SET enable_external_access=false")
            with self.assertRaisesRegex(Exception, "enable_external_access"):
                c.execute("SELECT * FROM demo.search(query := 'blocked')").fetchall()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
    else:
        unittest.main()
