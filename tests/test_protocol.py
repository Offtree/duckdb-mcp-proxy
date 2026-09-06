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
            register(c, self.fixture.url + "/legacy", secret_name="")
            with self.assertRaisesRegex(Exception, "supported versions: 2025-11-25"):
                c.execute("SELECT * FROM mcp_tools('api')").fetchall()
            self.assertEqual(len(self.fixture.events), 1)
            with self.assertRaisesRegex(Exception, "Unsupported.*option"):
                register(c, self.fixture.url, name="bad", secret_name="",
                         options={"protocol_version": "garbage"})


if __name__ == "__main__":
    unittest.main()
