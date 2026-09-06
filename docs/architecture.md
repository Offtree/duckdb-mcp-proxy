# Architecture investigation and decision

Target: stock DuckDB **v1.4.4**, native loadable extension `mcp_context`.
This is an experiment, not a DuckDB fork. Research was done against the pinned
DuckDB sources and `teaguesterling/duckdb_mcp` before implementation.

This document records the initial catalog/stdio investigation. The subsequent
[remote implementation](remote.md) adds stateless MCP 2026-07-28 and SDK-backed
OAuth in a linked Rust component, with a working DuckDB Secrets Manager adapter.
References below to deferred HTTP/Secrets describe the first milestone.

## Mechanisms investigated

| Mechanism | Finding / decision |
|---|---|
| Community extensions | Ordinary out-of-tree CMake projects use `build_loadable_extension` and `DUCKDB_CPP_EXTENSION_ENTRY`. `EXTENSION_STATIC_BUILD=ON` links DuckDB's static library into the plugin, the portable distribution mode needed by the official CLI and RTLD_LOCAL Python hosts. Community distribution adds build metadata/signing; it does not make arbitrary extension code persistent. C++ ABI is version-specific. |
| Table functions | `TableFunction` has bind, global initialization and scan callbacks. Bind declares columns; initialization can perform one remote invocation, and scan emits chunks. Tool calls must not happen during bind/EXPLAIN. Discovery can happen during bind. |
| Runtime function registration | `Catalog::CreateTableFunction` and `SchemaCatalogEntry::CreateTableFunction` accept runtime definitions. Functions need not be known at compile time. Native callback pointers, however, are not durable SQL definitions that survive restart. |
| Extension-backed catalogs | `StorageExtension` can supply an attached `Catalog`, custom `SchemaCatalogEntry`, lookup and transaction manager. Upstream MCP already does this for `ATTACH ... (TYPE mcp)`. Its schema lookup currently only searches stored entries, and creation of table functions throws. A custom catalog could synthesize function entries during lookup. Ordinary persistent DuckDB schemas cannot individually be replaced with custom implementations. |
| Replacement scans | Replace unresolved table references with another table reference/function. They are not a general hook for resolving missing `schema.function(...)` calls. |
| Durable metadata | Normal tables in a reserved schema give transactions, checkpointing, backup and inspectability. Persist definitions and discovery snapshots, not connections or native function pointers. |
| Persistent macros | SQL table macros survive restart and preserve `demo.search(query := 'duckdb')`. Generate them during explicit discovery; expand to one generic native table function. This is the smallest durable namespaced solution. |
| Parser extension | Custom `CREATE MCP SERVER` is possible through parser/planner extensions but unnecessary for this experiment. A query-producing PRAGMA expands registration into ordinary transactional SQL. |
| Secrets Manager | `SecretManager`, `SecretType`, `CreateSecretFunction` and `KeyValueSecret` support custom providers and redacted keys. A production server row should reference a secret name. Default persistent secrets live in the user's DuckDB secret directory, outside the database, and are not encrypted. This slice uses stdio with server-owned authentication (e.g. the server's own configuration under HOME) and stores no dedicated credential values. The reused transport passes only a minimal environment, not arbitrary parent tokens. |
| Autoload | DuckDB's known extension/function mappings drive autoload. A local experimental extension cannot install its own mapping into stock DuckDB or persist a `LOAD` action inside a database. Reopening requires `LOAD mcp_context` (or the binary path) once, but no per-server host setup, reconnection or discovery. Persisted macros do not auto-load unknown native functions. |

## Chosen vertical slice

* Registration PRAGMA writes `_mcp.servers` in the current database.
* `mcp_servers()` reads committed metadata without contacting servers.
* `mcp_tools('demo')` lazily starts stdio, performs initialize/initialized and
  paginated tools/list, and exposes the schemas.
