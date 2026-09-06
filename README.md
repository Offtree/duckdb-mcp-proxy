# MCP Context: a durable MCP catalog for DuckDB

An experimental **native extension for stock DuckDB 1.4.4**: C++ for the catalog
and SQL layer, with a linked Rust component for HTTP and OAuth. Register a local
stdio or remote HTTP MCP server once, discover its tools into persistent SQL table macros, and
query them again after restarting DuckDB:

```sql
-- Process A: one-time setup (substitute absolute executable/server paths).
LOAD 'build/extension/mcp_context/mcp_context.duckdb_extension';

BEGIN;
PRAGMA mcp_register('demo', '/usr/bin/python3',
                    '["/absolute/path/to/duckdb-mcp/tests/fixture_server.py"]');
COMMIT;

SELECT * FROM mcp_servers();          -- Offline; does not start the server
SELECT * FROM mcp_tools('demo');      -- Lazy connection and paginated discovery

SELECT * FROM mcp_tool(
    server := 'demo', tool := 'search', args := {'query': 'duckdb'}
);

BEGIN;
PRAGMA mcp_discover('demo');          -- Persist schemas and generate SQL macros
COMMIT;
SELECT id, title, author FROM demo.search(query := 'duckdb');
```

```text
id  title           author
1   duckdb issue 1  alice
2   duckdb issue 2  bob
```

```sql
-- Process B: reopen the same context.duckdb; no per-server setup.
LOAD 'build/extension/mcp_context/mcp_context.duckdb_extension';
SELECT * FROM demo.search(query := 'duckdb');
```

**The extension still needs `LOAD` once per process.** Experimental extensions
cannot add themselves to stock DuckDB's built-in autoload mappings. The database
persists server definitions, discovered schemas and macros; the extension
starts stdio servers lazily and sends self-contained requests to remote servers.
There is no host-side MCP client or registration
code. [Architecture investigation](docs/architecture.md) explains the API choices
and production recommendation.

## Remote servers and OAuth

Remote servers use **MCP 2026-07-28**, with no initialization handshake or MCP
session IDs. OAuth login, token storage and refresh are handled by the extension.

```sql
LOAD 'build/extension/mcp_context/mcp_context.duckdb_extension';

BEGIN;
PRAGMA mcp_register_http('github', 'https://your-server.example/mcp', 'github_oauth');
COMMIT;

PRAGMA mcp_login('github');  -- Opens a browser, waits for login, saves a secret

BEGIN;
PRAGMA mcp_discover('github');
COMMIT;

-- Tool names and arguments come from your server's discovery response.
SELECT * FROM github.search_issues(query := 'is:open');
```

After reopening the database, load the extension and query immediately. Stored
tokens are reused and refreshed when nearing expiry, including refresh-token
rotation. A revoked grant requires login again.

For a public server, omit the secret name:

```sql
PRAGMA mcp_register_http('public_api', 'https://your-public-server.example/mcp');
```

For manual/headless login, use two SQL calls in the same DatabaseInstance:

```sql
CALL mcp_login_begin('github', open_browser := false);
-- Open the returned authorization_url in a browser.
CALL mcp_login_finish('github');
```

The browser callback is bound to `127.0.0.1`; when DuckDB runs on another machine,
forward its callback port to the browser machine. Login expires after three
minutes. Browser-based consent is explicit; ordinary SELECTs never launch it.

Optional public-client configuration is a fourth JSON argument:

```sql
PRAGMA mcp_register_http('service', 'https://your-server.example/mcp', 'service_oauth',
  '{"client_id":"your-pre-registered-public-client", "scopes":["read"], "redirect_port":8765}');
```

Supported options: `client_id`, `client_metadata_url`, `scopes`, `redirect_port`
(default: an available port), and `persistent_secret` (default: true for new
secrets). Without a pre-registered client ID, the SDK selects a configured Client
ID Metadata Document when supported, otherwise dynamic client registration.
This implementation targets native **public clients** using authorization code +
PKCE; it does not expose confidential-client secrets or device-code login.

Credentials are stored as a redacted `mcp_oauth` secret through **DuckDB Secrets
Manager**, not in `_mcp` tables, generated SQL or command arguments. The database
stores a secret reference. Default persistent secrets live under DuckDB's
`secret_directory` outside the database and are unencrypted, protected by file
permissions. A copied database needs the corresponding secret store or a new
login. `mcp_servers()` reports the endpoint, secret reference and local auth status.

See [remote architecture](docs/remote.md) and the dependency-free
[HTTP/OAuth fixture](tests/http_fixture.py). To try a complete browser login
locally, run `python3 tests/http_fixture.py`, then render remote SQL examples using
its printed URL:

