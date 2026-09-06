#pragma once
#include "duckdb.hpp"
#include "nlohmann/json.hpp"

namespace duckdb {
namespace mcp_context {
using Json = nlohmann::ordered_json;
struct RemoteConfig {
	string url, secret_name;
	Json options = Json::object();
};
class RemoteBridge {
public:
	RemoteBridge();
	~RemoteBridge();
	RemoteBridge(const RemoteBridge &) = delete;
	RemoteBridge &operator=(const RemoteBridge &) = delete;
	Json Execute(ClientContext &context, const RemoteConfig &config, Json input);

private:
	void *handle;
};
void RegisterRemoteSecrets(ExtensionLoader &loader);
void ConfigureRemoteAuth(ClientContext &context, RemoteConfig &config);
string RemoteAuthStatus(ClientContext &context, const RemoteConfig &config);
} // namespace mcp_context
} // namespace duckdb
