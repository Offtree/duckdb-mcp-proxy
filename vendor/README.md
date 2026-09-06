# Vendored dependencies

`duckdb_mcp`: selected source files from
https://github.com/teaguesterling/duckdb_mcp at
`0c0ece1b0f38935ed7f9fb7f621154aa6c9d93fa`, Apache-2.0 (license and NOTICE included).
Only stdio/TCP transport, message codec, JSON utilities and logging are compiled;
the TCP placeholder is not exposed.

Local adaptation in `mcp_message.cpp`: explicit VARCHAR JSON parameters are
serialized before legacy method-specific parameter builders, so initialization
honors our capabilities and tools/call preserves its arguments. Invalid JSON
now propagates instead of silently becoming an empty parameter object.

`nlohmann/json`: single header v3.11.3, MIT (license included).
Used for schema mapping and discovery snapshots.

The remote HTTP/OAuth component uses Cargo-managed dependencies rather than these
vendored stdio sources. See `remote/README.md` and `remote/Cargo.lock` for the
pinned SDK, versions and licensing information.
