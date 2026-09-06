# Native remote component

Built by CMake as a Rust static library and linked into `mcp_context`.
See [the remote architecture](../docs/remote.md) for the C ABI and storage design.

The OAuth implementation is reused from the official Apache-2.0
[`rmcp` SDK](https://github.com/modelcontextprotocol/rust-sdk), pinned by git
revision in Cargo.toml. Its license is reproduced in `LICENSE-rmcp`.
Cargo.lock pins all transitive versions, including `oauth2` (MIT/Apache-2.0),
`reqwest` (MIT/Apache-2.0), `rustls` (Apache-2.0/ISC/MIT), `tokio` (MIT),
`sse-stream` (MIT) and `webbrowser` (MIT/Apache-2.0). Dependency source and license
files are fetched into Cargo's source cache when building.

Useful developer commands:

```bash
cargo fmt --manifest-path remote/Cargo.toml --check
cargo clippy --manifest-path remote/Cargo.toml --target-dir build/rust -- -D warnings
```

End-to-end behavior is tested through the native DuckDB artifact, not an isolated
mock of this bridge. Use the test commands in the root README.
