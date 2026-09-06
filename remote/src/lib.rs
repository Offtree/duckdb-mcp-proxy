//! Synchronous C ABI around a current-thread async runtime.
//! OAuth state lives only between explicit login_begin/login_finish operations.
use async_trait::async_trait;
use futures::StreamExt;
use reqwest::header::{AUTHORIZATION, HeaderMap, HeaderName, HeaderValue};
use reqwest::{Client, StatusCode, Url};
use rmcp::transport::auth::{
    AuthError, AuthorizationManager, AuthorizationRequest, AuthorizationSession,
    CredentialRefreshGuard, CredentialStore, StoredCredentials,
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    ffi::{CStr, CString, c_char, c_void},
    panic::{AssertUnwindSafe, catch_unwind},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};

type Result<T> = std::result::Result<T, String>;
type SaveFn = unsafe extern "C" fn(*mut c_void, *const c_char) -> i32;
const VERSION: &str = "2026-07-28";
const VERSIONS: &[&str] = &[
    VERSION,
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
    "2024-10-07",
];
const MAX_BODY: usize = 32 * 1024 * 1024;
const LOGIN_TTL: Duration = Duration::from_secs(180);

// The callback is installed only during an FFI call. The current-thread runtime
// drives all SDK futures on that caller thread; no DuckDB context crosses threads.
#[derive(Clone, Copy)]
struct Sink {
    save: SaveFn,
    context: usize,
}
#[derive(Default)]
struct StoreInner {
    credentials: Option<StoredCredentials>,
    sink: Option<Sink>,
}
#[derive(Clone, Default)]
struct Store(Arc<Mutex<StoreInner>>);
struct SinkGuard(Store);
impl Drop for SinkGuard {
    fn drop(&mut self) {
        self.0.0.lock().unwrap().sink = None;
    }
}
impl Store {
    fn new(value: &Value) -> Result<Self> {
        let store = Self::default();
        if !value.is_null() {
            store.0.lock().unwrap().credentials = Some(
                serde_json::from_value(value.clone())
                    .map_err(|_| "Invalid stored OAuth credentials".to_owned())?,
            );
        }
        Ok(store)
    }
    fn install(&self, sink: Sink) -> SinkGuard {
        self.0.lock().unwrap().sink = Some(sink);
        SinkGuard(self.clone())
    }
}
#[async_trait]
impl CredentialStore for Store {
    async fn acquire_refresh_guard(
        &self,
    ) -> std::result::Result<Option<CredentialRefreshGuard>, AuthError> {
        // C++ holds the credential mutex from loading the snapshot until the
        // FFI operation returns. Signal that coordination to the SDK, which
        // also preserves stored scopes when a refresh response omits scope.
        Ok(Some(CredentialRefreshGuard::new(())))
    }
    async fn load(&self) -> std::result::Result<Option<StoredCredentials>, AuthError> {
        Ok(self.0.lock().unwrap().credentials.clone())
    }
    async fn save(&self, credentials: StoredCredentials) -> std::result::Result<(), AuthError> {
        let mut inner = self.0.lock().unwrap();
        let serialized = CString::new(
            serde_json::to_string(&credentials)
                .map_err(|_| AuthError::CredentialStoreError("Serialization failed".into()))?,
        )
        .unwrap();
        let sink = inner
            .sink
            .ok_or_else(|| AuthError::CredentialStoreError("No active secret store".into()))?;
        if unsafe { (sink.save)(sink.context as *mut c_void, serialized.as_ptr()) } != 0 {
            return Err(AuthError::CredentialStoreError(
                "Could not persist DuckDB secret".into(),
            ));
        }
        inner.credentials = Some(credentials);
        Ok(())
    }
    async fn clear(&self) -> std::result::Result<(), AuthError> {
        let mut inner = self.0.lock().unwrap();
        let sink = inner
            .sink
            .ok_or_else(|| AuthError::CredentialStoreError("No active secret store".into()))?;
        if unsafe { (sink.save)(sink.context as *mut c_void, c"null".as_ptr()) } != 0 {
            return Err(AuthError::CredentialStoreError(
                "Could not clear DuckDB secret".into(),
            ));
        }
        inner.credentials = None;
        Ok(())
    }
}