* `PRAGMA mcp_discover('demo')` persists tool snapshots and generated table macros.
* `mcp_tool(server := ..., tool := ..., args := {...})` binds output columns
  from discovery and invokes tools/call once in global initialization.
* Transport sessions are shared by the extension's functions, scoped to a
  DatabaseInstance, and serialized. Nothing connects at extension load time.
* Metadata lookups use a separate connection to the same DatabaseInstance:
  registration/discovery must be committed before querying. This slice targets
  the primary database; attached-database metadata routing is deferred.

## Transport reuse

Vendor the Apache-2.0 stdio transport, message codec, JSON helpers and logger from
`duckdb_mcp`, retaining attribution and pinning the revision. Its full extension
adds a server runtime, ephemeral ATTACH catalogs and function names that overlap
this project, so depending on the entire extension is less useful than reusing
the transport sources. Its HTTP client currently performs a ping before
initialization, advertises only application/json, and does not implement MCP
session IDs/SSE. Do not advertise that as a complete Streamable HTTP client.
HTTP is deferred; stdio provides the working architectural experiment. The
message codec has a small documented adaptation to honor explicit JSON params
before its legacy method-specific builders (see `vendor/README.md`).

## Schema and execution contract

Common primitive inputs are validated against discovered JSON Schema. Objects
and arrays are accepted, with nested output values represented as JSON. A root
object becomes one row; an array of objects becomes multiple rows. A single
array property in a wrapper object is a useful MCP-compatible row envelope.
Without an output schema the result remains JSON, preserving a stable bind-time
schema without executing a potentially mutating tool to infer columns.

Generated macros have named default arguments and validate types in the native
binder. NULL optional macro arguments are omitted. Generic JSON arguments allow
explicit JSON null. SQL table function arguments must be bind-time constants;
correlated/lateral tool calls are not supported. Uncorrelated live results join
normally with local tables. Calls are materialized once per scan, with no
automatic retry of tools/call and no predicate pushdown. SQL rollback cannot
undo external effects.

## Source references

* [Community description](https://github.com/duckdb/community-extensions/blob/main/extensions/duckdb_mcp/description.yml)
* [Upstream MCP](https://github.com/teaguesterling/duckdb_mcp)
* DuckDB `src/include/duckdb/{function/table_function.hpp,catalog/catalog.hpp}`
* DuckDB `src/include/duckdb/catalog/catalog_entry/schema_catalog_entry.hpp`
* DuckDB `src/include/duckdb/main/{extension/extension_loader.hpp,secret/secret_manager.hpp}`
* DuckDB `src/include/duckdb/function/replacement_scan.hpp`
* DuckDB `src/include/duckdb/main/extension_entries.hpp`

## Production recommendation

Keep durable SQL definitions and discovery snapshots separate from live sessions.
For modest personal catalogs, generated macros are sufficient and unusually
simple to back up. Add schema fingerprints and explicit refresh/migration,
Secrets Manager providers, pooled transports with cancellation and timeouts,
stateless HTTP protocol support, resource scans and an opt-in result cache.
Use committed snapshots to bind offline without a discovery round trip.
If first-reference discovery of arbitrary names is essential, implement an
extension-backed attached catalog with lazy schema/function lookup, and a
documented SQL bootstrap to attach it from durable definitions. Seek an upstream
autoload/catalog-resolver integration rather than patching DuckDB privately.

For credentials, register an `mcp` `SecretType` and `CreateSecretFunction` through
`ExtensionLoader`. Store only `secret_name` in the server row; resolve it using
`SecretManager::Get(context).GetSecretByName(...)` at session creation. A
`KeyValueSecret` can redact tokens and environment maps via `redact_keys`.
Pass resolved values into stdio's existing `StdioConfig::environment` or the HTTP
Authorization header in memory. Provider-backed temporary secrets allow external
credential stores; optional persistent DuckDB secrets have a separate user-level
lifecycle and will not travel with a copied `.duckdb` file. Reconnection should
resolve credentials again to accommodate rotation.
