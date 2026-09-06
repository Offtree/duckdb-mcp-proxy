import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen

from http_fixture import FixtureProcess
from test_integration import EXTENSION, quote


def connect(db, secrets, readonly=False):
    import duckdb
    c = duckdb.connect(str(db), read_only=readonly, config={"allow_unsigned_extensions": "true"})
    c.execute("LOAD " + quote(EXTENSION))
    c.execute("SET secret_directory=" + quote(secrets))
    return c


def register(c, url, name="remote", options=None):
    c.execute(f"PRAGMA mcp_register_http({quote(name)}, {quote(url)}, {quote(name + '_oauth')}, "
              f"{quote(json.dumps(options or {}))})")


def login(c, name="remote"):
    start = c.execute(f"CALL mcp_login_begin({quote(name)}, open_browser := false)").fetchone()
    assert start[2] is False
    results = []

    def browser():
        try:
            with urlopen(start[0], timeout=15) as response:
                results.append(response.status)
        except Exception as exc:
            results.append(exc)

    thread = threading.Thread(target=browser, daemon=True)
    thread.start()
    try:
        c.execute(f"CALL mcp_login_finish({quote(name)})").fetchall()
    finally:
        thread.join(timeout=20)
    assert results == [200], results


def worker(mode, db, secrets, url):
    with connect(db, secrets, readonly=mode == "reopen") as c:
        if mode == "setup":
            register(c, url)
            login(c)
            c.execute("PRAGMA mcp_discover('remote')")
            assert c.execute("SELECT title FROM remote.search(query := 'first')").fetchone() == ("first from remote",)
            c.execute("CREATE TABLE people AS SELECT 'Alice' AS name, 'alice' AS username")
        else:
            assert c.execute("SELECT connection_status FROM mcp_servers()").fetchone() == ("stateless",)
            c.execute("EXPLAIN SELECT * FROM remote.search(query := 'offline')").fetchall()
            assert c.execute("SELECT p.name, i.title FROM people p JOIN remote.search(query := 'reopened') i "
                             "ON p.username=i.author").fetchone() == ("Alice", "reopened from remote")


