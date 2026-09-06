-- Render with scripts/render_examples.py; run on a fresh context.duckdb.
.bail on
LOAD @EXTENSION@;

BEGIN;
PRAGMA mcp_register('demo', @PYTHON@, @FIXTURE_ARGS@);
COMMIT;

SELECT * FROM mcp_servers();
SELECT name, description, input_schema, output_schema FROM mcp_tools('demo');
SELECT * FROM mcp_tool(server := 'demo', tool := 'search', args := {'query':'duckdb'});

BEGIN;
PRAGMA mcp_discover('demo');
COMMIT;

SELECT id, title, author FROM demo.search(query := 'duckdb');

CREATE TABLE people(name VARCHAR, github_username VARCHAR);
INSERT INTO people VALUES ('Alice Example','alice'), ('Bob Example','bob');
CREATE VIEW live_issues AS SELECT * FROM demo.search(query := 'duckdb');

SELECT p.name, i.title
FROM people p JOIN demo.search(query := 'duckdb') i
ON p.github_username = i.author
ORDER BY p.name;
