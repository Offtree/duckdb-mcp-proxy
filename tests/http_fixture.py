"""Stateless MCP + OAuth authorization server, using only the Python stdlib.

The authorization endpoint automatically grants consent for tests. No credentials
leave loopback. The fixture verifies PKCE, redirect/client binding and resource
indicators, and rotates refresh tokens on every refresh.
"""
import base64
import hashlib
import json
import threading
import subprocess
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


class Fixture:
    def __init__(self):
        self.events = []
        self.clients = {}
        self.codes = {}
        self.access_tokens = set()
        self.refresh_tokens = set()
        self.counter = 0
        self.lock = threading.RLock()
        self.reject_refresh = False
        self.transient_refresh = False
        self.omit_refresh_scope = False
        self.callback_issuer = None
        self.wrong_state = False
        self.issuer_suffix = ""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url = self.base + "/mcp"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                with fixture.lock:
                    parts = urlsplit(self.path)
                    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
                    if parts.path == "/_test/events":
                        self.reply(200, fixture.events)
                    elif parts.path.startswith("/.well-known/oauth-protected-resource"):
                        suffix = parts.path[len("/.well-known/oauth-protected-resource"):]
                        self.reply(200, {"resource": fixture.base + (suffix or "/mcp"),
                            "authorization_servers": [fixture.base + fixture.issuer_suffix],
                            "scopes_supported": ["issues:read", "offline_access"]})
                    elif parts.path.startswith("/.well-known/oauth-authorization-server"):
                        self.reply(200, {"issuer": fixture.base + fixture.issuer_suffix,
                            "authorization_endpoint": fixture.base + "/authorize",
                            "token_endpoint": fixture.base + "/token",
                            "registration_endpoint": fixture.base + "/register",
                            "response_types_supported": ["code"],
                            "grant_types_supported": ["authorization_code", "refresh_token"],
                            "token_endpoint_auth_methods_supported": ["none"],
                            "code_challenge_methods_supported": ["S256"],
                            "authorization_response_iss_parameter_supported": True,
                            "scopes_supported": ["issues:read", "offline_access"]})
                    elif parts.path == "/authorize":
                        client = fixture.clients.get(params.get("client_id"))
                        if not client or params.get("redirect_uri") not in client["redirect_uris"]:
                            self.reply(400, {"error": "invalid_client"})
                            return
                        if params.get("code_challenge_method") != "S256" or not params.get("state"):
                            self.reply(400, {"error": "PKCE/state required"})
                            return
                        fixture.counter += 1
                        code = f"fixture-code-{fixture.counter}"
                        fixture.codes[code] = params
                        fixture.events.append({"event": "authorize", "params": params})
                        query = urlencode({"code": code,
                            "state": "wrong" if fixture.wrong_state else params["state"],
                            "iss": fixture.callback_issuer or fixture.base + fixture.issuer_suffix})
                        self.send_response(302)
                        self.send_header("Location", params["redirect_uri"] + "?" + query)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        self.reply(404, {})

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with fixture.lock:
                    if self.path == "/_test/config":
                        for key, value in json.loads(data).items():
                            if key in FixtureProcess.FLAGS:
                                setattr(fixture, key, value)
                        self.reply(200, {})
                    elif self.path == "/register":
                        request = json.loads(data)
                        if request.get("application_type") != "native" or request.get("token_endpoint_auth_method") != "none":
                            self.reply(400, {"error": "native public client required"})
                            return
                        client_id = f"fixture-client-{len(fixture.clients)+1}"
                        fixture.clients[client_id] = request
                        fixture.events.append({"event": "register"})
                        self.reply(201, {**request, "client_id": client_id})
                    elif self.path == "/token":
                        params = {k: v[0] for k, v in parse_qs(data.decode()).items()}
                        grant = params.get("grant_type")
                        fixture.events.append({"event": "token", "grant": grant, "scope": params.get("scope", "")})
                        if grant == "authorization_code":
                            auth = fixture.codes.pop(params.get("code"), None)
                            challenge = base64.urlsafe_b64encode(hashlib.sha256(params.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
                            if not auth or challenge != auth["code_challenge"] or params.get("client_id") != auth["client_id"] or params.get("redirect_uri") != auth["redirect_uri"] or params.get("resource") != auth.get("resource"):
                                self.reply(400, {"error": "invalid_grant"})
                                return
                        elif grant == "refresh_token":
                            if fixture.transient_refresh:
                                self.reply(503, {"error": "temporarily_unavailable"})
                                return
                            if fixture.reject_refresh or params.get("refresh_token") not in fixture.refresh_tokens:
                                self.reply(400, {"error": "invalid_grant"})
                                return
                            fixture.refresh_tokens.remove(params["refresh_token"])
                        else:
                            self.reply(400, {"error": "unsupported_grant_type"})
                            return
                        if not params.get("resource", "").startswith(fixture.base + "/"):
                            self.reply(400, {"error": "resource required"})
                            return
                        fixture.counter += 1
                        access, refresh = f"fixture-access-{fixture.counter}", f"fixture-refresh-{fixture.counter}"
                        fixture.access_tokens.add(access)
                        fixture.refresh_tokens.add(refresh)
                        result = {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                            "expires_in": 1, "scope": "issues:read offline_access"}
                        if grant == "refresh_token" and fixture.omit_refresh_scope:
                            del result["scope"]
                        self.reply(200, result)
                    elif self.path.startswith("/mcp") or self.path == "/public":
                        request = json.loads(data)
                        method = request.get("method")
                        params = request.get("params", {})
                        fixture.events.append({"event": "mcp", "method": method, "params": params,
                            "session_header": self.headers.get("Mcp-Session-Id")})
                        if self.headers.get("Mcp-Session-Id") or self.headers.get("MCP-Protocol-Version") != "2026-07-28" or self.headers.get("Mcp-Method") != method or params.get("_meta", {}).get("io.modelcontextprotocol/protocolVersion") != "2026-07-28" or params.get("_meta", {}).get("io.modelcontextprotocol/clientCapabilities") != {}:
                            self.reply(400, {"error": "stateless metadata required"})
                            return
                        if method in ("initialize", "notifications/initialized"):
                            self.reply(400, {"error": "no handshake"})
                            return
                        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                        if self.path != "/public" and token not in fixture.access_tokens:
                            self.reply(401, {}, {"WWW-Authenticate": f'Bearer resource_metadata="{fixture.base}/.well-known/oauth-protected-resource{self.path}"'})
                            return
                        if method == "tools/list":
                            result = {"tools": [{"name": "search", "description": "Remote fixture search",
                                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                                "outputSchema": {"type": "array", "items": {"type": "object", "properties": {
                                    "id": {"type": "integer"}, "title": {"type": "string"}, "author": {"type": "string"}}}}}],
                                "ttlMs": 0, "cacheScope": "private"}
                        elif method == "tools/call":
                            if self.headers.get("Mcp-Name") != params.get("name"):
                                self.reply(400, {"error": "Mcp-Name mismatch"})
                                return
                            query = params["arguments"]["query"]
                            if query == "http_error":
                                self.reply(503, {"sensitive": "fixture-access-do-not-print"})
                                return
                            if query == "input_required":
                                self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": {"resultType": "input_required"}})
                                return
                            result = {"structuredContent": [{"id": 1, "title": f"{query} from remote", "author": "alice"}]}
                        else:
                            self.reply(400, {"error": "unexpected method"})
                            return
                        response = {"jsonrpc": "2.0", "id": request["id"], "result": {**result, "resultType": "complete"}}
                        if self.path.endswith("/sse"):
                            body = ('data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
                                + "event: message\ndata: " + json.dumps(response) + "\n\n").encode()
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                        else:
                            self.reply(200, response)
                    else:
                        self.reply(404, {})

        return Handler


class FixtureProcess:
    """Run independently of an embedded Python host's GIL during PRAGMA binding."""
    FLAGS = {"reject_refresh", "transient_refresh", "omit_refresh_scope", "callback_issuer", "wrong_state", "issuer_suffix"}

    def __init__(self):
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--json"],
                                        stdout=subprocess.PIPE, text=True)
        info = json.loads(self.process.stdout.readline())
        self.base, self.url = info["base"], info["url"]

    @property
    def events(self):
        with urlopen(self.base + "/_test/events", timeout=5) as response:
            return json.load(response)

    def __setattr__(self, name, value):
        if name in self.FLAGS:
            request = Request(self.base + "/_test/config", data=json.dumps({name: value}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5):
                pass
        else:
            object.__setattr__(self, name, value)

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        self.process.stdout.close()


if __name__ == "__main__":
    fixture = Fixture()
    if "--json" in sys.argv:
        print(json.dumps({"base": fixture.base, "url": fixture.url}), flush=True)
    else:
        print(f"MCP: {fixture.url}\nPublic MCP: {fixture.base}/public", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fixture.close()