fn auth_error(error: AuthError) -> String {
    // Provider responses may contain credentials: expose categories, never bodies.
    match error {
        AuthError::AuthorizationRequired | AuthError::TokenRefreshRejected(_) => {
            "OAuth login required; run PRAGMA mcp_login('server_name')".into()
        }
        AuthError::CredentialStoreError(_) => "OAuth credential storage failed".into(),
        AuthError::TokenRefreshFailed(_) => {
            "OAuth refresh failed; credentials retained, retry later".into()
        }
        AuthError::AuthorizationServerMismatch { .. }
        | AuthError::AuthorizationServerMissingIssuer { .. } => {
            "OAuth issuer validation failed".into()
        }
        AuthError::RegistrationFailed(_) => {
            "OAuth client registration failed; configure client_id or client_metadata_url".into()
        }
        AuthError::PkceUnsupported => "Authorization server does not support PKCE S256".into(),
        AuthError::MetadataError(_) | AuthError::NoAuthorizationSupport => {
            "OAuth metadata discovery failed".into()
        }
        _ => "OAuth authorization failed (callback, state, code, or provider response invalid)"
            .into(),
    }
}

fn endpoint(text: &str) -> Result<Url> {
    let url = Url::parse(text).map_err(|_| "Invalid MCP URL")?;
    let loopback = url.host_str().is_some_and(|host| {
        host == "localhost"
            || host
                .trim_start_matches('[')
                .trim_end_matches(']')
                .parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback())
    });
    if (url.scheme() != "https" && !(url.scheme() == "http" && loopback))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || url.query().is_some()
    {
        return Err(
            "Use an HTTPS MCP URL (HTTP allowed on loopback), without userinfo, query, or fragment"
                .into(),
        );
    }
    Ok(url)
}

fn secret_headers(input: &Value) -> Result<HeaderMap> {
    let mut headers = HeaderMap::new();
    if !input["header_auth"].as_bool().unwrap_or(false) {
        return Ok(headers);
    }
    let values = input["headers"]
        .as_object()
        .ok_or("HTTP secret headers must be an object")?;
    for (name, value) in values {
        let name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| "Invalid HTTP secret header name")?;
        if name.as_str().starts_with("mcp-")
            || matches!(
                name.as_str(),
                "host"
                    | "content-length"
                    | "content-type"
                    | "accept"
                    | "connection"
                    | "transfer-encoding"
                    | "te"
                    | "trailer"
                    | "upgrade"
                    | "proxy-authorization"
                    | "proxy-connection"
            )
        {
            return Err("HTTP secret cannot override MCP protocol or transport headers".into());
        }
        if headers.contains_key(&name) {
            return Err("Duplicate HTTP secret header name (case-insensitive)".into());
        }
        let mut value = HeaderValue::from_str(
            value
                .as_str()
                .ok_or("HTTP secret header value must be a string")?,
        )
        .map_err(|_| "Invalid HTTP secret header value")?;
        value.set_sensitive(true);
        headers.insert(name, value);
    }
    if let Some(token) = input["bearer_token"].as_str() {
        if token.is_empty() {
            return Err("HTTP secret bearer_token cannot be empty".into());
        }
        if headers.contains_key(AUTHORIZATION) {
            return Err("Specify bearer_token or an Authorization header, not both".into());
        }
        let mut value = HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| "Invalid HTTP secret bearer_token")?;
        value.set_sensitive(true);
        headers.insert(AUTHORIZATION, value);
    }
    if headers.is_empty() {
        return Err("HTTP authentication secret has no bearer_token or extra_http_headers".into());
    }
    Ok(headers)
}