```bash
python3 scripts/render_examples.py --remote-url http://127.0.0.1:PORT/mcp
duckdb -unsigned remote_context.duckdb < build/examples/remote_setup.sql
duckdb -unsigned -readonly remote_context.duckdb < build/examples/remote_reopen.sql
```

The fixture automatically grants consent and is only for local testing.

## API keys and header-based authentication

Use an `http` secret for a static bearer token or arbitrary authentication headers.
The extension's `mcp` provider redacts both fields in `duckdb_secrets()`:

```sql
CREATE PERSISTENT SECRET service_auth (
    TYPE http,
    PROVIDER mcp,
    SCOPE 'https://your-server.example/mcp',
    BEARER_TOKEN 'your-personal-access-token'
);

BEGIN;
PRAGMA mcp_register_http('service', 'https://your-server.example/mcp',
                         'service_auth', '{"auth":"headers"}');
COMMIT;

BEGIN;
PRAGMA mcp_discover('service');
COMMIT;

-- Tool names and arguments come from your server.
SELECT * FROM service.search(query := 'duckdb');
```

For custom headers, replace `BEARER_TOKEN` with a map:

```sql
CREATE PERSISTENT SECRET api_key_auth (
    TYPE http,
    PROVIDER mcp,
    SCOPE 'https://your-server.example/mcp',
    EXTRA_HTTP_HEADERS MAP {
        'X-API-Key': 'your-api-key',
        'X-Account': 'your-account'
    }
);
```

Reference `api_key_auth` when registering that server. `Authorization` can also be
supplied as a custom header, but cannot be combined with `BEARER_TOKEN`. Other
custom headers can accompany a bearer token. Header names are case-insensitive;
MCP protocol and HTTP transport headers cannot be overridden.

**No OAuth login is needed.** The registry stores only the secret name and auth
mode. The secret is loaded for every remote operation, so `CREATE OR REPLACE
PERSISTENT SECRET` rotates credentials without registering or discovering again.
Persistent secrets work after reopening, including read-only database opens.
An absent secret fails before sending a request; 401/403 errors ask you to update
the secret and do not trigger OAuth or replay a tool call.

When `auth` is omitted, registration recognizes an existing `http` secret and
persists header mode automatically. Use explicit `{"auth":"headers"}` when the
secret will be created later. HTTP secrets follow DuckDB's URL-prefix `SCOPE`
matching rules; an out-of-scope reference is rejected. `mcp_servers()` reports
`headers_stored` or `secret_missing` locally.

Existing built-in `http` secrets are supported. Prefer `PROVIDER mcp` for new
ones: DuckDB 1.4.4's default `http` provider does not redact bearer tokens or
custom-header values in introspection. `CREATE PERSISTENT SECRET` controls header
secret persistence; the registration option `persistent_secret` applies to OAuth
login only. Default persistent storage remains unencrypted and outside the database.

See [examples/header_auth.sql](examples/header_auth.sql) for a complete template.

## Build

Requirements: Linux, Git, CMake ≥3.18, a C++ compiler, Python 3, and Rust/Cargo
(Rust ≥1.88; tested with 1.97.1). The reused stdio
transport is POSIX-specific; this project is tested on Linux x86-64.

```bash
git clone https://github.com/Offtree/duckdb-mcp-proxy.git
cd duckdb-mcp-proxy
bash scripts/build.sh
```

The script fetches DuckDB v1.4.4 sources into `duckdb/`, then uses DuckDB's
supported out-of-tree CMake extension framework with `EXTENSION_STATIC_BUILD=ON`,
the standard portable distribution mode. This builds the unmodified DuckDB static
library and links the needed code into the loadable extension; it does not
require a custom host executable. The initial build can take several minutes.
Transport sources are
vendored and attributed in [vendor/README.md](vendor/README.md).
Cargo fetches the pinned `rmcp` SDK and dependencies in `remote/Cargo.lock`.
The resulting extension contains the Rust component; end users need no Rust,
Node or Python runtime for remote access or OAuth.

Artifact:

```text
build/extension/mcp_context/mcp_context.duckdb_extension
```

Use **DuckDB 1.4.4 with the matching platform/C++ ABI**. For the unsigned local
build, launch the official CLI with `duckdb -unsigned context.duckdb`.
Compilation against another release is not a compatibility guarantee.
Set `JOBS=4` to change build parallelism (default 2).

### Install the built extension

Start the matching DuckDB CLI with `duckdb -unsigned context.duckdb`, then:

```sql
INSTALL '/absolute/path/to/mcp_context.duckdb_extension';
LOAD mcp_context;
```

`INSTALL` copies the binary into your user-level DuckDB extension directory.
Future processes need `LOAD mcp_context` and unsigned extensions enabled.

