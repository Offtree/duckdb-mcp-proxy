# Stateless HTTP and native OAuth (0.2)

The remote layer targets **MCP 2026-07-28**. It sends self-contained HTTP POSTs
with protocol/client metadata, `MCP-Protocol-Version`, `Mcp-Method` and (for tool
calls) `Mcp-Name`. There is no `initialize` exchange, `Mcp-Session-Id`, remote
session cache or automatic downgrade. The HTTP library may reuse TCP connections.

## Components

```text
DuckDB SQL / durable macros
            │
    C++ bind + scan callbacks
            │
       narrow C ABI
            │
  Rust current-thread runtime
    ├─ reqwest + rustls: HTTP/TLS
    ├─ sse-stream: finite SSE responses
    └─ rmcp: OAuth discovery, PKCE, registration, exchange, refresh
            │ credential-store callback, on the calling thread
    C++ DuckDB Secrets Manager adapter
```

`rmcp` is pinned to commit `302319861a4b5ab538f6aebf25befdc3c7dfe039`
(3.2.0 source). Its `AuthorizationManager`, `AuthorizationSession` and
`CredentialStore` provide the OAuth implementation. Cargo.lock fixes transitive
dependencies. No JavaScript runtime or external OAuth helper is installed.

## Registration and durable state

`PRAGMA mcp_register_http(name, url, secret_name, options_json)` expands into
ordinary catalog SQL. `_mcp.servers` retains the common server identity;
`_mcp.http_servers` holds endpoint, secret reference and public client options.
This additive table avoids rewriting existing stdio metadata on database open.
Registration validates the URL/options without making a network request.

Existing `mcp_tool`, `mcp_tools`, discovery macros and relational mapping work with
both transports. Persisted schema snapshots bind offline. These snapshots are
explicit user-created catalog definitions, not an automatic protocol response
cache; list-result TTL/cacheScope never causes silent macro/schema mutation.

## Login and credential storage

1. `mcp_login_begin` binds a loopback callback listener and asks the SDK to discover
   the authorization server, select/register a public client and generate PKCE/state.
2. The extension opens the browser, or returns the URL for manual opening.
3. `mcp_login_finish` waits for the callback and gives its code/state/issuer to
   the SDK. The SDK exchanges the code only after validation.
4. `CredentialStore::save` writes a redacted `mcp_oauth` `KeyValueSecret` through
   DuckDB's native Secrets Manager. The callback returns success only after save.

`PRAGMA mcp_login(name)` combines those steps. Both table functions perform login
effects at scan initialization, never during bind/EXPLAIN. Browser callback state
is in memory, expires after 180 seconds, and disappears on database close.

The secret stores a resource URL and serialized SDK credentials (client ID,
issuer, tokens, granted scopes and token receipt time). The entire credential
payload is redacted in `duckdb_secrets()`. Runtime code passes values directly
through the native API: it does not generate SQL containing credentials. An
exact resource-URL check prevents using a secret reference for another endpoint.
Issuer changes are handled by the SDK's stored-credential validation.

Existing secret persistence/storage modes are retained on updates. New secrets
default to persistent; `persistent_secret: false` chooses temporary storage.
DuckDB's default persistent secret backend is an unencrypted, permission-protected
file outside the `.duckdb` database. OAuth writes are independent of SQL rollback,
which is necessary when a provider rotates a refresh token.

## Requests and refresh

Before a credentialed request, the extension loads the secret, resolves OAuth
metadata, validates issuer binding and asks the SDK for an access token. The SDK
refreshes tokens near expiry. Rotated credentials are saved **before** sending
the MCP request. The C++ adapter serializes credential operations in-process;
its lock also spans the SDK's refresh and save. A current-thread Tokio runtime
keeps callbacks on the caller's native thread, and Rust clears callback pointers
before returning across the ABI. Pending logins never retain a DuckDB context.

Transient refresh failures preserve credentials. A rejected grant asks for login.
401/403 responses are reported without replaying a potentially mutating tool.
The 401 challenge can seed subsequent OAuth discovery. Automatic browser prompts
and scope upgrades do not occur during ordinary SELECT execution.

Network/provider errors expose categories rather than token-response bodies.
Redirects are disabled for MCP POSTs. HTTPS uses the platform certificate trust
store; plain HTTP is accepted only for loopback endpoints. MCP bodies are bounded
to 32 MiB with a 30-second HTTP timeout. JSON and finite SSE responses are parsed
and checked for matching JSON-RPC IDs. `input_required` gets an explicit unsupported
error; no multi-round-trip tool replay is attempted.

## Tested and remaining boundaries

Tests exercise the actual compiled extension against a separate HTTP/OAuth server
process. The fixture verifies PKCE, redirect/client binding, issuer/state,
resource indicators and rotating refresh tokens. Another test drives the stock
CLI's one-command browser login through a test browser process, then reopens the
database read-only in another CLI process. No real account credentials are used.

This is not a certification for every OAuth deployment. The API supports native
public clients, with pre-registered IDs, CIMD configuration or dynamic registration.
Confidential clients, device-code grants, cross-process coordination of shared
rotating secrets, legacy HTTP, cancellation, tasks and multi-round-trip interactions
remain future work. Headless remote-host use needs loopback-port forwarding.

Source references:
* [MCP stateless revision](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
* [Pinned rmcp OAuth implementation](https://github.com/modelcontextprotocol/rust-sdk/blob/302319861a4b5ab538f6aebf25befdc3c7dfe039/crates/rmcp/src/transport/auth.rs)
* `src/remote.cpp`, `remote/src/lib.rs`, `tests/test_remote.py`
