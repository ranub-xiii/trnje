import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from trnje_api_queue import IdempotencyConflict, TaskQueue
from trnje_mcp_http import (
    MCPConfig,
    MCPConfigurationError,
    MODERN_PROTOCOL_VERSION,
    InsufficientScope,
    InvalidToken,
    OAuthError,
    OAuthIntrospection,
    OAuthUnavailable,
    READ_SCOPE,
    WRITE_SCOPE,
    create_server,
)


class FakeAuth:
    def __init__(self, scopes=None):
        self.scopes = set(scopes or {READ_SCOPE, WRITE_SCOPE})

    def verify(self, authorization, required_scope):
        if authorization != "Bearer test-access-token":
            raise InvalidToken("invalid token")
        if required_scope not in self.scopes:
            raise InsufficientScope("insufficient scope")
        return {"scopes": sorted(self.scopes)}


class RunningServer:
    def __init__(self, tmp_path, *, auth=None, allowed_origins=()):
        self.queue = TaskQueue(tmp_path / "queue.sqlite3")
        self.wake_count = 0
        config = MCPConfig(
            host="127.0.0.1",
            port=0,
            public_base_url="https://trnje.example.test",
            issuer="https://auth.example.test",
            introspection_url="https://auth.example.test/oauth/introspect",
            client_id="test-client",
            client_secret="test-secret",
            audience="https://trnje.example.test/mcp",
            allowed_origins=tuple(allowed_origins),
            allowed_hosts=("127.0.0.1", "localhost", "trnje.example.test"),
        )

        def validate(kind, payload):
            allowed = {"diagnostics", "git_status", "run_tests", "read_file", "run_task"}
            if kind not in allowed or not isinstance(payload, dict):
                raise ValueError("not allowlisted")
            if kind in {"diagnostics", "git_status", "run_tests"} and payload:
                raise ValueError("unexpected payload")
            return kind, payload

        def wake():
            self.wake_count += 1

        self.server = create_server(
            self.queue, validate, wake, config=config, auth=auth or FakeAuth()
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:{}/mcp".format(self.server.server_address[1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def running_server(tmp_path):
    server = RunningServer(tmp_path)
    try:
        yield server
    finally:
        server.close()


def request(
    server, message, *, token="test-access-token", origin=None,
    mcp_headers=True, header_overrides=None,
):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if origin is not None:
        headers["Origin"] = origin

    params = message.get("params", {})
    method = message.get("method")
    meta = params.get("_meta", {}) if isinstance(params, dict) else {}
    version = meta.get("io.modelcontextprotocol/protocolVersion")
    if mcp_headers and isinstance(version, str):
        headers["MCP-Protocol-Version"] = version
        headers["Mcp-Method"] = method
        if method in ("tools/call", "prompts/get"):
            name = params.get("name")
            if isinstance(name, str):
                headers["Mcp-Name"] = name
        elif method == "resources/read":
            uri = params.get("uri")
            if isinstance(uri, str):
                headers["Mcp-Name"] = uri

    for key, value in (header_overrides or {}).items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value

    req = urllib.request.Request(
        server.url,
        data=json.dumps(message).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def rpc(method, params=None, request_id=1, *, modern=True, protocol_version=MODERN_PROTOCOL_VERSION):
    params = dict(params or {})
    if modern:
        meta = dict(params.get("_meta", {}))
        meta["io.modelcontextprotocol/protocolVersion"] = protocol_version
        meta.setdefault("io.modelcontextprotocol/clientInfo", {
            "name": "trnje-tests", "version": "1.0"
        })
        meta.setdefault("io.modelcontextprotocol/clientCapabilities", {})
        params["_meta"] = meta
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }


def tool_call(name, arguments, request_id=1):
    return rpc("tools/call", {"name": name, "arguments": arguments}, request_id)


def decode_tool(response_body):
    message = json.loads(response_body)
    return json.loads(message["result"]["content"][0]["text"])


def test_protected_resource_metadata_is_available(running_server):
    url = running_server.url.replace("/mcp", "/.well-known/oauth-protected-resource")
    with urllib.request.urlopen(url, timeout=3) as response:
        metadata = json.loads(response.read())
    assert metadata["resource"] == "https://trnje.example.test/mcp"
    assert metadata["authorization_servers"] == ["https://auth.example.test"]
    assert set(metadata["scopes_supported"]) == {READ_SCOPE, WRITE_SCOPE}


def test_server_discover_reports_modern_protocol(running_server):
    status, _, body = request(running_server, rpc("server/discover"))
    result = json.loads(body)["result"]
    assert status == 200
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == [MODERN_PROTOCOL_VERSION]
    assert result["capabilities"] == {"tools": {}}
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "trnje"


def test_modern_request_requires_protocol_header(running_server):
    status, _, body = request(
        running_server, rpc("tools/list"), mcp_headers=False
    )
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32020


def test_modern_request_requires_matching_method_header(running_server):
    status, _, body = request(
        running_server, rpc("tools/list"),
        header_overrides={"Mcp-Method": "tools/call"},
    )
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32020


def test_modern_tool_call_requires_matching_name_header(running_server):
    status, _, body = request(
        running_server, tool_call("trnje_health", {}),
        header_overrides={"Mcp-Name": "trnje_submit_task"},
    )
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32020
    assert running_server.wake_count == 0


def test_modern_protocol_header_must_match_request_metadata(running_server):
    status, _, body = request(
        running_server, rpc("tools/list"),
        header_overrides={"MCP-Protocol-Version": "2025-11-25"},
    )
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32020


def test_unknown_modern_protocol_returns_supported_versions(running_server):
    status, _, body = request(
        running_server,
        rpc("tools/list", protocol_version="2099-01-01"),
    )
    message = json.loads(body)
    assert status == 400
    assert message["error"]["code"] == -32022
    assert message["error"]["data"] == {
        "supported": [MODERN_PROTOCOL_VERSION],
        "requested": "2099-01-01",
    }


def test_initialize_and_tools_list_follow_json_rpc(running_server):
    status, _, body = request(
        running_server,
        rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, modern=False),
    )
    assert status == 200
    initialized = json.loads(body)
    assert initialized["result"]["protocolVersion"] == "2025-11-25"
    assert initialized["result"]["serverInfo"]["name"] == "trnje"

    status, _, body = request(running_server, rpc("tools/list"))
    tools = json.loads(body)["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert status == 200
    assert names == {
        "trnje_health", "trnje_submit_task", "trnje_get_task", "trnje_list_tasks"
    }


def test_modern_tool_call_requires_name_header(running_server):
    status, _, body = request(
        running_server, tool_call("trnje_health", {}),
        header_overrides={"Mcp-Name": None},
    )
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32020


def test_legacy_tools_list_remains_available(running_server):
    legacy = rpc("tools/list", modern=False)
    status, _, body = request(
        running_server, legacy,
        header_overrides={"MCP-Protocol-Version": "2025-11-25"},
    )
    assert status == 200
    assert json.loads(body)["result"]["tools"]


def test_submit_is_durable_and_returns_without_running(running_server):
    status, _, body = request(
        running_server,
        tool_call(
            "trnje_submit_task",
            {
                "kind": "diagnostics",
                "payload": {},
                "idempotency_key": "mcp-req-1",
            },
        ),
    )
    result = decode_tool(body)
    assert status == 200
    assert result["status"] == "QUEUED"
    assert result["task_id"]
    assert running_server.queue.get(result["task_id"])["status"] == "QUEUED"
    assert running_server.wake_count == 1


def test_idempotent_replay_returns_same_task_and_does_not_wake_again(running_server):
    payload = {
        "kind": "diagnostics",
        "payload": {},
        "idempotency_key": "same-request",
    }
    first = decode_tool(request(
        running_server, tool_call("trnje_submit_task", payload)
    )[2])
    second = decode_tool(request(
        running_server, tool_call("trnje_submit_task", payload, request_id=2)
    )[2])
    assert first["task_id"] == second["task_id"]
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert running_server.wake_count == 1


def test_reused_idempotency_key_with_different_payload_is_not_accepted(running_server):
    request(
        running_server,
        tool_call("trnje_submit_task", {
            "kind": "diagnostics", "payload": {}, "idempotency_key": "conflict"
        }),
    )
    status, _, body = request(
        running_server,
        tool_call("trnje_submit_task", {
            "kind": "git_status", "payload": {}, "idempotency_key": "conflict"
        }),
    )
    message = json.loads(body)
    assert status == 200
    assert message["result"]["isError"] is True
    assert "already used" in message["result"]["content"][0]["text"]


def test_queue_capacity_returns_blocked_without_creating_task(running_server):
    running_server.queue.MAX_PENDING_TASKS = 0
    status, _, body = request(
        running_server,
        tool_call("trnje_submit_task", {
            "kind": "diagnostics", "payload": {}, "idempotency_key": "queue-full"
        }),
    )
    result = decode_tool(body)
    assert status == 200
    assert result["status"] == "BLOCKED"
    assert running_server.queue.pending_count() == 0
    assert running_server.wake_count == 0


def test_read_tool_returns_result_and_evidence_reference(running_server):
    record, _ = running_server.queue.enqueue("diagnostics", {}, "get-me")
    claimed = running_server.queue.claim_next()
    running_server.queue.complete(
        claimed["task_id"],
        {"status": "PASS", "detail": "verified", "exit": 0},
        evidence_path="api-task.md",
        evidence_sha256="a" * 64,
    )
    status, _, body = request(
        running_server,
        tool_call("trnje_get_task", {"task_id": record["task_id"]}),
    )
    result = decode_tool(body)
    assert status == 200
    assert result["status"] == "PASS"
    assert result["result"]["detail"] == "verified"
    assert result["evidence_sha256"] == "a" * 64


def test_write_tool_requires_write_scope(tmp_path):
    server = RunningServer(tmp_path, auth=FakeAuth({READ_SCOPE}))
    try:
        status, headers, body = request(
            server,
            tool_call("trnje_submit_task", {
                "kind": "diagnostics", "payload": {}, "idempotency_key": "no-write"
            }),
        )
        assert status == 403
        assert 'error="insufficient_scope"' in headers["WWW-Authenticate"]
        assert 'scope="trnje:write"' in headers["WWW-Authenticate"]
        assert "oauth-protected-resource/mcp" in headers["WWW-Authenticate"]
        assert server.queue.pending_count() == 0
    finally:
        server.close()


def test_introspection_outage_returns_503(tmp_path):
    class UnavailableAuth:
        def verify(self, authorization, required_scope):
            raise OAuthUnavailable("authorization server offline")

    server = RunningServer(tmp_path, auth=UnavailableAuth())
    try:
        status, headers, body = request(server, rpc("tools/list"))
        assert status == 503
        assert headers["Retry-After"] == "5"
        assert json.loads(body)["error"] == "authorization_service_unavailable"
    finally:
        server.close()


def test_missing_token_is_rejected(running_server):
    status, headers, _ = request(
        running_server, rpc("tools/list"), token=None
    )
    assert status == 401
    assert "Bearer" in headers["WWW-Authenticate"]


def test_invalid_origin_is_rejected(running_server):
    status, _, body = request(
        running_server, rpc("tools/list"), origin="https://evil.example"
    )
    assert status == 403
    assert json.loads(body)["error"] == "forbidden"


def test_explicitly_allowed_origin_is_accepted(tmp_path):
    server = RunningServer(
        tmp_path, allowed_origins=("https://allowed.example",)
    )
    try:
        status, _, _ = request(
            server, rpc("tools/list"), origin="https://allowed.example"
        )
        assert status == 200
    finally:
        server.close()


def test_health_and_unsupported_get_are_bounded(running_server):
    health_url = running_server.url.replace("/mcp", "/health")
    with urllib.request.urlopen(health_url, timeout=3) as response:
        assert json.loads(response.read()) == {"status": "ok", "service": "trnje-mcp"}
    try:
        urllib.request.urlopen(running_server.url, timeout=3)
    except urllib.error.HTTPError as exc:
        assert exc.code == 405
        assert exc.headers["Allow"] == "POST"
    else:
        raise AssertionError("GET /mcp unexpectedly succeeded")


def test_notifications_return_202_without_body(running_server):
    message = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    status, _, body = request(running_server, message)
    assert status == 202
    assert body == b""


def test_unknown_json_rpc_method_returns_method_not_found(running_server):
    status, _, body = request(running_server, rpc("no/such-method"))
    assert status == 404
    assert json.loads(body)["error"]["code"] == -32601


def test_oauth_config_requires_https_and_real_issuer(tmp_path, monkeypatch):
    values = {
        "TRNJE_PUBLIC_BASE_URL": "http://unsafe.example",
        "TRNJE_OAUTH_ISSUER": "https://auth.example",
        "TRNJE_OAUTH_INTROSPECTION_URL": "https://auth.example/introspect",
        "TRNJE_OAUTH_CLIENT_ID": "client",
        "TRNJE_OAUTH_CLIENT_SECRET": "secret",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(MCPConfigurationError):
        MCPConfig.from_env()


def test_oauth_allowed_hosts_reject_wildcards(monkeypatch):
    values = {
        "TRNJE_PUBLIC_BASE_URL": "https://trnje.example",
        "TRNJE_OAUTH_ISSUER": "https://auth.example",
        "TRNJE_OAUTH_INTROSPECTION_URL": "https://auth.example/introspect",
        "TRNJE_OAUTH_CLIENT_ID": "client",
        "TRNJE_OAUTH_CLIENT_SECRET": "secret",
        "TRNJE_MCP_ALLOWED_HOSTS": "trnje.example,*",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(MCPConfigurationError):
        MCPConfig.from_env()


def test_oauth_audience_must_match_the_advertised_mcp_resource(monkeypatch):
    values = {
        "TRNJE_PUBLIC_BASE_URL": "https://trnje.example",
        "TRNJE_OAUTH_ISSUER": "https://auth.example",
        "TRNJE_OAUTH_INTROSPECTION_URL": "https://auth.example/introspect",
        "TRNJE_OAUTH_CLIENT_ID": "client",
        "TRNJE_OAUTH_CLIENT_SECRET": "secret",
        "TRNJE_OAUTH_AUDIENCE": "https://other.example/mcp",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(MCPConfigurationError):
        MCPConfig.from_env()


class FakeResponse:
    status = 200

    def __init__(self, value):
        self.value = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.value[:size]


def introspection_config():
    return MCPConfig(
        host="127.0.0.1",
        port=0,
        public_base_url="https://trnje.example.test",
        issuer="https://auth.example.test",
        introspection_url="https://auth.example.test/introspect",
        client_id="client",
        client_secret="secret",
        audience="https://trnje.example.test/mcp",
        allowed_origins=(),
        allowed_hosts=("127.0.0.1",),
    )


def test_oauth_introspection_checks_active_audience_issuer_and_scope():
    auth = OAuthIntrospection(introspection_config())
    payload = {
        "active": True,
        "iss": "https://auth.example.test",
        "aud": ["https://trnje.example.test/mcp"],
        "scope": "trnje:read trnje:write",
    }
    with patch("trnje_mcp_http.urllib.request.urlopen", return_value=FakeResponse(payload)) as call:
        verified = auth.verify("Bearer opaque-token", READ_SCOPE)
    assert verified["scopes"] == ["trnje:read", "trnje:write"]
    request_object = call.call_args.args[0]
    assert request_object.get_method() == "POST"
    assert request_object.headers["Authorization"] == "Basic Y2xpZW50OnNlY3JldA=="
    assert "token=opaque-token" in request_object.data.decode("ascii")


@pytest.mark.parametrize("payload", [
    {"active": False, "aud": "https://trnje.example.test/mcp", "scope": "trnje:read"},
    {"active": True, "aud": "https://other.example/mcp", "scope": "trnje:read"},
    {"active": True, "aud": "https://trnje.example.test/mcp", "scope": "trnje:write"},
])
def test_oauth_introspection_rejects_inactive_wrong_audience_or_missing_scope(payload):
    auth = OAuthIntrospection(introspection_config())
    with patch("trnje_mcp_http.urllib.request.urlopen", return_value=FakeResponse(payload)):
        with pytest.raises(OAuthError):
            auth.verify("Bearer opaque-token", READ_SCOPE)
