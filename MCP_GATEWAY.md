# TRNJE Direct MCP Gateway

**Status: draft implementation.** The gateway is optional and disabled by default. It is not yet deployed to the Termux device. It does not replace the GitHub or optional Dropbox mailbox; it adds a direct MCP input to the same existing TRNJE worker process.

## Runtime design

- The existing `scripts/trnje_termux_worker.py` remains the only task executor and retains its existing single-instance `worker.lock`.
- MCP HTTP requests are handled on a server thread. A write request is validated with the same `parse()` allowlist as GitHub and Dropbox, then persisted to a separate SQLite inbox.
- The worker loop wakes immediately when the inbox receives a new task, claims one task, and calls the existing `execute()` function. The HTTP request never launches a task subprocess.
- SQLite is stored at `~/.local/state/trnje-worker/mcp-tasks.sqlite3`; the legacy `state.json` and Dropbox cursor fields are not repurposed or rewritten by MCP.
- A restart changes tasks left in `RUNNING` to `UNKNOWN`. They are never automatically replayed.
- The queue retains task evidence and does not silently purge it. It accepts at most 100 queued/running tasks and at most 1,000 total task records. Reaching either limit returns `BLOCKED`; archive/reconciliation is required before further submissions.
- Each completed task creates a redacted, mode-0600 evidence log under `~/.local/state/trnje-worker/logs/`, with a SHA-256 digest recorded in SQLite.

## MCP tools

| Tool | Scope | Behavior |
|---|---|---|
| `trnje_health` | `trnje:read` | Minimal gateway health and queue count |
| `trnje_submit_task` | `trnje:write` | Enqueue one allowlisted task; returns a task ID, never a false completion |
| `trnje_get_task` | `trnje:read` | Read status, result, output/evidence reference and digest |
| `trnje_list_tasks` | `trnje:read` | List recent task statuses without returning request payloads |

Allowed task kinds and their payloads are the same as the existing worker:

- `diagnostics`: `{}`
- `git_status`: `{}`
- `run_tests`: `{}`
- `read_file`: `{"path":"relative/path","offset":0,"limit":12000}` (offset/limit optional)
- `run_task`: `{"file":"relative/path","task":"bounded instruction","expect":"exact acceptance text"}`

Arbitrary shell, arbitrary commands, arbitrary output paths, and unknown payload fields are rejected. `run_task` continues to use the existing one-file executor, workspace boundary, lock, fixed checks, and guarded rollback behavior.

Every submit requires an `idempotency_key` containing 1–128 ASCII letters, digits, `.`, `_`, `:`, or `-`. Repeating the same key and request returns the original task. Reusing a key for a different request is rejected.

## OAuth 2.1 deployment requirement

This code is an OAuth **protected resource**, not an authorization server. It does not implement login, consent, the authorization-code flow, PKCE, client registration, or token issuance. Do not expose it publicly until it is connected to a real OAuth 2.1 Authorization Server.

The selected Authorization Server must:

1. Support Authorization Code with PKCE using `S256` for the MCP client.
2. Publish authorization-server metadata (RFC 8414 or OpenID Connect discovery).
3. Support the MCP client's registration/discovery requirements (Client ID Metadata Documents or a compatible Dynamic Client Registration flow).
4. Issue access tokens with the exact resource audience configured in `TRNJE_OAUTH_AUDIENCE`. In direct mode this defaults to `https://<public-host>/mcp`. If a trusted tunnel or proxy rewrites the connector-facing resource URL, configure the exact rewritten resource URL as the audience; do not accept multiple audience aliases.
5. Expose an RFC 7662 token-introspection endpoint that accepts the configured confidential resource-server client credentials and returns `active`, `aud`, and `scope` (plus `iss`, `exp`, and `nbf` when available).
6. Grant only `trnje:read` and `trnje:write` to the intended user/client.

The TRNJE resource server calls introspection for each protected MCP request. It rejects inactive tokens, an incorrect audience, an issuer mismatch when `iss` is returned, and expired/not-yet-valid tokens. An invalid token returns `401`; a valid token missing the operation's required scope returns `403` with an `insufficient_scope` challenge; introspection outage or malformed provider response returns `503` with a short `Retry-After`. A provider that only supplies self-contained JWT verification and has no compatible introspection endpoint is not directly compatible with this build. Do not replace this check with a static bearer token.

The required configuration values are supplied through the worker's protected environment, not committed to the repository:

