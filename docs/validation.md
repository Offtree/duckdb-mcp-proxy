# Verified results

Validated on Linux x86-64 against DuckDB **1.5.5**, using the portable
`EXTENSION_STATIC_BUILD=ON` artifact. Both hosts were unmodified release binaries:
the official GitHub CLI release (d8cdaa33) and the PyPI Python wheel (Python 3.14).
The remote/OAuth component was built with Rust 1.97.1. The extension was initially
validated on DuckDB 1.4.4; the 1.5.5 upgrade rebuilt from fresh v1.5.5 sources and
re-ran everything below.

## Automated integration suite

```text
$ DUCKDB_CLI=/path/to/duckdb .venv/bin/python -m unittest discover -s tests -v
Ran 35 tests in 15.958s
OK
```

Five stdio test groups cover process restart/read-only composition, schema mapping and
chunked invocation, errors/no retries, rollback/offline registration, and runtime
sharing within a database plus isolation across databases. Each group's setup
uses a separate process to create the database and persisted discovery macros.
The restart group also verifies the documented failure without `LOAD`.

Nine remote/OAuth tests additionally verify:

* Stateless public HTTP with no initialization or session headers.
* OAuth PKCE, client/redirect/resource binding and state/issuer rejection.
* Persistent credentials, read-only process restart and rotated refresh tokens.
* Redaction in `duckdb_secrets()` and no tokens in the `.duckdb` file.
* JSON and SSE responses, HTTP errors and no tool-request replay.
* Automatic older-version selection, initialization/session IDs, and compatibility
  checks before tool execution after reopening; explicit version pins still work.
* Transient/revoked refresh errors and preservation of omitted refresh scopes.
* Temporary secret mode, offline login EXPLAIN and external-access enforcement.
* The real `PRAGMA mcp_login` path in the official CLI, using a test browser
  subprocess that follows the fixture's automatic-consent redirect. A second CLI
  process reopens read-only and queries with refreshed credentials, without login.

The HTTP/OAuth fixture runs in a separate process, independent of an embedded
Python host's GIL during PRAGMA expansion. No real account credentials were used.

Six header-auth tests also passed:

* Static bearer credentials persist across OS processes without OAuth.
* Custom headers are redacted, and replacing the secret rotates the next request.
* Existing default-provider HTTP secrets and raw Authorization headers work.
* Missing, dropped and out-of-scope secrets fail before network access.
* Invalid headers, protocol overrides, duplicate names and conflicting
  Authorization values are rejected without including secret values in errors.
* Rejected credentials produce one request and no OAuth prompt or retry.

Two protocol-compatibility tests cover persisted version overrides on reopen,
older JSON/SSE responses without `resultType`, empty SSE priming events,
unsupported-version diagnostics without replay, and invalid option rejection.
Live validation on 2026-09-06 successfully discovered three tools from
`https://mcp.firecrawl.dev/v2/mcp` using `protocol_version: 2025-11-25` through
the rebuilt extension. The authenticated Firecrawl endpoint was not exercised.

Rust verification also passed:

```text
cargo clippy --locked --manifest-path remote/Cargo.toml --target-dir build/rust -- -D warnings
Finished `dev` profile
```

## Official CLI demonstration

Executed the rendered `examples/setup.sql` and `examples/reopen.sql` with two
independent CLI invocations opening the same file. The second used `-readonly`
and both passed `-f` (DuckDB 1.5's friendly CLI suppresses query output for
piped stdin). Both exited successfully. It started with server status `disconnected`, retained
the discovery timestamp, and produced:

```text
id  title           author
1   duckdb issue 1  alice
2   duckdb issue 2  bob

name           title
Alice Example  duckdb issue 1
Bob Example    duckdb issue 2
```

The join in the second process read the persistent `people` table and `live_issues`
view; the latter expands a persisted namespaced MCP table macro. Status afterward
was `connected`. No server registration or discovery PRAGMA was run on reopen.

## DAV-8 scalar chaining (2026-09-06)

The full integration suite passed: **35 tests**, including nine scalar tests in
`tests/test_scalar.py`. These cover dynamic server/tool/JSON arguments, recursive
CTEs, materialized response reuse, 2,050 selected rows across vector chunks,
NULL and empty inputs, offline EXPLAIN/PREPARE, runtime external-access checks,
input validation, and failed-call counts. Cross-transport chaining was verified
against the stdio and HTTP/OAuth fixtures after reopening a read-only database.
The stock DuckDB 1.5.5 CLI also executed correlated scalar calls successfully.

```bash
MCP_EXTENSION="$PWD/build/extension/mcp_context/mcp_context.duckdb_extension" \
  DUCKDB_CLI=$(command -v duckdb) \
  .venv/bin/python -m unittest discover -s tests -v
```

## DuckDB 1.5.5 upgrade (2026-09-06)

Rebuilt against the pinned DuckDB **v1.5.5** checkout (d8cdaa33) with a fresh
`build/` directory and re-ran the full 35-test suite above against the 1.5.5
CLI and PyPI wheel. Only one source change was required:

* `enable_external_access` is no longer a `DBConfigOptions` field in 1.5;
  `CheckExternal` now reads it through
  `Settings::Get<EnableExternalAccessSetting>(DBConfig::GetConfig(context))`.

Behavior changes verified on 1.5.5:

* The default `http` secret provider now redacts `bearer_token` in
  `duckdb_secrets()` introspection, but still shows `extra_http_headers`
  values; `PROVIDER mcp` redacts both (updated README/remote docs).
* The friendly CLI suppresses query output for piped stdin scripts, so the
  two-process example and CI use `-f <script>` instead of `< <script>`;
  errors still fail the process (nonzero exit with `.bail on`).
* `cargo clippy --locked --manifest-path remote/Cargo.toml -- -D warnings`
  passes; three lints introduced by the scalar PR were fixed there.

## Scope of this evidence

This verifies real stdio and stateless HTTP/OAuth I/O through the native extension;
it is not an interoperability certification for arbitrary MCP/OAuth deployments.
Resource scans, full JSON Schema, cancellation and
multi-round-trip execution remain outside the implementation, as described in
the README. The CI workflow repeats the integration suite and CLI demonstration;
only the local runs above have been executed here.