Successful [GitHub Actions runs](https://github.com/Offtree/duckdb-mcp-proxy/actions/workflows/test.yml)
publish a tested Linux x86-64 binary as the
`mcp_context-duckdb-v1.4.4-linux-amd64` artifact. Download and extract it to skip
compilation on a compatible Linux system. Artifacts require GitHub sign-in and
expire according to GitHub's retention settings; build locally if your system's
libraries are older than those on the CI runner. This project is not yet in
DuckDB's community extension repository.

## Run the two-process example

With the stock v1.4.4 `duckdb` CLI on PATH:

```bash
# Render absolute paths into SQL scripts under build/examples/.
python3 scripts/render_examples.py

# These are two independent CLI processes opening the same database.
duckdb -unsigned context.duckdb < build/examples/setup.sql
duckdb -unsigned -readonly context.duckdb < build/examples/reopen.sql
```

Start with a fresh `context.duckdb` for this example. The renderer only writes
SQL files; it neither opens DuckDB nor connects to MCP. See the source templates:
[setup.sql](examples/setup.sql), [reopen.sql](examples/reopen.sql).

The reopen example joins live issues with a persistent `people` table and queries
a persisted live view. MCP calls execute through ordinary relational scans:

```sql
SELECT p.name, i.title
FROM people p
JOIN demo.search(query := 'duckdb') i
  ON p.github_username = i.author;
```

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt
DUCKDB_CLI=/path/to/duckdb .venv/bin/python -m unittest discover -s tests -v
```

The integration suite uses fresh **OS processes**, a real database file, the
compiled extension and dependency-free stdio and HTTP/OAuth fixtures. It checks:

* registration without connecting; unavailable servers do not prevent startup;
* generic invocation before persisted discovery;
* tools/list pagination and durable discovery metadata;
* restart with no server setup, read-only reopen, live views and local joins;
* offline EXPLAIN with a discovery snapshot and reusable lazy sessions;
* primitive columns, nested JSON, explicit nulls, 5,000 rows and empty results;
* schema-less fallback and root-array/text JSON output;
* input/output validation, tool errors, no automatic retry, transaction rollback;
* the unsupported correlated-call boundary and `enable_external_access`.
* stateless public/authenticated HTTP, JSON and SSE responses, and no tool replay;
* PKCE, callback state/issuer checks, redaction and resource-bound secrets;
* token refresh, rotation, revoked/transient failures and read-only process restart;
* one-command browser login and reopen in the official CLI (when `DUCKDB_CLI` is set).
* static bearer/custom-header secrets, redaction, rotation, scope checks and restart;
* missing/invalid header credentials, protected headers and no OAuth fallback.

See [verified results](docs/validation.md) for the completed local runs.

`MCP_EXTENSION` can override the artifact path. The static-extension build loads
in the official CLI and Python wheel without exporting host symbols or changing
the host's dynamic-loader flags.

## SQL API and persistence

| API | Behavior |
|---|---|
| `PRAGMA mcp_register(name, command, args_json)` | Create a server row and an owned schema. Stdio only; executable should be absolute, argument array contains strings. No connection. |
| `PRAGMA mcp_register_http(name, url [, secret_name [, options_json]])` | Register a stateless remote endpoint without contacting it. |
| `CREATE [PERSISTENT] SECRET name (TYPE http, PROVIDER mcp, ...)` | Store redacted `bearer_token` and/or `extra_http_headers`; register with `auth: headers` or an existing HTTP secret reference. |
| `PRAGMA mcp_login(name)` | Interactive browser OAuth login; saves credentials as a DuckDB secret. |
| `mcp_login_begin(name, open_browser := false)` / `mcp_login_finish(name)` | Two-step login, exposed as table functions for `CALL`. Login effects occur at execution, not bind. |
| `mcp_servers()` | Name, transport, command, args, connection status, discovery timestamp, URL, secret reference and auth status. HTTP status is `stateless`; auth status reflects locally stored credentials, not an online validity check. |
| `mcp_tools('name')` | Live discovery: tool name, description, input/output JSON schemas, full definition. Does not persist discovery. |
| `PRAGMA mcp_discover('name')` | Create macros and persist schemas for all discovered tools. First-time materialization; existing macro names cause an error. |
| `mcp_tool(server := ..., tool := ..., args := ...)` | Native relational scan. Arguments accept a DuckDB STRUCT or JSON object string. |
| `demo.search(query := ...)` | Durable table macro generated during discovery; same native scan. |
| `_mcp.servers`, `_mcp.tools`, `_mcp.http_servers` | Ordinary durable metadata tables in the primary database. Backed up with the database. Existing stdio databases continue to work; HTTP metadata is added on registration. |

Run registration and discovery in separate, committed transactions, as above.
The PRAGMAs expand into multiple SQL statements; explicit transactions make
their catalog changes atomic. In embedded clients, send statements sequentially
and commit registration **before** sending discovery. DuckDB expands PRAGMAs
before execution of an entire submitted SQL batch. Metadata reads use a separate
connection and see committed state, not the caller's uncommitted changes.

Registration owns a new schema rather than merging with an existing user schema.
Use names consistently in the generic APIs (server strings are case-sensitive).
Generated identifiers are SQL-quoted. Reserved argument names need quoting:

```sql
SELECT * FROM demo.search(query := 'duckdb', "limit" := 10);
```

Discovery snapshots are intentionally stable. `mcp_tools` shows live definitions;
it does not silently change columns of existing views. Automatic schema refresh,
drop/alter server APIs and schema migration are future work. For this experiment,
remove owned macros and their `_mcp.tools` rows in a committed transaction before
rediscovery; do not change server definitions while queries are running.

## Mapping rules

* Input schemas validate string, integer, number, boolean, object, array,
  required fields, `additionalProperties: false`, and nullable type arrays.
  Nested object properties and array elements are validated recursively.
* Generated macro parameters default to SQL NULL; NULL means **omitted**.
  Required missing inputs fail at bind. Macros are not catalog-level typed
  native function declarations; the native binder validates their arguments.
* To pass explicit JSON null, use generic JSON arguments:
  `args := '{"nullable":null}'`. JSON Schema defaults are not injected.
* Output `string/integer/number/boolean` maps to
  `VARCHAR/BIGINT/DOUBLE/BOOLEAN`; nested arrays/objects and unsupported schema
  types map to JSON. Missing optional fields become SQL NULL.
* A schema describing an object produces one row. A root array of objects
  produces many rows. An object with exactly one array property (e.g. `items`)
  is unwrapped into rows. Wider envelopes are left as object columns.
* Prefer MCP `structuredContent`; otherwise parse a single text content block
  as JSON. Unstructured/multiple-block content remains JSON.
* No usable object row schema → one `result JSON` column, preserving the payload.
  This includes schema-less arrays: the extension never invokes a tool during
  binding merely to guess columns.
* `$ref`, combinators, formats, numeric bounds, enums and full JSON Schema
  validation are outside this slice. Full responses are materialized in memory.

## Current boundaries

* **Remote protocol:** targets stateless MCP 2026-07-28. Legacy session-based HTTP,
  multi-round-trip input, subscriptions, tasks, resource scans and prompts are
  not implemented. JSON and finite SSE responses are supported; remote responses
  are bounded to 32 MiB. There is no fallback to legacy HTTP sessions.
* **No custom `CREATE MCP SERVER` grammar.** PRAGMA registration is the supported
  SQL alternative. Namespaced tool syntax works through persistent macros after
  discovery, not first-reference synthesis of arbitrary missing functions.
* **Constant parameters only.** DuckDB's table-function binder rejects
  `demo.search(query := p.github_username)`. Fetch a live relation with constant
  arguments and join/filter locally; a per-person correlated RPC is not supported.
* **Primary database metadata only.** Attached-database routing, transactional
  metadata snapshot integration and concurrent metadata mutation are deferred.
* One serialized runtime per DatabaseInstance, shared across its connections.
  Stdio processes end when that instance closes. Remote HTTP connections can be
  reused by the HTTP library; no remote MCP session is established. There is no
  result cache. A failed tool request is not retried. Stdio initialization
  requests MCP 2025-06-18 (structured tool output), with
  2025-03-26 and 2024-11-05 negotiation accepted for older servers.
* Tool invocation happens in scan initialization, not bind/EXPLAIN. SQL may prune
  an unused scan; each initialized scan invokes once. External effects are not
  rolled back by SQL. Discovery may contact the server during bind if no snapshot
  exists. The transport has a 30-second default timeout but no DuckDB cancellation
  integration, bidirectional server-request handler or notification dispatcher.
* **Credentials:** OAuth uses DuckDB Secrets Manager. Credential operations are
  serialized in-process; concurrent independent processes sharing the same
  rotating OAuth secret are not supported. Secret changes are outside database
  transaction rollback. Stdio authentication remains server-managed; its transport
  passes only a minimal environment allowlist. Keep tokens out of command arguments.

## Recommendation

The experiment supports the macro-based architecture: durable definitions and
schema snapshots plus a lazy native scan achieve natural SQL without a DuckDB
fork. Native remote HTTP and SDK-backed OAuth extend the same catalog boundary.
Next, add schema refresh/versioning, cancellation, resource scans, multi-round-trip
execution and opt-in materialization. A custom
attached catalog is justified if truly automatic resolution of previously
undiscovered `server.tool(...)` names becomes essential.
