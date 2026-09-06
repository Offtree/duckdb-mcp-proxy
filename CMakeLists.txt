cmake_minimum_required(VERSION 3.18)
project(mcp_context)
include_directories(src vendor vendor/duckdb_mcp/src/include ${CMAKE_SOURCE_DIR}/third_party/yyjson/include)
find_program(CARGO_EXECUTABLE cargo REQUIRED)
set(RUST_TARGET_DIR ${CMAKE_BINARY_DIR}/rust)
set(REMOTE_LIB ${RUST_TARGET_DIR}/release/libmcp_context_remote.a)
add_custom_command(OUTPUT ${REMOTE_LIB}
  COMMAND ${CARGO_EXECUTABLE} build --release --locked
    --manifest-path ${CMAKE_CURRENT_SOURCE_DIR}/remote/Cargo.toml --target-dir ${RUST_TARGET_DIR}
  DEPENDS remote/Cargo.toml remote/Cargo.lock remote/src/lib.rs
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  COMMENT "Building native MCP HTTP/OAuth component")
add_custom_target(mcp_remote_build DEPENDS ${REMOTE_LIB})
set(SOURCES src/mcp_context.cpp src/remote.cpp
  vendor/duckdb_mcp/src/protocol/mcp_transport.cpp
  vendor/duckdb_mcp/src/protocol/mcp_message.cpp
  vendor/duckdb_mcp/src/json_utils.cpp
  vendor/duckdb_mcp/src/duckdb_mcp_logging.cpp)
build_static_extension(mcp_context ${SOURCES})
build_loadable_extension(mcp_context "" ${SOURCES})
foreach(TARGET mcp_context_extension mcp_context_loadable_extension)
  add_dependencies(${TARGET} mcp_remote_build)
  target_link_libraries(${TARGET} ${REMOTE_LIB} ${CMAKE_DL_LIBS} pthread m)
endforeach()
if(NOT MSVC)
  # The official CLI does not export RTTI; the Python wheel happens to do so.
  target_compile_options(mcp_context_extension PRIVATE -fno-rtti)
  target_compile_options(mcp_context_loadable_extension PRIVATE -fno-rtti)
endif()
