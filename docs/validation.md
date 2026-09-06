# Verified results

Validated on Linux x86-64 against DuckDB **1.4.4**, using the portable
`EXTENSION_STATIC_BUILD=ON` artifact. Both hosts were unmodified release binaries:
the official GitHub CLI release and the PyPI Python wheel (Python 3.14).
The remote/OAuth component was built with Rust 1.97.1.

## Automated integration suite

```text
$ DUCKDB_CLI=/path/to/duckdb .venv/bin/python -m unittest discover -s tests -v
Ran 14 tests in 8.234s
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
* Transient/revoked refresh errors and preservation of omitted refresh scopes.
* Temporary secret mode, offline login EXPLAIN and external-access enforcement.
* The real `PRAGMA mcp_login` path in the official CLI, using a test browser
  subprocess that follows the fixture's automatic-consent redirect. A second CLI
  process reopens read-only and queries with refreshed credentials, without login.

The HTTP/OAuth fixture runs in a separate process, independent of an embedded
Python host's GIL during PRAGMA expansion. No real account credentials were used.

Rust verification also passed:

```text
cargo clippy --locked --manifest-path remote/Cargo.toml --target-dir build/rust -- -D warnings
Finished `dev` profile
```

## Official CLI demonstration

Executed the rendered `examples/setup.sql` and `examples/reopen.sql` with two
independent CLI invocations opening the same file. The second used `-readonly`.
Both exited successfully. It started with server status `disconnected`, retained
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

## Scope of this evidence

This verifies real stdio and stateless HTTP/OAuth I/O through the native extension;
it is not an interoperability certification for arbitrary MCP/OAuth deployments.
Resource scans, full JSON Schema, cancellation, correlated remote calls and
multi-round-trip execution remain outside the implementation, as described in
the README. The CI workflow repeats the integration suite and CLI demonstration;
only the local runs above have been executed here.
