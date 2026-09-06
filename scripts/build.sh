#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -f "$ROOT/duckdb/CMakeLists.txt" ]]; then
  git clone --depth 1 --branch v1.4.4 https://github.com/duckdb/duckdb.git "$ROOT/duckdb"
fi
if [[ "$(git -C "$ROOT/duckdb" rev-parse HEAD)" != "6ddac802ffa9bcfbcc3f5f0d71de5dff9b0bc250" ]]; then
  echo "Expected the pinned DuckDB v1.4.4 checkout in $ROOT/duckdb" >&2
  exit 1
fi
cmake -S "$ROOT/duckdb" -B "$ROOT/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DDUCKDB_EXTENSION_CONFIGS="$ROOT/extension_config.cmake" \
  -DBUILD_UNITTESTS=OFF -DBUILD_SHELL=OFF -DDISABLE_UNITY=OFF \
  -DEXTENSION_STATIC_BUILD=ON
cmake --build "$ROOT/build" --target mcp_context_loadable_extension -j "${JOBS:-2}"