struct Pending {
    session: AuthorizationSession,
    listener: TcpListener,
    store: Store,
    created: Instant,
    secret_name: String,
}
struct Remote {
    client: Client,
    pending: HashMap<String, Pending>,
    challenges: HashMap<String, String>,
    versions: HashMap<String, String>,
    sessions: HashMap<String, String>,
    next_id: u64,
}
pub struct Bridge {
    runtime: tokio::runtime::Runtime,
    remote: Remote,
}

async fn manager(url: &str, store: Store, challenge: Option<&str>) -> Result<AuthorizationManager> {
    let mut manager = AuthorizationManager::new(url).await.map_err(auth_error)?;
    manager.set_credential_store(store);
    let metadata = manager
        .resolve_metadata_from_challenge(challenge)
        .await
        .map_err(auth_error)?;
    if !metadata.source.is_discovered() {
        return Err("OAuth server must publish discovery metadata".into());
    }
    manager.set_metadata(metadata.metadata);
    Ok(manager)
}

impl Remote {
    async fn execute(&mut self, input: Value, sink: Sink) -> Result<Value> {
        self.pending.retain(|_, p| p.created.elapsed() < LOGIN_TTL);
        let url = endpoint(input["url"].as_str().ok_or("Missing URL")?)?.to_string();
        let key = format!("{}\n{}", url, input["secret_name"].as_str().unwrap_or(""));
        match input["op"].as_str().ok_or("Missing operation")? {
            "validate" => Ok(json!({"url":url})),
            "login_begin" => {
                if input["secret_name"].as_str().unwrap_or("").is_empty() {
                    return Err("Registration needs a secret_name for OAuth login".into());
                }
                let options = &input["options"];
                let port = options["redirect_port"].as_u64().unwrap_or(0);
                let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port as u16))
                    .await
                    .map_err(|_| "Cannot bind OAuth loopback callback listener")?;
                let redirect = format!(
                    "http://127.0.0.1:{}/callback",
                    listener.local_addr().unwrap().port()
                );
                let store = Store::default();
                let _guard = store.install(sink);
                let manager = manager(
                    &url,
                    store.clone(),
                    self.challenges.get(&url).map(String::as_str),
                )
                .await?;
                let mut request = AuthorizationRequest::new(redirect.clone())
                    .with_client_name("DuckDB MCP Context")
                    .with_application_type("native");
                if let Some(id) = options["client_id"].as_str() {
                    request = request.with_preregistered_client(id);
                }
                if let Some(metadata_url) = options["client_metadata_url"].as_str() {
                    request = request.with_client_metadata_url(metadata_url);
                }
                if let Some(scopes) = options["scopes"].as_array() {
                    request = request.with_scopes(scopes.iter().filter_map(Value::as_str));
                }
                let session = AuthorizationSession::new(manager, request)
                    .await
                    .map_err(|(_, e)| auth_error(e))?;
                let authorization_url = session.auth_url.clone();
                self.pending.insert(
                    key,
                    Pending {
                        session,
                        listener,
                        store,
                        created: Instant::now(),
                        secret_name: input["secret_name"].as_str().unwrap().into(),
                    },
                );
                let browser_opened = input["open_browser"].as_bool().unwrap_or(true)
                    && webbrowser::open(&authorization_url).is_ok();
                Ok(
                    json!({"authorization_url":authorization_url,"callback_url":redirect,
                    "browser_opened":browser_opened,"expires_in":LOGIN_TTL.as_secs()}),
                )
            }
            "login_finish" => {
                let pending = self
                    .pending
                    .remove(&key)
                    .ok_or("No pending login; run mcp_login_begin first")?;
                if pending.secret_name != input["secret_name"].as_str().unwrap_or("") {
                    return Err("Login secret reference changed".into());
                }
                let _guard = pending.store.install(sink);
                let remaining = LOGIN_TTL.saturating_sub(pending.created.elapsed());
                tokio::time::timeout(remaining, finish_login(pending))
                    .await
                    .map_err(|_| "OAuth login timed out; start login again".to_owned())??;
                Ok(json!({"authenticated":true}))
            }
            "request" => {
                let headers = secret_headers(&input)?;
                let store = Store::new(&input["credentials"])?;
                let _guard = store.install(sink);
                let token = if input["credentials"].is_null() {
                    None
                } else {
                    let mut manager =
                        manager(&url, store, self.challenges.get(&url).map(String::as_str)).await?;
                    if !manager.initialize_from_store().await.map_err(auth_error)? {
                        return Err(
                            "OAuth login required; stored credentials are absent or issuer changed"
                                .into(),
                        );
                    }
                    Some(manager.get_access_token().await.map_err(auth_error)?)
                };
                self.request(
                    &url,
                    input["method"].as_str().ok_or("Missing method")?,
                    input["params"].clone(),
                    token,
                    headers,
                    &input,
                )
                .await
            }
            _ => Err("Unknown remote bridge operation".into()),
        }
    }

    async fn request(
        &mut self,
        url: &str,
        method: &str,
        params: Value,
        token: Option<String>,
        headers: HeaderMap,
        input: &Value,
    ) -> Result<Value> {
        if let Some(version) = input["options"]["protocol_version"].as_str() {
            return self
                .request_version(url, method, params, token, headers, input, version)
                .await;
        }
        let key = format!("{url}\n{}\n{}", input["secret_name"], input["options"]);
        if let Some(version) = self.versions.get(&key).cloned() {
            let result = self
                .request_version(url, method, params, token, headers, input, &version)
                .await;
            if result.is_err() {
                // Reconnect on a later query; never replay this request.
                self.versions.remove(&key);
                self.sessions.remove(&key);
            }
            return result;
        }
        // Negotiate using a read-only request, including after reopening a database
        // whose persisted macros may invoke tools/call before any discovery.
        self.sessions.remove(&key);
        let probe_params = if method == "tools/list" {
            params.clone()
        } else {
            json!({})
        };
        for (index, version) in VERSIONS.iter().enumerate() {
            let mut selected = (*version).to_owned();
            let mut probe = self
                .request_version(
                    url,
                    "tools/list",
                    probe_params.clone(),
                    token.clone(),
                    headers.clone(),
                    input,
                    &selected,
                )
                .await;
            if let Err(error) = &probe {
                let lower = error.to_ascii_lowercase();
                if *version != VERSION
                    && (lower.contains("session") || lower.contains("initializ"))
                    && (lower.starts_with("mcp http status 400")
                        || lower.starts_with("mcp json-rpc error"))
                {
                    let initialized = self
                        .request_version(
                            url,
                            "initialize",
                            json!({
                                "protocolVersion": selected,
                                "capabilities": {},
                                "clientInfo": {"name": "duckdb-mcp-context", "version": "0.2.0"}
                            }),
                            token.clone(),
                            headers.clone(),
                            input,
                            &selected,
                        )
                        .await?;
                    let negotiated = initialized["protocolVersion"]
                        .as_str()
                        .ok_or("MCP initialize response missing protocolVersion")?;
                    if !VERSIONS.contains(&negotiated) || negotiated == VERSION {
                        self.sessions.remove(&key);
                        return Err("Unsupported MCP initialize protocol version".into());
                    }
                    selected = negotiated.into();
                    self.request_version(
                        url,
                        "notifications/initialized",
                        json!({}),
                        token.clone(),
                        headers.clone(),
                        input,
                        &selected,
                    )
                    .await?;
                    probe = self
                        .request_version(
                            url,
                            "tools/list",
                            probe_params.clone(),
                            token.clone(),
                            headers.clone(),
                            input,
                            &selected,
                        )
                        .await;
                }
            }
            match probe {
                Ok(result) => {
                    self.versions.insert(key.clone(), selected.clone());
                    if method == "tools/list" {
                        return Ok(result);
                    }
                    let result = self
                        .request_version(url, method, params, token, headers, input, &selected)
                        .await;
                    if result.is_err() {
                        self.versions.remove(&key);
                        self.sessions.remove(&key);
                    }
                    return result;
                }
                Err(error) => {
                    // Version rejection or a legacy response permits another probe.
                    // Auth, network, parsing and ordinary server errors are final.
                    let lower = error.to_ascii_lowercase();
                    let legacy_response = *version == VERSION
                        && error == "MCP 2026-07-28 response missing resultType";
                    if index + 1 == VERSIONS.len()
                        || !(legacy_response
                            || ((lower.starts_with("mcp http status 400")
                                || lower.starts_with("mcp json-rpc error"))
                                && (lower.contains("unsupported protocol version")
                                    || lower.contains("unsupported mcp protocol version")
                                    || (*version == VERSION
                                        && (lower.contains("session")
                                            || lower.contains("initializ"))))))
                    {
                        return Err(error);
                    }
                }
            }
        }
        unreachable!()
    }

    #[allow(clippy::too_many_arguments)]
    async fn request_version(
        &mut self,
        url: &str,
        method: &str,
        mut params: Value,
        token: Option<String>,
        headers: HeaderMap,
        input: &Value,
        version: &str,
    ) -> Result<Value> {
        let header_auth = input["header_auth"].as_bool().unwrap_or(false);
        if !VERSIONS.contains(&version) {
            return Err("Unsupported MCP protocol_version".into());
        }
        let id = self.next_id;
        self.next_id += 1;
        if version == VERSION {
            params["_meta"] = json!({
                "io.modelcontextprotocol/protocolVersion": VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name":"duckdb-mcp-context","version":"0.2.0"}
            });
        }
        let mut request = self
            .client
            .post(url)
            .headers(headers)
            .header("MCP-Protocol-Version", version)
            .header("Mcp-Method", method)
            .header("Accept", "application/json, text/event-stream");
        if method == "tools/call" {
            request = request.header(
                "Mcp-Name",
                params["name"].as_str().ok_or("Missing tool name")?,
            );
        }
        if let Some(token) = token {
            request = request.bearer_auth(token);
        }
        let key = format!("{url}\n{}\n{}", input["secret_name"], input["options"]);
        if method != "initialize" && let Some(session) = self.sessions.get(&key) {
            request = request.header("Mcp-Session-Id", session);
        }
        let mut body = json!({"jsonrpc":"2.0","method":method,"params":params});
        if method != "notifications/initialized" {
            body["id"] = json!(id);
        }
        // No redirects or automatic retry of tool requests.
        let response = request
            .json(&body)
            .send()
            .await
            .map_err(|_| "MCP HTTP request failed (network or TLS); request was not retried")?;
        if response.status() == StatusCode::UNAUTHORIZED {
            if header_auth {
                return Err(
                    "MCP HTTP authentication rejected (401); update the referenced HTTP secret"
                        .into(),
                );
            }
            if let Some(challenge) = response
                .headers()
                .get("WWW-Authenticate")
                .and_then(|h| h.to_str().ok())
            {
                self.challenges.insert(url.into(), challenge.into());
            }
            return Err("OAuth login required; run PRAGMA mcp_login('server_name')".into());
        }
        if response.status() == StatusCode::FORBIDDEN {
            if header_auth {
                return Err(
                    "MCP access denied (403); check the referenced HTTP secret and its permissions"
                        .into(),
                );
            }
            return Err("MCP access denied; login with the required scopes".into());
        }
        if !response.status().is_success() {
            let status = response.status();
            let mut stream = response.bytes_stream();
            let mut body = Vec::new();
            while let Some(Ok(part)) = stream.next().await {
                if body.len() + part.len() > 65536 {
                    break;
                }
                body.extend_from_slice(&part);
            }
            let detail = serde_json::from_slice::<Value>(&body)
                .ok()
                .and_then(|v| {
                    v["error"]["message"]
                        .as_str()
                        .or_else(|| v["error"].as_str())
                        .map(str::to_owned)
                })
                .map(|s| {
                    s.chars()
                        .filter(|c| !c.is_control())
                        .take(2048)
                        .collect::<String>()
                })
                .unwrap_or_default();
            return Err(format!(
                "MCP HTTP status {status}; protocol_version={version}; request was not retried{}",
                if detail.is_empty() {
                    String::new()
                } else {
                    format!(": {detail}")
                }
            ));
        }
        if method == "notifications/initialized" {
            return Ok(json!({}));
        }
        let session = response
            .headers()
            .get("Mcp-Session-Id")
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let content_type = response
            .headers()
            .get("Content-Type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_owned();
        let message = if content_type.starts_with("text/event-stream") {
            let mut received = 0usize;
            let stream = response.bytes_stream().map(move |part| {
                let part = part.map_err(std::io::Error::other)?;
                received += part.len();
                if received > MAX_BODY {
                    return Err(std::io::Error::other("MCP response exceeds limit"));
                }
                Ok(part)
            });
            let stream = sse_stream::SseStream::from_bytes_stream(stream);
            tokio::pin!(stream);
            let mut found = None;
            while let Some(event) = stream.next().await {
                let event = event.map_err(|_| "Invalid or oversized MCP SSE response")?;
                if let Some(data) = event.data {
                    // SSE priming/keepalive events can carry an empty data field.
                    if data.trim().is_empty() {
                        continue;
                    }
                    let message: Value = serde_json::from_str(&data)
                        .map_err(|_| "Invalid MCP JSON in SSE response")?;
                    if message.get("id").is_some() {
                        found = Some(message);
                        break;
                    }
                    // Notifications require no response; server requests are not supported.
                }
            }
            found.ok_or("MCP SSE stream ended without a response")?
        } else if content_type.starts_with("application/json") {
            let mut stream = response.bytes_stream();
            let mut body = Vec::new();
            while let Some(part) = stream.next().await {
                let part = part.map_err(|_| "MCP HTTP response read failed")?;
                if body.len() + part.len() > MAX_BODY {
                    return Err("MCP response exceeds 32 MiB limit".into());
                }
                body.extend_from_slice(&part);
            }
            serde_json::from_slice(&body).map_err(|_| "Invalid MCP JSON response")?
        } else {
            return Err("Expected MCP application/json or text/event-stream response".into());
        };
        if message["jsonrpc"] != "2.0" || message["id"] != id {
            return Err("MCP response version or ID mismatch".into());
        }
        if let Some(error) = message.get("error") {
            let detail: String = error["message"]
                .as_str()
                .unwrap_or("")
                .chars()
                .filter(|c| !c.is_control())
                .take(2048)
                .collect();
            return Err(format!("MCP JSON-RPC error {}: {detail}", error["code"]));
        }
        let result = message.get("result").ok_or("MCP response missing result")?;
        match result["resultType"].as_str() {
            Some("input_required") => return Err(
                "MCP tool requires additional input; multi-round-trip execution is not supported"
                    .into(),
            ),
            Some("complete") => {}
            None if version != VERSION && result.get("resultType").is_none() => {}
            None if result.get("resultType").is_none() => {
                return Err("MCP 2026-07-28 response missing resultType".into());
            }
            _ => return Err("Expected MCP 2026-07-28 resultType 'complete'".into()),
        }
        if method == "initialize" && let Some(session) = session {
            self.sessions.insert(key, session);
        }
        Ok(result.clone())
    }
}

