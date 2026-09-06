#include "remote.hpp"
#include "duckdb/main/secret/secret_manager.hpp"
#include "duckdb/catalog/catalog_transaction.hpp"
#include <chrono>
#include <mutex>

extern "C" {
void *mcp_remote_new();
void mcp_remote_free(void *);
char *mcp_remote_execute(void *, const char *, void *, int (*)(void *, const char *));
void mcp_remote_string_free(char *);
}

namespace duckdb {
namespace mcp_context {
// Secrets Manager's persistent storage is shared across DatabaseInstances. Keep
// load -> refresh -> save serialized in this process, even across database files.
static std::mutex credential_mutex;

void ConfigureRemoteAuth(ClientContext &context, RemoteConfig &config) {
	if (!config.options.contains("auth") && !config.secret_name.empty()) {
		auto &db = DatabaseInstance::GetDatabase(context);
		auto entry = SecretManager::Get(context).GetSecretByName(CatalogTransaction::GetSystemTransaction(db),
		                                                         config.secret_name);
		if (entry && entry->secret->GetType() == "http")
			config.options["auth"] = "headers";
	}
	if (config.options.value("auth", "oauth") == "headers" && config.secret_name.empty())
		throw InvalidInputException("Header authentication requires a secret name");
}

static unique_ptr<SecretEntry> FindSecret(ClientContext &context, const RemoteConfig &config) {
	if (config.secret_name.empty())
		return nullptr;
	auto &db = DatabaseInstance::GetDatabase(context);
	auto entry =
	    SecretManager::Get(context).GetSecretByName(CatalogTransaction::GetSystemTransaction(db), config.secret_name);
	if (entry) {
		if (config.options.value("auth", "oauth") == "headers") {
			if (entry->secret->GetType() != "http")
				throw InvalidInputException("Header authentication requires an http secret");
			if (entry->secret->MatchScore(config.url) < 0)
				throw InvalidInputException("HTTP secret scope does not match the MCP URL");
			return entry;
		}
		if (entry->secret->GetType() != "mcp_oauth")
			throw InvalidInputException("Secret reference is not an mcp_oauth secret");
		auto &secret = static_cast<const KeyValueSecret &>(*entry->secret);
		if (secret.TryGetValue("resource").ToString() != config.url)
			throw InvalidInputException("OAuth secret belongs to a different MCP resource URL");
	}
	return entry;
}
static Json Headers(const SecretEntry &entry) {
	auto &secret = static_cast<const KeyValueSecret &>(*entry.secret);
	Json headers = Json::object();
	auto values = secret.TryGetValue("extra_http_headers");
	if (!values.IsNull()) {
		for (auto &pair : MapValue::GetChildren(values)) {
			auto &fields = StructValue::GetChildren(pair);
			if (fields[0].IsNull() || fields[1].IsNull())
				throw InvalidInputException("HTTP secret headers cannot contain NULL keys or values");
			headers[fields[0].ToString()] = fields[1].ToString();
		}
	}
	return headers;
}
static Json Credentials(const unique_ptr<SecretEntry> &entry) {
	if (!entry)
		return nullptr;
	auto value = static_cast<const KeyValueSecret &>(*entry->secret).TryGetValue("credentials");
	if (value.IsNull())
		return nullptr;
	try {
		return Json::parse(value.ToString());
	} catch (...) {
		throw InvalidInputException("Invalid OAuth secret payload");
	}
}
struct SecretSink {
	ClientContext &context;
	const RemoteConfig &config;
	SecretPersistType persist_type;
	string storage;
	string error;
};
static int SaveSecret(void *opaque, const char *credentials) {
	auto &sink = *static_cast<SecretSink *>(opaque);
	try {
		if (sink.config.secret_name.empty())
			throw InvalidInputException("No OAuth secret name configured");
		auto secret =
		    make_uniq<KeyValueSecret>(vector<string> {sink.config.url}, "mcp_oauth", "config", sink.config.secret_name);
		secret->secret_map["resource"] = Value(sink.config.url);
		secret->secret_map["credentials"] = Value(credentials);
		secret->redact_keys = {"credentials"};
		auto &db = DatabaseInstance::GetDatabase(sink.context);
		SecretManager::Get(sink.context)
		    .RegisterSecret(CatalogTransaction::GetSystemTransaction(db), std::move(secret),
		                    OnCreateConflict::REPLACE_ON_CONFLICT, sink.persist_type, sink.storage);
		return 0;
	} catch (...) {
		// Do not propagate exceptions across the C ABI or include credential values.
		sink.error = "Could not write OAuth secret; check secret_directory and persistent-secret settings";
		return 1;
	}
}
RemoteBridge::RemoteBridge() : handle(mcp_remote_new()) {
	if (!handle)
		throw IOException("Could not initialize remote MCP runtime");
}
RemoteBridge::~RemoteBridge() {
	mcp_remote_free(handle);
}
Json RemoteBridge::Execute(ClientContext &context, const RemoteConfig &config, Json input) {
	std::lock_guard<std::mutex> lock(credential_mutex);
	auto entry = FindSecret(context, config);
	bool header_auth = config.options.value("auth", "oauth") == "headers";
	if (header_auth && input["op"] != "validate") {
		if (input["op"] != "request")
			throw InvalidInputException("Header authentication uses an HTTP secret; OAuth login is not applicable");
		if (!entry)
			throw InvalidInputException(
			    "HTTP authentication secret not found; create or restore the referenced secret");
		input["headers"] = Headers(*entry);
		auto bearer = static_cast<const KeyValueSecret &>(*entry->secret).TryGetValue("bearer_token");
		if (!bearer.IsNull())
			input["bearer_token"] = bearer.ToString();
	}
	SecretSink sink {context, config,
	                 entry ? entry->persist_type
	                       : (config.options.value("persistent_secret", true) ? SecretPersistType::PERSISTENT
	                                                                          : SecretPersistType::TEMPORARY),
	                 entry ? entry->storage_mode : "", ""};
	input["url"] = config.url;
	input["secret_name"] = config.secret_name;
	input["options"] = config.options;
	input["credentials"] = header_auth ? Json() : Credentials(entry);
	input["header_auth"] = header_auth;
	char *raw = mcp_remote_execute(handle, input.dump().c_str(), &sink, SaveSecret);
	if (!raw)
		throw IOException("Remote MCP bridge returned no response");
	string text(raw);
	mcp_remote_string_free(raw);
	auto result = Json::parse(text);
	if (!sink.error.empty())
		throw IOException(sink.error);
	if (result.contains("error"))
		throw IOException(result["error"].get<string>());
	return result.at("ok");
}
static unique_ptr<BaseSecret> CreateOAuthSecret(ClientContext &, CreateSecretInput &input) {
	auto secret = make_uniq<KeyValueSecret>(input.scope, input.type, input.provider, input.name);
	secret->TrySetValue("resource", input);
	secret->TrySetValue("credentials", input);
	secret->redact_keys = {"credentials"};
	return std::move(secret);
}
static unique_ptr<BaseSecret> CreateHeaderSecret(ClientContext &, CreateSecretInput &input) {
	auto secret = make_uniq<KeyValueSecret>(input.scope, input.type, input.provider, input.name);
	secret->TrySetValue("bearer_token", input);
	secret->TrySetValue("extra_http_headers", input);
	secret->redact_keys = {"bearer_token", "extra_http_headers"};
	return std::move(secret);
}
void RegisterRemoteSecrets(ExtensionLoader &loader) {
	SecretType type;
	type.name = "mcp_oauth";
	type.default_provider = "config";
	type.deserializer = KeyValueSecret::Deserialize<KeyValueSecret>;
	loader.RegisterSecretType(type);
	CreateSecretFunction function;
	function.secret_type = "mcp_oauth";
	function.provider = "config";
	function.function = CreateOAuthSecret;
	function.named_parameters = {{"resource", LogicalType::VARCHAR}, {"credentials", LogicalType::VARCHAR}};
	loader.RegisterFunction(function);
	CreateSecretFunction headers;
	headers.secret_type = "http";
	headers.provider = "mcp";
	headers.function = CreateHeaderSecret;
	headers.named_parameters = {{"bearer_token", LogicalType::VARCHAR},
	                            {"extra_http_headers", LogicalType::MAP(LogicalType::VARCHAR, LogicalType::VARCHAR)}};
	loader.RegisterFunction(headers);
}
string RemoteAuthStatus(ClientContext &context, const RemoteConfig &config) {
	if (config.secret_name.empty())
		return "not_configured";
	std::lock_guard<std::mutex> lock(credential_mutex);
	auto entry = FindSecret(context, config);
	if (config.options.value("auth", "oauth") == "headers")
		return entry ? "headers_stored" : "secret_missing";
	auto creds = Credentials(entry);
	if (creds.is_null() || !creds.contains("token_response") || creds["token_response"].is_null())
		return "login_required";
	auto token = creds["token_response"];
	if (token.contains("expires_in") && creds.contains("token_received_at") && !creds["token_received_at"].is_null()) {
		auto now = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch())
		               .count();
		if (now + 60 >= creds["token_received_at"].get<int64_t>() + token["expires_in"].get<int64_t>())
			return "refresh_required";
	}
	return "credentials_stored";
}
} // namespace mcp_context
} // namespace duckdb
