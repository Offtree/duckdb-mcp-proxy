-- Render with scripts/render_examples.py --remote-url URL.
.bail on
LOAD @EXTENSION@;

BEGIN;
PRAGMA mcp_register_http('remote_demo', @REMOTE_URL@, 'remote_demo_oauth');
COMMIT;

PRAGMA mcp_login('remote_demo');

BEGIN;
PRAGMA mcp_discover('remote_demo');
COMMIT;

SELECT name, transport, url, auth_status FROM mcp_servers();
-- These tool names/columns are supplied by tests/http_fixture.py.
SELECT * FROM remote_demo.search(query := 'duckdb');
CREATE TABLE remote_people AS SELECT 'Alice Example' AS name, 'alice' AS username;