| Variable | Example / meaning |
|---|---|
| `TRNJE_MCP_ENABLED` | `1` to enable; unset/`0` keeps the gateway off |
| `TRNJE_MCP_HOST` | Must remain `127.0.0.1` |
| `TRNJE_MCP_PORT` | `8766` by default |
| `TRNJE_PUBLIC_BASE_URL` | `https://trnje.example.com` |
| `TRNJE_OAUTH_ISSUER` | Exact issuer URL from the Authorization Server |
| `TRNJE_OAUTH_INTROSPECTION_URL` | HTTPS RFC 7662 endpoint |
| `TRNJE_OAUTH_CLIENT_ID` | Confidential resource-server client ID |
| `TRNJE_OAUTH_CLIENT_SECRET` | Confidential resource-server secret; never commit it |
| `TRNJE_OAUTH_AUDIENCE` | Exact token `aud`; defaults to `https://trnje.example.com/mcp` in direct mode. For a resource-rewriting tunnel, set the exact tunnel resource URL, including its tunnel ID. |
| `TRNJE_MCP_ALLOWED_HOSTS` | Comma-separated exact hosts, e.g. `trnje.example.com,127.0.0.1,localhost` |
| `TRNJE_MCP_ALLOWED_ORIGINS` | Exact trusted HTTPS browser origins; leave empty for native clients without an Origin header |

The public host is the upstream MCP origin used to build Protected Resource Metadata. The token audience is configured independently: it must exactly match the resource URL used by the client and returned in introspection. Keep one exact expected audience; do not add wildcard or fallback audience aliases. For a tunnel that rewrites the resource URL, use that tunnel URL (including its tunnel ID) for `TRNJE_OAUTH_AUDIENCE` while keeping `TRNJE_PUBLIC_BASE_URL` set to the upstream MCP origin. Do not add wildcard hosts or wildcard browser origins. The listener refuses any bind address other than `127.0.0.1`.

## Protected Resource Metadata and endpoints

The server exposes unauthenticated discovery at:

- `GET /.well-known/oauth-protected-resource/mcp` (the path-aware resource metadata URL used in authorization challenges)
- `GET /.well-known/oauth-protected-resource` (root compatibility alias)

The metadata identifies `/mcp` as the resource, advertises the configured issuer as the authorization server, and lists the supported scopes.

The MCP endpoint is `POST /mcp`. `GET /mcp` is rejected. `/health` returns only a minimal service status; it is not a task interface.

### Protocol compatibility

The server is dual-era:

- Modern clients use protocol `2026-07-28`. Every POST must include `MCP-Protocol-Version` matching `params._meta["io.modelcontextprotocol/protocolVersion"]`, and `Mcp-Method` matching the JSON-RPC method. Calls to `tools/call`, `resources/read`, and `prompts/get` also require a matching `Mcp-Name` header. The server validates these values before processing the request.
- Modern clients can call `server/discover` to retrieve the supported modern version, server identity and capabilities. The modern path does not use an `initialize` handshake or protocol-level sessions.
- Legacy clients using `2025-11-25`, `2025-06-18`, or `2025-03-26` retain the initialization-based flow. A legacy `initialize` request must select one of those legacy versions.
- CORS preflight allows the standard modern MCP request headers. Browser Origins remain opt-in and must be exact HTTPS origins.

## Cloudflare Tunnel boundary

Only after OAuth and local tests pass, configure the named Cloudflare Tunnel to forward the public hostname to:

`http://127.0.0.1:8766`

Do not forward ports `8765` (Pocket GPT), `11434` (Ollama), or `18182` (llama-server). Cloudflare Tunnel provides transport, not OAuth authorization. Any Cloudflare Access policy is an additional perimeter control and must not be mistaken for the Authorization Server or MCP token validation.

## Activation and verification boundary

The installer keeps its safe default `TRNJE_WORKER_REF=main`. During this unmerged draft, an intentional test deployment would need to select `feat/direct-mcp-gateway-20261114` explicitly. Do not install that branch until the external Authorization Server is configured and the deployment is deliberately approved.


- `TRNJE_MCP_ENABLED` defaults off; `--once` does not start the server.
- The installer places the worker and both imported modules together. It verifies SHA-256 pins and Python compilation before replacing files. Modules are installed before the worker; the worker is replaced last.
- `--update` makes timestamped backups of existing files. It does not restart an already running worker.
- No package installation is required: the gateway, queue, and HTTP transport use Python's standard library.
- Enabling the variable and restarting the existing worker is a deployment action, not part of preparing this draft. Validate the Authorization Server, OAuth metadata, tunnel, and real ChatGPT/client interoperability before exposing the hostname.
- Passing unit/CI tests proves code paths in the test environment only; it does not prove Termux service startup, OAuth provider compatibility, tunnel availability, or ChatGPT plan entitlement.
