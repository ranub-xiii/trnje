"""Small, stdlib-only MCP Streamable HTTP resource server for TRNJE.

OAuth is delegated to a configured OAuth 2.1 authorization server. This module
implements the protected-resource side and validates opaque access tokens via
RFC 7662 introspection; it is not itself an authorization server.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from trnje_api_queue import IdempotencyConflict, QueueFull


MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOLS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
)
SUPPORTED_PROTOCOLS = (MODERN_PROTOCOL_VERSION, *LEGACY_PROTOCOLS)
MAX_BODY_BYTES = 65_536
MAX_TOKEN_CHARS = 8_192
READ_SCOPE = "trnje:read"
WRITE_SCOPE = "trnje:write"


class MCPConfigurationError(RuntimeError):
    pass


class OAuthError(RuntimeError):
    pass


class InvalidToken(OAuthError):
    """The presented bearer token is invalid for this resource."""


class InsufficientScope(OAuthError):
    """The token is valid but lacks a required permission."""


class OAuthUnavailable(OAuthError):
    """The authorization server could not validate the token."""


@dataclass(frozen=True)
class MCPConfig:
    host: str
    port: int
    public_base_url: str
    issuer: str
    introspection_url: str
    client_id: str
    client_secret: str
    audience: str
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]

    @property
    def resource_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/mcp"

    @property
    def resource_metadata_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/.well-known/oauth-protected-resource/mcp"

    @classmethod
    def from_env(cls) -> "MCPConfig":
        host = os.environ.get("TRNJE_MCP_HOST", "127.0.0.1").strip()
        if host != "127.0.0.1":
            raise MCPConfigurationError("TRNJE_MCP_HOST must be 127.0.0.1.")
        try:
            port = int(os.environ.get("TRNJE_MCP_PORT", "8766"))
        except ValueError as exc:
            raise MCPConfigurationError("TRNJE_MCP_PORT must be an integer.") from exc
        if not 1 <= port <= 65535:
            raise MCPConfigurationError("TRNJE_MCP_PORT must be between 1 and 65535.")

        public_base = cls._https_url(
            os.environ.get("TRNJE_PUBLIC_BASE_URL", "").strip(),
            "TRNJE_PUBLIC_BASE_URL",
            allow_path=False,
        ).rstrip("/")
        issuer = cls._https_url(
            os.environ.get("TRNJE_OAUTH_ISSUER", "").strip(),
            "TRNJE_OAUTH_ISSUER",
            allow_path=True,
        ).rstrip("/")
        introspection = cls._https_url(
            os.environ.get("TRNJE_OAUTH_INTROSPECTION_URL", "").strip(),
            "TRNJE_OAUTH_INTROSPECTION_URL",
            allow_path=True,
        )
        client_id = os.environ.get("TRNJE_OAUTH_CLIENT_ID", "")
        client_secret = os.environ.get("TRNJE_OAUTH_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise MCPConfigurationError(
                "OAuth introspection client credentials are not configured."
            )
        audience = cls._https_url(
            os.environ.get("TRNJE_OAUTH_AUDIENCE", "").strip() or (public_base + "/mcp"),
            "TRNJE_OAUTH_AUDIENCE",
            allow_path=True,
        ).rstrip("/")
        if audience != public_base + "/mcp":
            raise MCPConfigurationError(
                "TRNJE_OAUTH_AUDIENCE must exactly match TRNJE_PUBLIC_BASE_URL + /mcp."
            )

        origins = tuple(
            item.strip().rstrip("/")
            for item in os.environ.get("TRNJE_MCP_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
        for origin in origins:
            parsed = urllib.parse.urlsplit(origin)
            try:
                _ = parsed.port
            except ValueError:
                raise MCPConfigurationError("Allowed browser origin contains an invalid port.") from None
            if (
                parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
            ):
                raise MCPConfigurationError(
                    "Allowed browser origins must be HTTPS origins without paths or user information."
                )

        public_host = urllib.parse.urlsplit(public_base).hostname
        hosts = tuple(
            item.strip().lower()
            for item in os.environ.get("TRNJE_MCP_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        )
        if not hosts:
            hosts = ("127.0.0.1", "localhost", public_host or "")
        host_pattern = (
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
        )
        if any(not re.fullmatch(host_pattern, item) for item in hosts):
            raise MCPConfigurationError(
                "Allowed hosts must be exact DNS names or IPv4 addresses; wildcards and ports are not allowed."
            )
        return cls(
            host=host,
            port=port,
            public_base_url=public_base,
            issuer=issuer,
            introspection_url=introspection,
            client_id=client_id,
            client_secret=client_secret,
            audience=audience,
            allowed_origins=origins,
            allowed_hosts=tuple(x for x in hosts if x),
        )

    @staticmethod
    def _https_url(value: str, name: str, *, allow_path: bool) -> str:
        parsed = urllib.parse.urlsplit(value)
        try:
            _ = parsed.port
        except ValueError:
            raise MCPConfigurationError(name + " contains an invalid port.") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (not allow_path and parsed.path not in ("", "/"))
        ):
            raise MCPConfigurationError(name + " must be a valid HTTPS URL.")
        return value


class OAuthIntrospection:
    """RFC 7662 token validator. Tokens and provider errors are never logged."""

    def __init__(self, config: MCPConfig, *, timeout_seconds: float = 5.0) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def verify(self, authorization: str | None, required_scope: str) -> dict[str, Any]:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise InvalidToken("A Bearer access token is required.")
        token = authorization[7:]
        if (
            not token
            or len(token) > MAX_TOKEN_CHARS
            or any(char.isspace() for char in token)
        ):
            raise InvalidToken("Bearer token format is invalid.")

        # RFC 6749 §2.3.1 requires form-encoding each client credential
        # before applying HTTP Basic authentication.
        credentials = (
            urllib.parse.quote_plus(self.config.client_id, safe="")
            + ":"
            + urllib.parse.quote_plus(self.config.client_secret, safe="")
        ).encode("ascii")
        body = urllib.parse.urlencode({
            "token": token,
            "token_type_hint": "access_token",
        }).encode("ascii")
        request = urllib.request.Request(
            self.config.introspection_url,
            data=body,
            headers={
                "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise OAuthUnavailable(
                        "OAuth introspection endpoint returned a non-200 response."
                    )
                raw = response.read(65_537)
                if len(raw) > 65_536:
                    raise OAuthUnavailable(
                        "OAuth introspection response exceeded its size limit."
                    )
            data = json.loads(raw.decode("utf-8"))
        except OAuthUnavailable:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            # HTTP 401/403 from the introspection endpoint is an outage/misconfiguration,
            # not evidence that the caller's access token is invalid.
            raise OAuthUnavailable("OAuth introspection service is unavailable.") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OAuthUnavailable("OAuth introspection response was invalid.") from None

        if not isinstance(data, dict) or data.get("active") is not True:
            raise InvalidToken("Access token is inactive.")
        issuer = data.get("iss")
        if issuer is not None and (
            not isinstance(issuer, str)
            or issuer.rstrip("/") != self.config.issuer.rstrip("/")
        ):
            raise InvalidToken("Access token issuer does not match the configured issuer.")
        audience = data.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if not isinstance(audiences, list) or self.config.audience not in audiences:
            raise InvalidToken("Access token audience does not match this MCP resource.")

        now = int(time.time())
        expires = data.get("exp")
        not_before = data.get("nbf")
        if expires is not None and (type(expires) is not int or expires <= now):
            raise InvalidToken("Access token has expired.")
        if not_before is not None and (type(not_before) is not int or not_before > now):
            raise InvalidToken("Access token is not yet valid.")

        scope_value = data.get("scope", data.get("scp", []))
        if isinstance(scope_value, str):
            scopes = set(scope_value.split())
        elif isinstance(scope_value, list) and all(isinstance(x, str) for x in scope_value):
            scopes = set(scope_value)
        else:
            scopes = set()
        if required_scope not in scopes:
            raise InsufficientScope("Access token lacks the required scope.")
        return {"scopes": sorted(scopes)}


def _decode_mcp_name(value: str | None) -> str | None:
    """Decode a modern Mcp-Name value, rejecting unsafe or malformed encodings."""
    if not isinstance(value, str) or not value:
        return None
    prefix, suffix = "=?base64?", "?="
    if value.startswith(prefix) and value.endswith(suffix):
        encoded = value[len(prefix):-len(suffix)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    if value != value.strip(" \\t") or any(
        (ord(char) < 0x20 and char != "\\t") or ord(char) > 0x7E
        for char in value
    ):
        return None
    return value


def _valid_mcp_method_header(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value == value.strip(" \\t")
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def _json_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "trnje_health",
            "description": "Return minimal gateway health and pending task count. Does not execute a task.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "trnje_submit_task",
            "description": (
                "Submit one allowlisted TRNJE task to the single worker queue. "
                "Returns immediately with a task ID; poll trnje_get_task for the result."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["diagnostics", "git_status", "run_tests", "read_file", "run_task"],
                    },
                    "payload": {"type": "object"},
                    "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["kind", "payload", "idempotency_key"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
        },
        {
            "name": "trnje_get_task",
            "description": "Get the current status, result, and evidence reference for one submitted task.",
            "inputSchema": {
                "type": "object",
                "properties": {"task_id": {"type": "string", "minLength": 1, "maxLength": 64}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "trnje_list_tasks",
            "description": "List recent direct MCP tasks without returning their input payloads.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
    ]


def create_server(
    queue: Any,
    validate_task: Callable[[str, dict[str, Any]], tuple[str, dict[str, Any]]],
    on_enqueue: Callable[[], None],
    *,
    config: MCPConfig | None = None,
    auth: Any | None = None,
) -> ThreadingHTTPServer:
    """Create a localhost-only MCP HTTP server; caller owns its lifecycle."""
    config = config or MCPConfig.from_env()
    auth = auth or OAuthIntrospection(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "TRNJE-MCP/1"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            # Avoid logging request headers, tokens, payloads, or file names.
            return

        def _send(
            self, status: int, body: dict[str, Any] | None = None,
            *, headers: dict[str, str] | None = None,
        ) -> None:
            raw = b"" if body is None else json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            if body is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
            else:
                self.send_header("Content-Length", "0")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            return origin in config.allowed_origins

        def _host_allowed(self) -> bool:
            host_header = self.headers.get("Host", "")
            try:
                parsed = urllib.parse.urlsplit("//" + host_header)
                # Accessing .port also rejects malformed port suffixes.
                _ = parsed.port
            except ValueError:
                return False
            return bool(
                parsed.hostname
                and parsed.hostname.lower() in config.allowed_hosts
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )

        def _security_check(self) -> bool:
            if not self._host_allowed() or not self._origin_allowed():
                self._send(403, {"error": "forbidden"})
                return False
            return True

        def _metadata(self) -> dict[str, Any]:
            return {
                "resource": config.resource_url,
                "authorization_servers": [config.issuer],
                "scopes_supported": [READ_SCOPE, WRITE_SCOPE],
                "bearer_methods_supported": ["header"],
            }

        def do_GET(self) -> None:
            if not self._security_check():
                return
            path = urllib.parse.urlsplit(self.path).path
            if path in (
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp",
            ):
                self._send(200, self._metadata())
            elif path == "/health":
                self._send(200, {"status": "ok", "service": "trnje-mcp"})
            elif path == "/mcp":
                self._send(405, {"error": "method_not_allowed"}, headers={"Allow": "POST"})
            else:
                self._send(404, {"error": "not_found"})

        def do_OPTIONS(self) -> None:
            if not self._security_check():
                return
            if urllib.parse.urlsplit(self.path).path != "/mcp":
                self._send(404, {"error": "not_found"})
                return
            origin = self.headers.get("Origin")
            headers = {
                "Allow": "POST, OPTIONS",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Authorization, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name, Idempotency-Key"
                ),
                "Vary": "Origin",
            }
            if origin:
                headers["Access-Control-Allow-Origin"] = origin
            self._send(204, headers=headers)

        def _validate_modern_request(
            self, request: dict[str, Any], request_id: Any,
            method: str, params: dict[str, Any],
        ) -> bool | None:
            meta = params.get("_meta")
            meta_version = (
                meta.get("io.modelcontextprotocol/protocolVersion")
                if isinstance(meta, dict) else None
            )
            protocol_header = self.headers.get("MCP-Protocol-Version")
            modern_hint = (
                method == "server/discover"
                or meta_version is not None
                or protocol_header == MODERN_PROTOCOL_VERSION
                or (
                    protocol_header is not None
                    and protocol_header not in LEGACY_PROTOCOLS
                )
            )
            if not modern_hint:
                if protocol_header is not None and protocol_header not in SUPPORTED_PROTOCOLS:
                    self._rpc_error(
                        request_id, -32022, "Unsupported protocol version",
                        status=400,
                        data={"supported": list(LEGACY_PROTOCOLS), "requested": protocol_header},
                    )
                    return False
                return None

            if (
                not isinstance(meta, dict)
                or not isinstance(meta_version, str)
                or protocol_header != meta_version
            ):
                self._rpc_error(
                    request_id, -32020, "Header mismatch: MCP-Protocol-Version must match request _meta.",
                    status=400,
                )
                return False

            if meta_version != MODERN_PROTOCOL_VERSION:
                self._rpc_error(
                    request_id, -32022, "Unsupported protocol version",
                    status=400,
                    data={
                        "supported": [MODERN_PROTOCOL_VERSION],
                        "requested": meta_version,
                    },
                )
                return False

            method_header = self.headers.get("Mcp-Method")
            if not _valid_mcp_method_header(method_header) or method_header != method:
                self._rpc_error(
                    request_id, -32020, "Header mismatch: Mcp-Method must match request method.",
                    status=400,
                )
                return False

            name_source = None
            if method in ("tools/call", "prompts/get"):
                name_source = params.get("name")
            elif method == "resources/read":
                name_source = params.get("uri")
            if method in ("tools/call", "resources/read", "prompts/get"):
                name_header = _decode_mcp_name(self.headers.get("Mcp-Name"))
                if (
                    not isinstance(name_source, str)
                    or name_header is None
                    or name_header != name_source
                ):
                    self._rpc_error(
                        request_id, -32020, "Header mismatch: Mcp-Name must match request params.",
                        status=400,
                    )
                    return False
            return True

        def do_POST(self) -> None:
            if not self._security_check():
                return
            if urllib.parse.urlsplit(self.path).path != "/mcp":
                self._send(404, {"error": "not_found"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._send(415, {"error": "content_type_must_be_application_json"})
                return
            accept = self.headers.get("Accept", "").lower()
            if "application/json" not in accept and "*/*" not in accept:
                self._send(406, {"error": "application_json_must_be_accepted"})
                return
            length = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,8}", length):
                self._send(411, {"error": "content_length_required"})
                return
            size = int(length)
            if size < 1 or size > MAX_BODY_BYTES:
                self._send(413, {"error": "request_body_size_invalid"})
                return
            try:
                request = json.loads(
                    self.rfile.read(size).decode("utf-8"),
                    object_pairs_hook=_json_pairs_no_duplicates,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send(400, {"error": "invalid_json"})
                return
            if not isinstance(request, dict):
                self._send(400, {"error": "one_json_rpc_message_required"})
                return
            if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                self._rpc_error(request.get("id"), -32600, "Invalid Request")
                return
            request_id = request.get("id")
            if request_id is not None and (
                isinstance(request_id, bool) or not isinstance(request_id, (str, int))
            ):
                self._rpc_error(None, -32600, "Invalid Request")
                return
            method = request["method"]
            params = request.get("params", {})
            if not isinstance(params, dict):
                self._rpc_error(request_id, -32602, "Invalid params")
                return
            if "id" not in request and method not in (
                "notifications/initialized", "notifications/cancelled"
            ):
                self._rpc_error(None, -32600, "Request ID is required")
                return

            modern = self._validate_modern_request(request, request_id, method, params)
            if modern is False:
                return

            required_scope = WRITE_SCOPE if (
                method == "tools/call"
                and isinstance(params.get("name"), str)
                and params.get("name") == "trnje_submit_task"
            ) else READ_SCOPE
            try:
                auth.verify(self.headers.get("Authorization"), required_scope)
            except InsufficientScope:
                challenge = (
                    'Bearer error="insufficient_scope", scope="' + required_scope
                    + '", resource_metadata="' + config.resource_metadata_url + '"'
                )
                self._send(
                    403,
                    {"error": "insufficient_scope"},
                    headers={"WWW-Authenticate": challenge},
                )
                return
            except OAuthUnavailable:
                self._send(
                    503,
                    {"error": "authorization_service_unavailable"},
                    headers={"Retry-After": "5"},
                )
                return
            except OAuthError:
                challenge = (
                    'Bearer error="invalid_token", resource_metadata="'
                    + config.resource_metadata_url + '", scope="' + required_scope + '"'
                )
                self._send(
                    401,
                    {"error": "invalid_token"},
                    headers={"WWW-Authenticate": challenge},
                )
                return

            if method in ("notifications/initialized", "notifications/cancelled"):
                self._send(202)
                return
            if modern is True and method == "server/discover":
                if set(params) - {"_meta"}:
                    self._rpc_error(request_id, -32602, "Invalid params")
                    return
                self._send(200, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "resultType": "complete",
                        "supportedVersions": [MODERN_PROTOCOL_VERSION],
                        "capabilities": {"tools": {}},
                        "_meta": {
                            "io.modelcontextprotocol/serverInfo": {
                                "name": "trnje", "version": "1.0.0"
                            }
                        },
                        "instructions": (
                            "TRNJE accepts bounded allowlisted tasks. Submissions are queued "
                            "and must be checked by task ID; queued does not mean completed."
                        ),
                    },
                })
                return
            if method == "initialize" and modern is not True:
                requested = params.get("protocolVersion")
                if requested not in LEGACY_PROTOCOLS:
                    self._rpc_error(
                        request_id, -32602, "Unsupported legacy protocol version",
                        data={"supported": list(LEGACY_PROTOCOLS), "requested": requested},
                    )
                    return
                self._send(200, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": requested,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "trnje", "version": "1.0.0"},
                        "instructions": (
                            "TRNJE accepts bounded allowlisted tasks. Submissions are queued "
                            "and must be checked by task ID; queued does not mean completed."
                        ),
                    },
                })
                return
            if method == "ping":
                self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": {}})
                return
            if method == "tools/list":
                self._send(200, {
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"tools": _tool_definitions()},
                })
                return
            if method == "tools/call":
                try:
                    result, is_error = self._call_tool(params)
                except (ValueError, KeyError):
                    self._rpc_error(request_id, -32602, "Invalid params")
                    return
                except Exception:
                    self._rpc_error(request_id, -32603, "Internal error")
                    return
                self._send(200, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False, sort_keys=True),
                        }],
                        "isError": is_error,
                    },
                })
                return
            if modern is True:
                self._rpc_error(request_id, -32601, "Method not found", status=404)
            else:
                self._rpc_error(request_id, -32601, "Method not found")

        def _rpc_error(
            self, request_id: Any, code: int, message: str, *,
            status: int = 200, data: dict[str, Any] | None = None,
        ) -> None:
            error: dict[str, Any] = {"code": code, "message": message}
            if data is not None:
                error["data"] = data
            self._send(status, {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": error,
            })

        def _call_tool(self, params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            name = params.get("name")
            args = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object.")
            allowed = {tool["name"] for tool in _tool_definitions()}
            if name not in allowed:
                return {"status": "BLOCKED", "detail": "Unknown tool."}, True

            if name == "trnje_health":
                if args:
                    raise ValueError("Health tool takes no arguments.")
                return {
                    "status": "PASS",
                    "service": "trnje-mcp",
                    "pending_tasks": queue.pending_count(),
                }, False

            if name == "trnje_submit_task":
                if set(args) != {"kind", "payload", "idempotency_key"}:
                    raise ValueError("Submit fields are invalid.")
                kind, payload = validate_task(args["kind"], args["payload"])
                try:
                    record, duplicate = queue.enqueue(
                        kind, payload, args["idempotency_key"]
                    )
                except IdempotencyConflict as exc:
                    return {"status": "FAIL", "detail": str(exc)}, True
                except QueueFull as exc:
                    return {"status": "BLOCKED", "detail": str(exc)}, True
                if not duplicate:
                    on_enqueue()
                return {
                    "task_id": record["task_id"],
                    "status": record["status"],
                    "created_at": record["created_at"],
                    "duplicate": duplicate,
                }, False

            if name == "trnje_get_task":
                if set(args) != {"task_id"} or not isinstance(args["task_id"], str):
                    raise ValueError("Task ID is required.")
                record = queue.get(args["task_id"])
                if record is None:
                    return {"status": "BLOCKED", "detail": "Task not found."}, True
                return record, False

            if name == "trnje_list_tasks":
                if set(args) - {"limit"}:
                    raise ValueError("Unknown list arguments.")
                limit = args.get("limit", 10)
                if type(limit) is not int or not 1 <= limit <= 20:
                    raise ValueError("List limit is invalid.")
                return {"tasks": queue.list_recent(limit)}, False

            raise ValueError("Unsupported tool.")

    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.daemon_threads = True
    return server
