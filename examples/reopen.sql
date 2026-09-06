-- Open the same database in a new process. Only the extension is loaded.
.bail on
LOAD @EXTENSION@;

SELECT * FROM mcp_servers(); -- Still disconnected
EXPLAIN SELECT * FROM demo.search(query := 'duckdb'); -- Offline schema binding

SELECT id, title, author FROM demo.search(query := 'duckdb');
SELECT p.name, i.title
FROM people p JOIN live_issues i ON p.github_username = i.author
ORDER BY p.name;

SELECT name, connection_status, last_discovery FROM mcp_servers();
