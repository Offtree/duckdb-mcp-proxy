import unittest

import test_headers
from test_headers import register
from test_remote import connect


class ProtocolIntegration(unittest.TestCase):
    setUp = test_headers.HeaderIntegration.setUp
    def test_version_override_reopen_json_and_sse(self):
        for suffix in ("", "/sse"):
            name = "legacy_sse" if suffix else "legacy_json"
            with connect(self.db, self.secrets) as c:
                register(c, self.fixture.url + "/legacy" + suffix, name=name,
                         secret_name="", options={"protocol_version": "2025-11-25"})
                c.execute(f"PRAGMA mcp_discover('{name}')")
            with connect(self.db, self.secrets, readonly=True) as c:
                self.assertEqual(c.execute(f"SELECT title FROM {name}.search(query := 'old')").fetchone(),
                                 ("old from remote",))
                with self.assertRaisesRegex(Exception, "additional input"):
                    c.execute(f"SELECT * FROM {name}.search(query := 'input_required')").fetchall()

    def test_version_error_detail_without_retry(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url + "/legacy", secret_name="",
                     options={"protocol_version": "2026-07-28"})
            with self.assertRaisesRegex(Exception, "supported versions: 2025-11-25"):
                c.execute("SELECT * FROM mcp_tools('api')").fetchall()
            self.assertEqual(len(self.fixture.events), 1)
            with self.assertRaisesRegex(Exception, "Unsupported.*option"):
                register(c, self.fixture.url, name="bad", secret_name="",
                          options={"protocol_version": "garbage"})

    def test_automatic_versions_reopen_and_no_tool_replay(self):
        for version in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"):
            for suffix in ("", "/sse"):
                name = "auto_" + version.replace("-", "_") + ("_sse" if suffix else "")
                with connect(self.db, self.secrets) as c:
                    register(c, self.fixture.url + "/legacy/" + version + suffix,
                             name=name, secret_name="")
                    c.execute(f"PRAGMA mcp_discover('{name}')")
                with connect(self.db, self.secrets, readonly=True) as c:
                    self.assertEqual(c.execute(f"SELECT title FROM {name}.search(query := 'auto')").fetchone(),
                                     ("auto from remote",))
                    before = len(self.fixture.events)
                    with self.assertRaisesRegex(Exception, "503"):
                        c.execute(f"SELECT * FROM {name}.search(query := 'http_error')").fetchall()
                    self.assertEqual(len(self.fixture.events), before + 1)
                    self.assertEqual(self.fixture.events[-1]["method"], "tools/call")

    def test_automatic_session_initialization(self):
        for suffix in ("", "/sse"):
            name = "session_sse" if suffix else "session_json"
            with connect(self.db, self.secrets) as c:
                register(c, self.fixture.url + "/legacy/session" + suffix, name=name, secret_name="")
                c.execute(f"PRAGMA mcp_discover('{name}')")
            before = len(self.fixture.events)
            with connect(self.db, self.secrets, readonly=True) as c:
                self.assertEqual(c.execute(f"SELECT title FROM {name}.search(query := 'session')").fetchone(),
                                 ("session from remote",))
            events = self.fixture.events[before:]
            self.assertEqual(sum(e["method"] == "initialize" for e in events), 1)
            self.assertEqual(sum(e["method"] == "notifications/initialized" for e in events), 1)
            self.assertEqual(sum(e["method"] == "tools/call" for e in events), 1)
            self.assertEqual(events[-1]["session_header"], "fixture-session")

    def test_legacy_server_ignoring_version_header(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url + "/legacy/lenient", secret_name="")
            c.execute("PRAGMA mcp_discover('api')")
            self.assertEqual(c.execute("SELECT title FROM api.search(query := 'lenient')").fetchone(),
                             ("lenient from remote",))
        self.assertEqual([e["method"] for e in self.fixture.events],
                         ["tools/list", "tools/list", "tools/call"])
        self.assertNotIn("_meta", self.fixture.events[-1]["params"])

    def test_ordinary_discovery_error_is_not_retried(self):
        with connect(self.db, self.secrets) as c:
            register(c, self.fixture.url + "/discovery-error", secret_name="")
            with self.assertRaisesRegex(Exception, "Invalid discovery parameters"):
                c.execute("SELECT * FROM mcp_tools('api')").fetchall()
        self.assertEqual(len(self.fixture.events), 1)


if __name__ == "__main__":
    unittest.main()