class RemoteIntegration(unittest.TestCase):
    def setUp(self):
        root = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-oauth-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "context.duckdb"
        self.secrets = Path(self.temp.name) / "secrets"
        self.fixture = FixtureProcess()
        self.addCleanup(self.fixture.close)

    def test_oauth_persists_and_refreshes_after_process_restart(self):
        command = [sys.executable, __file__, "--worker"]
        subprocess.run(command + ["setup", str(self.db), str(self.secrets), self.fixture.url], check=True, timeout=45)
        before = len(self.fixture.events)
        subprocess.run(command + ["reopen", str(self.db), str(self.secrets), self.fixture.url], check=True, timeout=45)
        new = self.fixture.events[before:]
        self.assertEqual(sum(e.get("event") == "authorize" for e in new), 0)
        self.assertEqual(sum(e.get("event") == "register" for e in new), 0)
        self.assertEqual(sum(e.get("grant") == "refresh_token" for e in new), 1)
        self.assertEqual([e["method"] for e in new if e["event"] == "mcp"], ["tools/list", "tools/call"])
        self.assertTrue(all(e.get("session_header") is None for e in self.fixture.events))
        self.assertTrue(list(self.secrets.glob("*.duckdb_secret")))
        self.assertNotIn(b"fixture-access-", self.db.read_bytes())
        self.assertNotIn(b"fixture-refresh-", self.db.read_bytes())

    def test_public_http_needs_no_oauth_or_handshake(self):
        with connect(self.db, self.secrets) as c:
            c.execute(f"PRAGMA mcp_register_http('public_api',{quote(self.fixture.base + '/public')})")
            self.assertEqual(self.fixture.events, [])
            c.execute("PRAGMA mcp_discover('public_api')")
            self.assertEqual(c.execute("SELECT title FROM public_api.search(query := 'public')").fetchone(), ("public from remote",))
            self.assertTrue(all(e["event"] == "mcp" for e in self.fixture.events))

    def test_secret_redaction_sse_and_no_tool_replay(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url + "/sse")
            with self.assertRaisesRegex(Exception, "OAuth login required"):
                c.execute("SELECT * FROM mcp_tools('remote')").fetchall()
            login(c)
            rows = c.execute("SELECT * FROM duckdb_secrets() WHERE type='mcp_oauth'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertNotIn("fixture-access", str(rows))
            self.assertNotIn("fixture-refresh", str(rows))
            c.execute("PRAGMA mcp_discover('remote')")
            self.assertEqual(c.execute("SELECT title FROM remote.search(query := 'sse')").fetchone(), ("sse from remote",))
            before = len(self.fixture.events)
            with self.assertRaisesRegex(Exception, "503") as error:
                c.execute("SELECT * FROM remote.search(query := 'http_error')").fetchall()
            self.assertNotIn("fixture-access", str(error.exception))
            self.assertEqual(sum(e.get("method") == "tools/call" for e in self.fixture.events[before:]), 1)
            with self.assertRaisesRegex(Exception, "additional input"):
                c.execute("SELECT * FROM remote.search(query := 'input_required')").fetchall()

    def test_refresh_errors_preserve_secret_and_do_not_call_tool(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url)
            login(c)
            c.execute("PRAGMA mcp_discover('remote')")
            self.fixture.transient_refresh = True
            before = len(self.fixture.events)
            with self.assertRaisesRegex(Exception, "refresh failed"):
                c.execute("SELECT * FROM remote.search(query := 'blocked')").fetchall()
            self.assertFalse(any(e.get("method") == "tools/call" for e in self.fixture.events[before:]))
            self.fixture.transient_refresh = False
            self.assertEqual(c.execute("SELECT count(*) FROM remote.search(query := 'retry')").fetchone(), (1,))
            self.fixture.reject_refresh = True
            with self.assertRaisesRegex(Exception, "login required"):
                c.execute("SELECT * FROM remote.search(query := 'revoked')").fetchall()

    def test_wrong_callback_state_and_issuer_do_not_save_credentials(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url)
            self.fixture.callback_issuer = "https://wrong.example"
            with self.assertRaisesRegex(Exception, "issuer validation"):
                login(c)
            self.fixture.callback_issuer = None
            self.fixture.wrong_state = True
            with self.assertRaisesRegex(Exception, "authorization failed"):
                login(c)
            self.assertEqual(c.execute("SELECT count(*) FROM duckdb_secrets() WHERE type='mcp_oauth'").fetchone(), (0,))
            self.assertFalse(any(e.get("grant") == "authorization_code" for e in self.fixture.events))

    def test_secret_binding_and_external_access(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url)
            login(c)
            c.execute(f"PRAGMA mcp_register_http('other',{quote(self.fixture.url + '/other')},'remote_oauth')")
            with self.assertRaisesRegex(Exception, "different MCP resource"):
                c.execute("SELECT * FROM mcp_tools('other')").fetchall()
            c.execute("SET enable_external_access=false")
            with self.assertRaisesRegex(Exception, "enable_external_access"):
                c.execute("CALL mcp_login_begin('remote',open_browser := false)").fetchall()

    def test_temporary_secret_and_offline_login_bind(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url, options={"persistent_secret": False})
            c.execute("EXPLAIN SELECT * FROM mcp_login_begin('remote',open_browser := false)").fetchall()
            self.assertEqual(self.fixture.events, [])
            login(c)
            self.assertEqual(c.execute("SELECT persistent FROM duckdb_secrets() WHERE type='mcp_oauth'").fetchone(), (False,))
        with connect(self.db, self.secrets) as c:
            self.assertEqual(c.execute("SELECT auth_status FROM mcp_servers()").fetchone(), ("login_required",))

    def test_refresh_keeps_scopes_when_provider_omits_them(self):
        self.fixture.omit_refresh_scope = True
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url)
            login(c)
            c.execute("SELECT * FROM mcp_tools('remote')").fetchall()
            c.execute("SELECT * FROM mcp_tools('remote')").fetchall()
        refreshes = [e for e in self.fixture.events if e.get("grant") == "refresh_token"]
        self.assertEqual(len(refreshes), 2)
        self.assertTrue(all("issues:read" in e["scope"].split() for e in refreshes))

    def test_interactive_pragma_with_stock_cli(self):
        cli = os.environ.get("DUCKDB_CLI") or shutil.which("duckdb")
        if not cli:
            self.skipTest("Set DUCKDB_CLI to the stock DuckDB 1.4.4 executable")
        browser = Path(__file__).with_name("browser_fixture.py")
        env = {**os.environ, "BROWSER": f"{sys.executable} {browser} %s"}
        setup = f""".bail on
LOAD {quote(EXTENSION)};
SET secret_directory={quote(self.secrets)};
PRAGMA mcp_register_http('remote',{quote(self.fixture.url)},'cli_oauth');
PRAGMA mcp_login('remote');
PRAGMA mcp_discover('remote');
SELECT title FROM remote.search(query := 'cli');
"""
        result = subprocess.run([cli, "-unsigned", str(self.db)], input=setup, text=True,
                                env=env, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cli from remote", result.stdout)
        before = len(self.fixture.events)
        reopen = f""".bail on
LOAD {quote(EXTENSION)};
SET secret_directory={quote(self.secrets)};
SELECT title FROM remote.search(query := 'cli-reopened');
"""
        result = subprocess.run([cli, "-unsigned", "-readonly", str(self.db)], input=reopen,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cli-reopened from remote", result.stdout)
        self.assertFalse(any(e["event"] == "authorize" for e in self.fixture.events[before:]))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
    else:
        unittest.main()
