"""Header secrets use the same persisted catalog and native scan as OAuth."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from http_fixture import FixtureProcess
from test_remote import connect
from test_integration import quote


def secret(c, name="api_auth", bearer=None, headers=None, scope=None, persistent=False, provider="mcp"):
    options = ["TYPE http"]
    if provider:
        options.append("PROVIDER " + provider)
    if bearer is not None:
        options.append("BEARER_TOKEN " + quote(bearer))
    if headers is not None:
        keys = ",".join(quote(key) for key in headers)
        values = ",".join(quote(value) for value in headers.values())
        options.append(f"EXTRA_HTTP_HEADERS MAP([{keys}],[{values}])")
    if scope:
        options.append("SCOPE " + quote(scope))
    c.execute(f"CREATE OR REPLACE {'PERSISTENT ' if persistent else ''}SECRET {name} (" + ",".join(options) + ")")


def register(c, url, name="api", secret_name="api_auth", options=None):
    extra = "," + quote(json.dumps(options)) if options is not None else ""
    c.execute(f"PRAGMA mcp_register_http({quote(name)},{quote(url)},{quote(secret_name)}{extra})")


def worker(mode, db, secrets, url):
    with connect(db, secrets, readonly=mode == "reopen") as c:
        if mode == "setup":
            secret(c, bearer="fixture-static-bearer", persistent=True, scope=url)
            register(c, url)
            c.execute("PRAGMA mcp_discover('api')")
        assert c.execute("SELECT title FROM api.search(query := 'persisted')").fetchone() == ("persisted from remote",)
        assert c.execute("SELECT auth_status FROM mcp_servers()").fetchone() == ("headers_stored",)


class HeaderIntegration(unittest.TestCase):
    def setUp(self):
        root = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-headers-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "context.duckdb"
        self.secrets = Path(self.temp.name) / "secrets"
        self.fixture = FixtureProcess()
        self.addCleanup(self.fixture.close)

    def test_bearer_persists_across_processes_without_oauth(self):
        for mode in ("setup", "reopen"):
            subprocess.run([sys.executable, __file__, "--worker", mode, str(self.db), str(self.secrets),
                            self.fixture.url + "/bearer"], check=True, timeout=30)
        self.assertTrue(all(e["event"] == "mcp" for e in self.fixture.events))
        self.assertNotIn(b"fixture-static-bearer", self.db.read_bytes())

    def test_custom_headers_redaction_and_rotation(self):
        with connect(self.db, self.secrets) as c:
            headers = {"X-API-Key": "fixture-header-key", "X-Account": "acme"}
            secret(c, headers=headers)
            register(c, self.fixture.url + "/headers")
            self.assertEqual(self.fixture.events, [])
            c.execute("PRAGMA mcp_discover('api')")
            self.assertEqual(c.execute("SELECT count(*) FROM api.search(query := 'key')").fetchone(), (1,))
            self.assertNotIn("fixture-header-key", str(c.execute("SELECT * FROM duckdb_secrets()").fetchall()))
            self.assertNotIn("fixture-header-key", str(c.execute("SELECT * FROM _mcp.http_servers").fetchall()))
            self.assertFalse(self.fixture.events[-1]["rotated_header"])
            headers["X-API-Key"] = "fixture-header-key-rotated"
            secret(c, headers=headers)
            c.execute("SELECT * FROM api.search(query := 'rotated')").fetchall()
            self.assertTrue(self.fixture.events[-1]["rotated_header"])
            with self.assertRaisesRegex(Exception, "OAuth login is not applicable"):
                c.execute("CALL mcp_login_begin('api',open_browser := false)").fetchall()

    def test_existing_default_http_secret_and_raw_authorization(self):
        with connect(self.db, self.secrets) as c:
            secret(c, bearer="fixture-static-bearer", provider=None)
            register(c, self.fixture.url + "/bearer")
            self.assertEqual(c.execute("SELECT count(*) FROM mcp_tools('api')").fetchone(), (1,))
            secret(c, headers={"aUtHoRiZaTiOn": "Basic fixture-basic"})
            register(c, self.fixture.url + "/authorization", name="basic_api")
            self.assertEqual(c.execute("SELECT count(*) FROM mcp_tools('basic_api')").fetchone(), (1,))

    def test_missing_dropped_and_scoped_secrets_fail_before_network(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url + "/bearer", options={"auth": "headers"})
            self.assertEqual(c.execute("SELECT auth_status FROM mcp_servers()").fetchone(), ("secret_missing",))
            with self.assertRaisesRegex(Exception, "secret not found"):
                c.execute("SELECT * FROM mcp_tools('api')").fetchall()
            self.assertEqual(self.fixture.events, [])
            secret(c, bearer="fixture-static-bearer", scope=self.fixture.base + "/elsewhere")
            with self.assertRaisesRegex(Exception, "scope does not match"):
                c.execute("SELECT * FROM mcp_tools('api')").fetchall()
            self.assertEqual(self.fixture.events, [])
            c.execute("DROP SECRET api_auth")
            with self.assertRaisesRegex(Exception, "secret not found"):
                c.execute("SELECT * FROM mcp_tools('api')").fetchall()

    def test_invalid_headers_conflicts_and_empty_secrets(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url + "/headers", options={"auth": "headers"})
            cases = [
                ({"X-API-Key": "private-value\r\nInjected: bad"}, None, "Invalid HTTP secret header value"),
                ({"Mcp-Session-Id": "private-value"}, None, "cannot override"),
                ({"Content-Type": "private-value"}, None, "cannot override"),
                ({"Host": "private-value"}, None, "cannot override"),
                ({"Authorization": "private-value"}, "private-bearer", "not both"),
                ({"X-API-Key": "private-value", "x-api-key": "other"}, None, "Duplicate"),
                (None, None, "has no bearer_token"),
            ]
            for headers, bearer, message in cases:
                secret(c, headers=headers, bearer=bearer)
                with self.assertRaisesRegex(Exception, message) as error:
                    c.execute("SELECT * FROM mcp_tools('api')").fetchall()
                self.assertNotIn("private-value", str(error.exception))
                self.assertNotIn("private-bearer", str(error.exception))
            self.assertEqual(self.fixture.events, [])

    def test_rejected_headers_do_not_prompt_oauth_or_retry(self):
        with connect(self.db, self.secrets) as c:
            secret(c, bearer="wrong-private-value")
            register(c, self.fixture.url + "/bearer")
            with self.assertRaisesRegex(Exception, "HTTP authentication rejected") as error:
                c.execute("SELECT * FROM mcp_tools('api')").fetchall()
            self.assertNotIn("wrong-private-value", str(error.exception))
            self.assertEqual(len(self.fixture.events), 1)
            self.assertEqual(self.fixture.events[0]["event"], "mcp")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5])
    else:
        unittest.main()
