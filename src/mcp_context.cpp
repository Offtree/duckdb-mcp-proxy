#define DUCKDB_EXTENSION_MAIN
#include "duckdb.hpp"
#include "duckdb/main/extension/extension_loader.hpp"
#include "duckdb/main/database.hpp"
#include "duckdb/main/config.hpp"
#include "duckdb/function/pragma_function.hpp"
#include "protocol/mcp_transport.hpp"
#include "protocol/mcp_message.hpp"
#include "nlohmann/json.hpp"
#include "remote.hpp"
#include <map>
#include <set>
#include <mutex>
#include <iostream>

namespace duckdb {
namespace mcp_context {
using Json = nlohmann::ordered_json;

static string Quote(const string &s, char quote = '\'') {
	string out(1, quote);
	for (auto c : s) {
		out += c;
		if (c == quote)
			out += c;
	}
	return out + quote;
}
static string Ident(const string &s) {
	return Quote(s, '"');
}
static void CheckExternal(ClientContext &context) {
	if (!DBConfig::GetConfig(context).options.enable_external_access)
		throw PermissionException("MCP requires enable_external_access");
}
static unique_ptr<MaterializedQueryResult> Query(Connection &c, const string &sql) {
	auto result = c.Query(sql);
	if (result->HasError())
		throw InvalidInputException(result->GetError());
	return result;
}
static string CatalogName(ClientContext &context) {
	Connection c(DatabaseInstance::GetDatabase(context));
	return Query(c, "SELECT current_database()")->GetValue(0, 0).ToString();
}
static string Meta(ClientContext &context) {
	return Ident(CatalogName(context)) + ".\"_mcp\".";
}
static bool HasMetadata(Connection &c) {
	return Query(c, "SELECT count(*) FROM information_schema.tables WHERE table_catalog=current_database() "
	                "AND table_schema='_mcp' AND table_name='servers'")
	           ->GetValue(0, 0)
	           .GetValue<int64_t>() != 0;
}
static string MetadataDDL(ClientContext &context) {
	auto catalog = Ident(CatalogName(context));
	auto meta = catalog + ".\"_mcp\".";
	return "CREATE SCHEMA IF NOT EXISTS " + catalog +
	       ".\"_mcp\";"
	       "CREATE TABLE IF NOT EXISTS " +
	       meta +
	       "servers(name VARCHAR PRIMARY KEY, transport VARCHAR NOT NULL, "
	       "command VARCHAR NOT NULL, args VARCHAR NOT NULL, created_at TIMESTAMP DEFAULT current_timestamp);"
	       "CREATE TABLE IF NOT EXISTS " +
	       meta +
	       "tools(server VARCHAR, name VARCHAR, definition VARCHAR NOT NULL, "
	       "discovered_at TIMESTAMP DEFAULT current_timestamp, PRIMARY KEY(server,name));"
	       "CREATE TABLE IF NOT EXISTS " +
	       meta +
	       "http_servers(name VARCHAR PRIMARY KEY, url VARCHAR NOT NULL, secret_name VARCHAR NOT NULL, options VARCHAR "
	       "NOT NULL);";
}

struct Definition {
	string name, command, args;
	string transport;
	RemoteConfig remote;
	Definition(string name, string command, string args, string transport = "stdio")
	    : name(std::move(name)), command(std::move(command)), args(std::move(args)), transport(std::move(transport)) {
	}
	Definition() = default;
	string Key() const {
		return Json::array({command, args}).dump();
	}
};
static Definition Lookup(ClientContext &context, const string &name) {
	Connection c(DatabaseInstance::GetDatabase(context));
	if (!HasMetadata(c))
		throw BinderException("No MCP servers registered; use PRAGMA mcp_register");
	auto r = Query(c, "SELECT command,args,transport FROM " + Meta(context) + "servers WHERE name=" + Quote(name));
	if (!r->RowCount())
		throw BinderException("Unknown MCP server: %s", name);
	Definition d {name, r->GetValue(0, 0).ToString(), r->GetValue(1, 0).ToString(), r->GetValue(2, 0).ToString()};
	if (d.transport == "http") {
		auto h =
		    Query(c, "SELECT url,secret_name,options FROM " + Meta(context) + "http_servers WHERE name=" + Quote(name));
		if (!h->RowCount())
			throw BinderException("Missing HTTP server metadata");
		d.remote.url = h->GetValue(0, 0).ToString();
		d.remote.secret_name = h->GetValue(1, 0).ToString();
		d.remote.options = Json::parse(h->GetValue(2, 0).ToString());
	} else if (d.transport != "stdio")
		throw BinderException("Unsupported MCP transport");
	return d;
}

struct Session {
	unique_ptr<MCPTransport> transport;
	uint64_t next_id = 1;
	Json Request(const string &method, const Json &params) {
		auto id = Value::UBIGINT(next_id++);
		auto response = transport->SendAndReceive(MCPMessage::CreateRequest(method, Value(params.dump()), id));
		if (response.IsError())
			throw IOException("MCP %s: %s", method, response.error.message);
		if (response.id.ToString() != id.ToString())
			throw IOException("MCP response ID mismatch");
		return Json::parse(response.result.ToString());
	}
};
struct Runtime {
	std::mutex mutex;
	std::map<string, unique_ptr<Session>> sessions;
	unique_ptr<RemoteBridge> remote;
	Json Remote(ClientContext &context, const RemoteConfig &config, Json input) {
		if (!remote)
			remote = make_uniq<RemoteBridge>();
		return remote->Execute(context, config, std::move(input));
	}
	Json Request(ClientContext &context, const Definition &d, const string &method, const Json &params) {
		if (d.transport == "http")
			return Remote(context, d.remote, {{"op", "request"}, {"method", method}, {"params", params}});
		return Connect(d).Request(method, params);
	}
	Session &Connect(const Definition &d) {
		auto key = d.name + d.Key();
		auto it = sessions.find(key);
		if (it != sessions.end())
			return *it->second;
		auto s = make_uniq<Session>();
		StdioTransport::StdioConfig config;
		config.command_path = d.command;
		for (auto &arg : Json::parse(d.args))
			config.arguments.push_back(arg.get<string>());
		s->transport = make_uniq<StdioTransport>(config);
		if (!s->transport->Connect())
			throw IOException("Cannot start MCP server %s", d.name);
		auto init = s->Request("initialize", {{"protocolVersion", "2025-06-18"},
		                                      {"capabilities", Json::object()},
		                                      {"clientInfo", {{"name", "duckdb-mcp-context"}, {"version", "0.1.0"}}}});
		if (init.value("protocolVersion", "") != "2024-11-05" && init.value("protocolVersion", "") != "2025-03-26" &&
		    init.value("protocolVersion", "") != "2025-06-18")
			throw IOException("Unsupported MCP protocol version");
		s->transport->Send(MCPMessage::CreateNotification("notifications/initialized", Value("{}")));
		auto &result = *s;
		sessions[key] = std::move(s);
		return result;
	}
	Json Discover(ClientContext &context, const Definition &d) {
		std::lock_guard<std::mutex> lock(mutex);
		Json tools = Json::array(), params = Json::object();
		std::set<string> cursors, names;
		for (idx_t page = 0; page < 1000; page++) {
			auto response = Request(context, d, "tools/list", params);
			if (!response.contains("tools") || !response["tools"].is_array())
				throw IOException("tools/list must return a tools array");
			for (auto &t : response["tools"]) {
				auto name = t.at("name").get<string>();
				if (!names.insert(StringUtil::Lower(name)).second)
					throw IOException("Duplicate MCP tool name");
				tools.push_back(t);
			}
			string cursor = response.value("nextCursor", "");
			if (cursor.empty())
				return tools;
			if (!cursors.insert(cursor).second)
				throw IOException("Repeated tools/list cursor");
			params["cursor"] = cursor;
		}
		throw IOException("Too many tools/list pages");
	}
	Json Call(ClientContext &context, const Definition &d, const string &tool, const Json &args) {
		std::lock_guard<std::mutex> lock(mutex);
		try {
			auto result = Request(context, d, "tools/call", {{"name", tool}, {"arguments", args}});
			if (result.value("isError", false))
				throw IOException("MCP tool %s returned isError: %s", tool, result.dump());
			return result;
		} catch (...) {
			// Never retry a potentially mutating call. Reconnect only on a later query.
			sessions.erase(d.name + d.Key());
			throw;
		}
	}
};
struct Info : TableFunctionInfo {
	shared_ptr<Runtime> runtime;
	explicit Info(shared_ptr<Runtime> runtime) : runtime(std::move(runtime)) {
	}
};
struct BindData : TableFunctionData {
	shared_ptr<Runtime> runtime;
	Definition definition;
	string tool, envelope;
	Json args, schema;
	bool invoke = false, fallback = false, array = false;
	string login_action;
	bool open_browser = true;
	vector<string> names;
	vector<LogicalType> types;
	vector<vector<Value>> rows;
};
struct ScanState : GlobalTableFunctionState {
	vector<vector<Value>> rows;
	idx_t offset = 0;
};
static Json ToJson(const Value &v) {
	if (v.IsNull())
		return nullptr;
	switch (v.type().id()) {
	case LogicalTypeId::STRUCT: {
		Json obj = Json::object();
		auto &children = StructValue::GetChildren(v);
		for (idx_t i = 0; i < children.size(); i++)
			obj[StructType::GetChildName(v.type(), i)] = ToJson(children[i]);
		return obj;
	}
	case LogicalTypeId::LIST: {
		Json a = Json::array();
		for (auto &item : ListValue::GetChildren(v))
			a.push_back(ToJson(item));
		return a;
	}
	case LogicalTypeId::BOOLEAN:
		return v.GetValue<bool>();
	case LogicalTypeId::TINYINT:
	case LogicalTypeId::SMALLINT:
	case LogicalTypeId::INTEGER:
	case LogicalTypeId::BIGINT:
		return v.GetValue<int64_t>();
	case LogicalTypeId::UTINYINT:
	case LogicalTypeId::USMALLINT:
	case LogicalTypeId::UINTEGER:
	case LogicalTypeId::UBIGINT:
		return v.GetValue<uint64_t>();
	case LogicalTypeId::FLOAT:
	case LogicalTypeId::DOUBLE:
	case LogicalTypeId::DECIMAL:
		return v.GetValue<double>();
	case LogicalTypeId::VARCHAR:
		return v.ToString();
	default:
		throw BinderException("Unsupported MCP argument SQL type: %s", v.type().ToString());
	}
}
static string SchemaType(const Json &schema) {
	if (!schema.is_object())
		return "";
	auto t = schema.value("type", Json());
	if (t.is_string())
		return t.get<string>();
	if (t.is_array()) {
		string result;
		for (auto &v : t)
			if (v != "null") {
				if (!result.empty())
					return "";
				result = v.get<string>();
			}
		return result;
	}
	return "";
}
static void Validate(const Json &value, const Json &schema, const string &path) {
	if (!schema.is_object())
		return;
	string t = SchemaType(schema);
	if (value.is_null()) {
		auto type = schema.value("type", Json());
		if (type.is_null() || type == "null" ||
		    (type.is_array() && std::find(type.begin(), type.end(), Json("null")) != type.end()))
			return;
		throw BinderException("MCP %s does not allow null", path);
	}
	bool valid = t.empty() || (t == "string" && value.is_string()) || (t == "integer" && value.is_number_integer()) ||
	             (t == "number" && value.is_number()) || (t == "boolean" && value.is_boolean()) ||
	             (t == "object" && value.is_object()) || (t == "array" && value.is_array());
	if (!valid)
		throw BinderException("MCP %s requires %s", path, t);
	if (value.is_object()) {
		for (auto &r : schema.value("required", Json::array()))
			if (!value.contains(r.get<string>()))
				throw BinderException("Missing required MCP argument/field: %s.%s", path, r.get<string>());
		auto props = schema.value("properties", Json::object());
		for (auto it = value.begin(); it != value.end(); ++it) {
			if (props.contains(it.key()))
				Validate(it.value(), props[it.key()], path + "." + it.key());
			else if (schema.value("additionalProperties", Json(true)) == false)
				throw BinderException("Unknown MCP field: %s", it.key());
		}
	}
	if (value.is_array() && schema.contains("items"))
		for (auto &v : value)
			Validate(v, schema["items"], path + "[]");
}
static LogicalType SqlType(const Json &s) {
	auto t = SchemaType(s);
	if (t == "string")
		return LogicalType::VARCHAR;
	if (t == "integer")
		return LogicalType::BIGINT;
	if (t == "number")
		return LogicalType::DOUBLE;
	if (t == "boolean")
		return LogicalType::BOOLEAN;
	return LogicalType::JSON();
}
static void Columns(BindData &d) {
	Json row = d.schema;
	if (SchemaType(row) == "object") {
		auto props = row.value("properties", Json::object());
		if (props.size() == 1 && SchemaType(props.begin().value()) == "array") {
			d.envelope = props.begin().key();
			row = props.begin().value();
		}
	}
	if (SchemaType(row) == "array") {
		d.array = true;
		row = row.value("items", Json::object());
	}
	if (SchemaType(row) == "object" && row.contains("properties") && !row["properties"].empty()) {
		std::set<string> seen;
		for (auto it = row["properties"].begin(); it != row["properties"].end(); ++it) {
			if (!seen.insert(StringUtil::Lower(it.key())).second)
				throw BinderException("Case-colliding output columns");
			d.names.push_back(it.key());
			d.types.push_back(SqlType(it.value()));
		}
	} else {
		d.fallback = true;
		d.names = {"result"};
		d.types = {LogicalType::JSON()};
	}
}
static unique_ptr<FunctionData> BindTool(ClientContext &context, TableFunctionBindInput &input,
                                         vector<LogicalType> &types, vector<string> &names) {
	CheckExternal(context);
	auto d = make_uniq<BindData>();
	d->runtime = input.info->Cast<Info>().runtime;
	d->invoke = true;
	for (auto key : {"server", "tool"})
		if (!input.named_parameters.count(key) || input.named_parameters[key].IsNull())
			throw BinderException("mcp_tool requires server and tool");
	d->definition = Lookup(context, input.named_parameters["server"].ToString());
	d->tool = input.named_parameters["tool"].ToString();
	d->args = Json::object();
	if (input.named_parameters.count("args")) {
		auto &a = input.named_parameters["args"];
		d->args = a.type().id() == LogicalTypeId::VARCHAR ? Json::parse(a.ToString()) : ToJson(a);
	}
	if (!d->args.is_object())
		throw BinderException("MCP args must be a STRUCT or JSON object string");
	if (input.named_parameters.count("omit_nulls") && input.named_parameters["omit_nulls"].GetValue<bool>()) {
		for (auto it = d->args.begin(); it != d->args.end();) {
			if (it.value().is_null())
				it = d->args.erase(it);
			else
				++it;
		}
	}
	Json tool;
	Connection c(DatabaseInstance::GetDatabase(context));
	auto stored = Query(c, "SELECT definition FROM " + Meta(context) +
	                           "tools WHERE server=" + Quote(d->definition.name) + " AND name=" + Quote(d->tool));
	if (stored->RowCount())
		tool = Json::parse(stored->GetValue(0, 0).ToString());
	else
		for (auto &t : d->runtime->Discover(context, d->definition))
			if (t["name"] == d->tool) {
				tool = t;
				break;
			}
	if (tool.is_null())
		throw BinderException("Unknown MCP tool: %s", d->tool);
	Validate(d->args, tool.value("inputSchema", Json::object()), "arguments");
	d->schema = tool.value("outputSchema", Json::object());
	Columns(*d);
	types = d->types;
	names = d->names;
	return std::move(d);
}
static unique_ptr<FunctionData> BindTools(ClientContext &context, TableFunctionBindInput &input,
                                          vector<LogicalType> &types, vector<string> &names) {
	CheckExternal(context);
	auto d = make_uniq<BindData>();
	d->runtime = input.info->Cast<Info>().runtime;
	auto def = Lookup(context, input.inputs[0].ToString());
	names = {"server", "name", "description", "input_schema", "output_schema", "definition"};
	types = {LogicalType::VARCHAR, LogicalType::VARCHAR, LogicalType::VARCHAR,
	         LogicalType::JSON(),  LogicalType::JSON(),  LogicalType::JSON()};
	for (auto &t : d->runtime->Discover(context, def))
		d->rows.push_back({Value(def.name), Value(t["name"].get<string>()), Value(t.value("description", "")),
		                   Value(t.value("inputSchema", Json::object()).dump()),
		                   t.contains("outputSchema") ? Value(t["outputSchema"].dump()) : Value(), Value(t.dump())});
	return std::move(d);
}
static unique_ptr<FunctionData> BindServers(ClientContext &context, TableFunctionBindInput &input,
                                            vector<LogicalType> &types, vector<string> &names) {
	auto d = make_uniq<BindData>();
	auto runtime = input.info->Cast<Info>().runtime;
	names = {"name",           "transport", "command",     "args",       "connection_status",
	         "last_discovery", "url",       "secret_name", "auth_status"};
	types = {LogicalType::VARCHAR, LogicalType::VARCHAR, LogicalType::VARCHAR,
	         LogicalType::JSON(),  LogicalType::VARCHAR, LogicalType::TIMESTAMP,
	         LogicalType::VARCHAR, LogicalType::VARCHAR, LogicalType::VARCHAR};
	Connection c(DatabaseInstance::GetDatabase(context));
	if (!HasMetadata(c))
		return std::move(d);
	auto r = Query(c, "SELECT s.name,s.transport,s.command,s.args,(SELECT max(discovered_at) FROM " + Meta(context) +
	                      "tools t WHERE t.server=s.name) FROM " + Meta(context) + "servers s ORDER BY s.name");
	std::lock_guard<std::mutex> lock(runtime->mutex);
	for (idx_t i = 0; i < r->RowCount(); i++) {
		if (r->GetValue(1, i).ToString() == "http") {
			auto def = Lookup(context, r->GetValue(0, i).ToString());
			d->rows.push_back({r->GetValue(0, i), r->GetValue(1, i), Value(), r->GetValue(3, i), Value("stateless"),
			                   r->GetValue(4, i), Value(def.remote.url), Value(def.remote.secret_name),
			                   Value(RemoteAuthStatus(context, def.remote))});
			continue;
		}
		Definition def {r->GetValue(0, i).ToString(), r->GetValue(2, i).ToString(), r->GetValue(3, i).ToString()};
		auto it = runtime->sessions.find(def.name + def.Key());
		bool connected = it != runtime->sessions.end() && it->second->transport->IsConnected();
		d->rows.push_back({r->GetValue(0, i), r->GetValue(1, i), r->GetValue(2, i), r->GetValue(3, i),
		                   Value(connected ? "connected" : "disconnected"), r->GetValue(4, i), Value(), Value(),
		                   Value("server_managed")});
	}
	return std::move(d);
}
static Value Cell(const Json &v, const LogicalType &type) {
	if (v.is_null())
		return Value(type);
	if (type == LogicalType::JSON())
		return Value(v.dump());
	if (type == LogicalType::VARCHAR)
		return Value(v.get<string>());
	if (type == LogicalType::BOOLEAN)
		return Value::BOOLEAN(v.get<bool>());
	if (type == LogicalType::BIGINT) {
		if (v.is_number_unsigned() && v.get<uint64_t>() > uint64_t(INT64_MAX))
			throw IOException("MCP integer exceeds BIGINT");
		return Value::BIGINT(v.get<int64_t>());
	}
	return Value::DOUBLE(v.get<double>());
}
static unique_ptr<GlobalTableFunctionState> Init(ClientContext &context, TableFunctionInitInput &input) {
	auto &d = input.bind_data->Cast<BindData>();
	auto s = make_uniq<ScanState>();
	if (!d.login_action.empty()) {
		CheckExternal(context);
		std::lock_guard<std::mutex> lock(d.runtime->mutex);
		auto result =
		    d.runtime->Remote(context, d.definition.remote, {{"op", d.login_action}, {"open_browser", d.open_browser}});
		if (d.login_action == "login_begin")
			s->rows.push_back({Value(result.at("authorization_url").get<string>()),
			                   Value(result.at("callback_url").get<string>()),
			                   Value::BOOLEAN(result.at("browser_opened").get<bool>()),
			                   Value::BIGINT(result.at("expires_in").get<int64_t>())});
		else
			s->rows.push_back({Value::BOOLEAN(true)});
		return std::move(s);
	}
	if (!d.invoke) {
		s->rows = d.rows;
		return std::move(s);
	}
	CheckExternal(context);
	auto result = d.runtime->Call(context, d.definition, d.tool, d.args);
	Json payload;
	if (result.contains("structuredContent"))
		payload = result["structuredContent"];
	else if (result.contains("content") && result["content"].is_array() && result["content"].size() == 1 &&
	         result["content"][0].value("type", "") == "text") {
		auto text = result["content"][0].value("text", "");
		payload = Json::parse(text, nullptr, false);
		if (payload.is_discarded())
			payload = result;
	} else
		payload = result;
	if (d.fallback) {
		s->rows.push_back({Value(payload.dump())});
		return std::move(s);
	}
	Validate(payload, d.schema, "output");
	if (!d.envelope.empty())
		payload = payload.at(d.envelope);
	Json rows = d.array ? payload : Json::array({payload});
	for (auto &r : rows) {
		vector<Value> row;
		for (idx_t col = 0; col < d.names.size(); col++)
			row.push_back(Cell(r.value(d.names[col], Json()), d.types[col]));
		s->rows.push_back(std::move(row));
	}
	return std::move(s);
}
static void Scan(ClientContext &, TableFunctionInput &input, DataChunk &output) {
	auto &s = input.global_state->Cast<ScanState>();
	auto count = MinValue<idx_t>(STANDARD_VECTOR_SIZE, s.rows.size() - s.offset);
	for (idx_t row = 0; row < count; row++)
		for (idx_t col = 0; col < output.ColumnCount(); col++)
			output.SetValue(col, row, s.rows[s.offset + row][col]);
	s.offset += count;
	output.SetCardinality(count);
}
static string Register(ClientContext &context, const FunctionParameters &p) {
	CheckExternal(context);
	for (auto &v : p.values)
		if (v.IsNull())
			throw InvalidInputException("Registration parameters cannot be NULL");
	auto name = p.values[0].ToString(), command = p.values[1].ToString(), args = p.values[2].ToString();
	auto lower_name = StringUtil::Lower(name);
	if (name.empty() || lower_name == "_mcp" || lower_name == "main" || lower_name == "information_schema" ||
	    lower_name == "pg_catalog")
		throw InvalidInputException("Choose a non-reserved MCP schema name");
	if (command.empty() || command[0] != '/')
		throw InvalidInputException("Use an absolute stdio executable path");
	auto a = Json::parse(args);
	if (!a.is_array())
		throw InvalidInputException("stdio args must be a JSON array of strings");
	for (auto &v : a)
		if (!v.is_string())
			throw InvalidInputException("stdio args must be strings");
	return MetadataDDL(context) + "INSERT INTO " + Meta(context) + "servers(name,transport,command,args) VALUES(" +
	       Quote(name) + ",'stdio'," + Quote(command) + "," + Quote(a.dump()) + ");CREATE SCHEMA " +
	       Ident(CatalogName(context)) + "." + Ident(name) + ";";
}
static string RegisterHTTP(ClientContext &context, const FunctionParameters &p) {
	CheckExternal(context);
	for (auto &v : p.values)
		if (v.IsNull())
			throw InvalidInputException("Registration parameters cannot be NULL");
	auto name = p.values[0].ToString();
	auto lower = StringUtil::Lower(name);
	if (name.empty() || lower == "_mcp" || lower == "main" || lower == "information_schema" || lower == "pg_catalog")
		throw InvalidInputException("Choose a non-reserved MCP schema name");
	RemoteConfig config;
	config.url = p.values[1].ToString();
	RemoteBridge validator;
	config.url = validator.Execute(context, config, {{"op", "validate"}}).at("url").get<string>();
	if (p.values.size() > 2)
		config.secret_name = p.values[2].ToString();
	if (p.values.size() > 3)
		config.options = Json::parse(p.values[3].ToString());
	if (!config.options.is_object())
		throw InvalidInputException("OAuth options must be a JSON object");
	for (auto it = config.options.begin(); it != config.options.end(); ++it) {
		auto k = it.key();
		auto &v = it.value();
		bool valid = ((k == "client_id" || k == "client_metadata_url") && v.is_string()) ||
		             (k == "persistent_secret" && v.is_boolean()) ||
		             (k == "redirect_port" && v.is_number_unsigned() && v.get<uint64_t>() <= 65535);
		if (k == "scopes" && v.is_array()) {
			valid = true;
			for (auto &s : v)
				if (!s.is_string())
					valid = false;
		}
		if (!valid)
			throw InvalidInputException("Unsupported OAuth option or type: %s", k);
	}
	return MetadataDDL(context) + "INSERT INTO " + Meta(context) + "servers(name,transport,command,args) VALUES(" +
	       Quote(name) + ",'http','','[]');INSERT INTO " + Meta(context) + "http_servers VALUES(" + Quote(name) + "," +
	       Quote(config.url) + "," + Quote(config.secret_name) + "," + Quote(config.options.dump()) +
	       ");CREATE SCHEMA " + Ident(CatalogName(context)) + "." + Ident(name) + ";";
}
static unique_ptr<FunctionData> BindLogin(ClientContext &context, TableFunctionBindInput &input,
                                          vector<LogicalType> &types, vector<string> &names) {
	CheckExternal(context);
	auto d = make_uniq<BindData>();
	d->runtime = input.info->Cast<Info>().runtime;
	d->definition = Lookup(context, input.inputs[0].ToString());
	if (d->definition.transport != "http" || d->definition.remote.secret_name.empty())
		throw BinderException("OAuth login requires an HTTP server registered with a secret name");
	if (input.table_function.name == "mcp_login_begin") {
		d->login_action = "login_begin";
		if (input.named_parameters.count("open_browser"))
			d->open_browser = input.named_parameters["open_browser"].GetValue<bool>();
		names = {"authorization_url", "callback_url", "browser_opened", "expires_in"};
		types = {LogicalType::VARCHAR, LogicalType::VARCHAR, LogicalType::BOOLEAN, LogicalType::BIGINT};
	} else {
		d->login_action = "login_finish";
		names = {"authenticated"};
		types = {LogicalType::BOOLEAN};
	}
	return std::move(d);
}
static void Login(ClientContext &context, const FunctionParameters &p) {
	CheckExternal(context);
	Connection c(DatabaseInstance::GetDatabase(context));
	auto name = p.values[0].ToString();
	auto started = Query(c, "CALL mcp_login_begin(" + Quote(name) + ")");
	std::cerr << "MCP login: " << started->GetValue(0, 0).ToString() << std::endl;
	Query(c, "CALL mcp_login_finish(" + Quote(name) + ")");
}
static string Discover(ClientContext &context, const FunctionParameters &p) {
	CheckExternal(context);
	auto name = p.values[0].ToString();
	Connection c(DatabaseInstance::GetDatabase(context));
	auto tools = Query(c, "SELECT name,definition FROM mcp_tools(" + Quote(name) + ")");
	string sql;
	for (idx_t i = 0; i < tools->RowCount(); i++) {
		auto tool = tools->GetValue(0, i).ToString();
		auto definition = tools->GetValue(1, i).ToString();
		auto schema = Json::parse(definition).value("inputSchema", Json::object());
		auto props = schema.value("properties", Json::object());
		string params, args;
		std::set<string> seen;
		for (auto it = props.begin(); it != props.end(); ++it) {
			if (!seen.insert(StringUtil::Lower(it.key())).second)
				throw InvalidInputException("Case-colliding tool parameters");
			if (!params.empty()) {
				params += ",";
				args += ",";
			}
			params += Ident(it.key()) + " := NULL";
			args += Ident(it.key()) + " := " + Ident(it.key());
		}
		args = args.empty() ? "'{}'" : "struct_pack(" + args + ")";
		sql += "CREATE MACRO " + Ident(CatalogName(context)) + "." + Ident(name) + "." + Ident(tool) + "(" + params +
		       ") AS TABLE SELECT * FROM mcp_tool(server := " + Quote(name) + ",tool := " + Quote(tool) +
		       ",args := " + args + ",omit_nulls := true);";
		sql += "INSERT INTO " + Meta(context) + "tools(server,name,definition) VALUES(" + Quote(name) + "," +
		       Quote(tool) + "," + Quote(definition) + ");";
	}
	return sql.empty() ? "SELECT true AS discovered;" : sql;
}
static void Load(ExtensionLoader &loader) {
	RegisterRemoteSecrets(loader);
	auto runtime = make_shared_ptr<Runtime>();
	TableFunction tool("mcp_tool", {}, Scan, BindTool, Init);
	tool.named_parameters = {{"server", LogicalType::VARCHAR},
	                         {"tool", LogicalType::VARCHAR},
	                         {"args", LogicalType::ANY},
	                         {"omit_nulls", LogicalType::BOOLEAN}};
	tool.function_info = make_shared_ptr<Info>(runtime);
	loader.RegisterFunction(tool);
	TableFunction tools("mcp_tools", {LogicalType::VARCHAR}, Scan, BindTools, Init);
	tools.function_info = make_shared_ptr<Info>(runtime);
	loader.RegisterFunction(tools);
	TableFunction servers("mcp_servers", {}, Scan, BindServers, Init);
	servers.function_info = make_shared_ptr<Info>(runtime);
	loader.RegisterFunction(servers);
	loader.RegisterFunction(PragmaFunction::PragmaCall(
	    "mcp_register", Register, {LogicalType::VARCHAR, LogicalType::VARCHAR, LogicalType::VARCHAR}));
	loader.RegisterFunction(PragmaFunction::PragmaCall("mcp_discover", Discover, {LogicalType::VARCHAR}));
	PragmaFunctionSet registration("mcp_register_http");
	for (idx_t count = 2; count <= 4; count++)
		registration.AddFunction(PragmaFunction::PragmaCall("mcp_register_http", RegisterHTTP,
		                                                    vector<LogicalType>(count, LogicalType::VARCHAR)));
	loader.RegisterFunction(registration);
	for (auto name : {"mcp_login_begin", "mcp_login_finish"}) {
		TableFunction login(name, {LogicalType::VARCHAR}, Scan, BindLogin, Init);
		login.function_info = make_shared_ptr<Info>(runtime);
		if (string(name) == "mcp_login_begin")
			login.named_parameters = {{"open_browser", LogicalType::BOOLEAN}};
		loader.RegisterFunction(login);
	}
	loader.RegisterFunction(PragmaFunction::PragmaCall("mcp_login", Login, {LogicalType::VARCHAR}));
}
} // namespace mcp_context
} // namespace duckdb
extern "C" {
DUCKDB_CPP_EXTENSION_ENTRY(mcp_context, loader) {
	duckdb::mcp_context::Load(loader);
}
}