async fn finish_login(pending: Pending) -> Result<()> {
    loop {
        let (mut socket, _) = pending
            .listener
            .accept()
            .await
            .map_err(|_| "OAuth callback listener failed")?;
        let mut bytes = Vec::new();
        let read = tokio::time::timeout(Duration::from_secs(5), async {
            while !bytes.windows(4).any(|w| w == b"\r\n\r\n") && bytes.len() < 16384 {
                let mut buf = [0u8; 1024];
                let n = socket.read(&mut buf).await?;
                if n == 0 {
                    break;
                }
                bytes.extend_from_slice(&buf[..n]);
            }
            Ok::<_, std::io::Error>(())
        })
        .await;
        if !matches!(read, Ok(Ok(()))) {
            continue;
        }
        let request = String::from_utf8_lossy(&bytes);
        let mut words = request.lines().next().unwrap_or("").split_whitespace();
        let method = words.next().unwrap_or("");
        let path = words.next().unwrap_or("");
        if method != "GET" || !path.starts_with("/callback?") {
            let _ = socket
                .write_all(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .await;
            continue;
        }
        let redirect = Url::parse(&pending.session.redirect_uri).unwrap();
        let callback = format!("{}{}", redirect.origin().ascii_serialization(), path);
        let result = pending
            .session
            .handle_callback_url(&callback)
            .await
            .map_err(auth_error);
        let (status, body) = if result.is_ok() {
            (
                "200 OK",
                "DuckDB MCP login complete. You can close this tab.",
            )
        } else {
            (
                "400 Bad Request",
                "DuckDB MCP login failed. Return to DuckDB for details.",
            )
        };
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = socket.write_all(response.as_bytes()).await;
        return result.map(|_| ());
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn mcp_remote_new() -> *mut Bridge {
    catch_unwind(|| {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .ok()?;
        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .ok()?;
        Some(Box::into_raw(Box::new(Bridge {
            runtime,
            remote: Remote {
                client,
                pending: HashMap::new(),
                challenges: HashMap::new(),
                versions: HashMap::new(),
                sessions: HashMap::new(),
                next_id: 1,
            },
        })))
    })
    .ok()
    .flatten()
    .unwrap_or(std::ptr::null_mut())
}

/// Inputs and save callback remain valid for this call only. The returned string
/// is owned by Rust and must be freed with mcp_remote_string_free.
///
/// # Safety
/// `bridge` must be a live handle from `mcp_remote_new`, exclusively borrowed for
/// this call. `input` must be a valid NUL-terminated string. `context` and `save`
/// must remain valid until return; the callback must not unwind or reenter this
/// bridge. The caller must serialize access to credentials for the operation.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn mcp_remote_execute(
    bridge: *mut Bridge,
    input: *const c_char,
    context: *mut c_void,
    save: SaveFn,
) -> *mut c_char {
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<Value> {
        let bridge = unsafe { bridge.as_mut() }.ok_or("Missing remote bridge")?;
        let input: Value = serde_json::from_slice(unsafe { CStr::from_ptr(input) }.to_bytes())
            .map_err(|_| "Invalid bridge input")?;
        bridge.runtime.block_on(bridge.remote.execute(
            input,
            Sink {
                save,
                context: context as usize,
            },
        ))
    }));
    let value = match result {
        Ok(Ok(value)) => json!({"ok":value}),
        Ok(Err(error)) => json!({"error":error}),
        Err(_) => json!({"error":"Remote bridge internal panic"}),
    };
    CString::new(value.to_string()).unwrap().into_raw()
}
/// # Safety
/// `value` must be null or an unfreed string returned by `mcp_remote_execute`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn mcp_remote_string_free(value: *mut c_char) {
    if !value.is_null() {
        drop(unsafe { CString::from_raw(value) });
    }
}
/// # Safety
/// `bridge` must be null or a live, exclusively owned `mcp_remote_new` handle,
/// and may be freed only once, with no concurrent execution in progress.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn mcp_remote_free(bridge: *mut Bridge) {
    if !bridge.is_null() {
        drop(unsafe { Box::from_raw(bridge) });
    }
}
