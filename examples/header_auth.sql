-- Template: replace endpoint, secret values, and tool name with your server's.
-- Execute statements sequentially; registration must commit before discovery.
LOAD 'build/extension/mcp_context/mcp_context.duckdb_extension';

CREATE PERSISTENT SECRET service_headers (
    TYPE http,
    PROVIDER mcp,
    SCOPE 'https://your-server.example/mcp',
    EXTRA_HTTP_HEADERS MAP {
        'X-API-Key': 'replace-with-your-api-key'
    }
);
-- For a personal access token, use BEARER_TOKEN 'your-token' instead of the map.

BEGIN;
PRAGMA mcp_register_http('service', 'https://your-server.example/mcp',
                         'service_headers', '{"auth":"headers"}');
COMMIT;

BEGIN;
PRAGMA mcp_discover('service');
COMMIT;

SELECT name, secret_name, auth_status FROM mcp_servers();
SELECT * FROM service.search(query := 'duckdb');

-- In a later process, load the extension and query service.search directly.
-- To rotate credentials, CREATE OR REPLACE PERSISTENT SECRET service_headers (...).
