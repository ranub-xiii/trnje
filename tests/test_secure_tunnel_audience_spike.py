"""Characterization and regression tests for Secure MCP Tunnel audience binding.

These tests use a synthetic tunnel host with the documented endpoint shape:
<OPENAI_MCP_TUNNEL_BASE_URL>/v1/mcp/<tunnel_id>.
They do not call OpenAI, ChatGPT, or a real OAuth provider.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

from trnje_mcp_http import (
    MCPConfig,
    InvalidToken,
    OAuthIntrospection,
    READ_SCOPE,
)


UPSTREAM_RESOURCE = "https://trnje.example.test/mcp"
TUNNEL_BASE = "https://tunnel.example.test/v1/mcp/"
TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdef"
OTHER_TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdee"
TUNNEL_RESOURCE = TUNNEL_BASE + TUNNEL_ID
OTHER_TUNNEL_RESOURCE = TUNNEL_BASE + OTHER_TUNNEL_ID
OTHER_RESOURCE = "https://other-tunnel.example.test/v1/mcp/" + TUNNEL_ID
ISSUER = "https://auth.example.test"


class JsonResponse:
    def __init__(self, payload):
        self.status = 200
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]


def make_config(audience):
    return MCPConfig(
        host="127.0.0.1",
        port=8766,
        public_base_url="https://trnje.example.test",
        issuer=ISSUER,
        introspection_url=ISSUER + "/oauth/introspect",
        client_id="trnje-resource",
        client_secret="test-only-secret",
        audience=audience,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1", "localhost", "trnje.example.test"),
    )


def verify_with_audience(monkeypatch, configured_audience, token_audience):
    payload = {
        "active": True,
        "iss": ISSUER,
        "aud": [token_audience],
        "scope": READ_SCOPE,
    }
    monkeypatch.setattr(
        "trnje_mcp_http.urllib.request.urlopen",
        lambda request, timeout: JsonResponse(payload),
    )
    return OAuthIntrospection(make_config(configured_audience)).verify(
        "Bearer opaque-test-token", READ_SCOPE
    )


def set_required_environment(monkeypatch, audience=None):
    values = {
        "TRNJE_MCP_HOST": "127.0.0.1",
        "TRNJE_MCP_PORT": "8766",
        "TRNJE_PUBLIC_BASE_URL": "https://trnje.example.test",
        "TRNJE_OAUTH_ISSUER": ISSUER,
        "TRNJE_OAUTH_INTROSPECTION_URL": ISSUER + "/oauth/introspect",
        "TRNJE_OAUTH_CLIENT_ID": "trnje-resource",
        "TRNJE_OAUTH_CLIENT_SECRET": "test-only-secret",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    if audience is None:
        monkeypatch.delenv("TRNJE_OAUTH_AUDIENCE", raising=False)
    else:
        monkeypatch.setenv("TRNJE_OAUTH_AUDIENCE", audience)


def test_local_resource_token_is_accepted(monkeypatch):
    result = verify_with_audience(
        monkeypatch, UPSTREAM_RESOURCE, UPSTREAM_RESOURCE
    )
    assert result == {"scopes": [READ_SCOPE]}


def test_tunnel_resource_is_rejected_when_upstream_audience_is_expected(monkeypatch):
    with pytest.raises(InvalidToken, match="audience"):
        verify_with_audience(
            monkeypatch, UPSTREAM_RESOURCE, TUNNEL_RESOURCE
        )


def test_exact_configured_tunnel_resource_is_accepted(monkeypatch):
    result = verify_with_audience(
        monkeypatch, TUNNEL_RESOURCE, TUNNEL_RESOURCE
    )
    assert result == {"scopes": [READ_SCOPE]}


@pytest.mark.parametrize(
    "unexpected_audience",
    [OTHER_TUNNEL_RESOURCE, OTHER_RESOURCE],
    ids=["different-tunnel-id", "different-resource-host"],
)
def test_tunnel_token_rejects_wrong_tunnel_id_or_resource(
    monkeypatch, unexpected_audience
):
    with pytest.raises(InvalidToken, match="audience"):
        verify_with_audience(
            monkeypatch, TUNNEL_RESOURCE, unexpected_audience
        )


def test_environment_parser_accepts_separate_tunnel_audience(monkeypatch):
    set_required_environment(monkeypatch, TUNNEL_RESOURCE)

    config = MCPConfig.from_env()

    # Public resource metadata continues to describe the upstream MCP server.
    assert config.resource_url == UPSTREAM_RESOURCE
    # Authorization is bound to the exact connector-facing tunnel resource.
    assert config.audience == TUNNEL_RESOURCE


def test_environment_parser_keeps_direct_resource_as_default(monkeypatch):
    set_required_environment(monkeypatch)

    config = MCPConfig.from_env()

    assert config.resource_url == UPSTREAM_RESOURCE
    assert config.audience == UPSTREAM_RESOURCE
