"""Characterization tests for OpenAI Secure MCP Tunnel resource rewriting.

These tests do not call OpenAI or an OAuth provider. They model the documented
contract: Tunnel rewrites the connector-facing PRMD resource to an
OpenAI-hosted /v1/mcp/{tunnel_id} URL, then forwards Authorization to the
upstream MCP server. They capture the current TRNJE configuration limitation.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

from trnje_mcp_http import (
    MCPConfig,
    MCPConfigurationError,
    InvalidToken,
    OAuthIntrospection,
    READ_SCOPE,
)


UPSTREAM_RESOURCE = "https://trnje.example.test/mcp"
# Synthetic host; the path mirrors the documented OpenAI tunnel endpoint.
TUNNEL_RESOURCE = (
    "https://tunnel.example.test/v1/mcp/"
    "0123456789abcdef0123456789abcdef"
)
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


def test_local_resource_token_is_accepted(monkeypatch):
    result = verify_with_audience(
        monkeypatch, UPSTREAM_RESOURCE, UPSTREAM_RESOURCE
    )
    assert result == {"scopes": [READ_SCOPE]}


def test_tunnel_rewritten_resource_token_is_rejected_with_current_default_audience(
    monkeypatch,
):
    # OpenAI Tunnel rewrites the discovered resource to its hosted endpoint.
    # A token bound to that URL must not match TRNJE's current upstream audience.
    with pytest.raises(InvalidToken, match="audience"):
        verify_with_audience(
            monkeypatch, UPSTREAM_RESOURCE, TUNNEL_RESOURCE
        )


def test_validator_accepts_tunnel_token_only_when_exact_tunnel_audience_is_configured(
    monkeypatch,
):
    # The validator itself already supports an exact external audience. The
    # current environment parser is the restriction exercised by the next test.
    result = verify_with_audience(
        monkeypatch, TUNNEL_RESOURCE, TUNNEL_RESOURCE
    )
    assert result == {"scopes": [READ_SCOPE]}


def test_environment_parser_rejects_tunnel_rewritten_resource_audience(monkeypatch):
    monkeypatch.setenv("TRNJE_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("TRNJE_MCP_PORT", "8766")
    monkeypatch.setenv("TRNJE_PUBLIC_BASE_URL", "https://trnje.example.test")
    monkeypatch.setenv("TRNJE_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv(
        "TRNJE_OAUTH_INTROSPECTION_URL", ISSUER + "/oauth/introspect"
    )
    monkeypatch.setenv("TRNJE_OAUTH_CLIENT_ID", "trnje-resource")
    monkeypatch.setenv("TRNJE_OAUTH_CLIENT_SECRET", "test-only-secret")
    monkeypatch.setenv("TRNJE_OAUTH_AUDIENCE", TUNNEL_RESOURCE)

    with pytest.raises(
        MCPConfigurationError,
        match="must exactly match TRNJE_PUBLIC_BASE_URL",
    ):
        MCPConfig.from_env()
