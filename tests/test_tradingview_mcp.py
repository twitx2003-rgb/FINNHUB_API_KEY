"""TradingView MCP client, exercised end to end against a local OAuth-protected server.

TradingView's real server is unreachable from CI and needs a paid account, so
this builds the closest faithful stand-in: a real streamable-HTTP MCP server
behind a minimal OAuth 2.1 authorization server (protected-resource metadata,
AS metadata, dynamic client registration, authorize redirect, token endpoint).
Everything on the client side is the production code path.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from pipeline.errors import ProviderError
from pipeline.providers.tradingview_mcp import (
    AuthorizationRequired,
    FileTokenStorage,
    LocalCallback,
    TradingViewMCP,
    describe_result,
    describe_tools,
    parse_tool_args,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_tradingview_mcp():
    from mcp.server import MCPServer

    srv = MCPServer("fake-tradingview")

    @srv.tool()
    def get_quote(symbol: str) -> dict:
        """Latest quote for an EXCHANGE:TICKER symbol."""
        return {"symbol": symbol, "close": 101.25, "market_cap": 2.5e12}

    return srv


# ------------------------------------------------------------ local OAuth server
class FakeAuthServer:
    """Serves MCP at /mcp only to callers holding the token it issued."""

    TOKEN = "access-token-1"

    def __init__(self, dcr: bool = True, waf: bool = False):
        self.dcr = dcr                       # False: no registration endpoint at all
        self.waf = waf                       # True: 403 for requests without a known User-Agent
        self.user_agents: list[str] = []
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.registrations: list[dict] = []
        self.token_requests: list[dict] = []
        self._mcp_app = _fake_tradingview_mcp().streamable_http_app()
        self._server = None

    async def app(self, scope, receive, send):
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response

        if scope["type"] != "http":
            return await self._mcp_app(scope, receive, send)   # lifespan etc.

        path, base = scope["path"], self.base
        request = Request(scope, receive)
        agent = request.headers.get("user-agent", "")
        self.user_agents.append(agent)
        if self.waf and not agent.startswith("market-research-pipeline"):
            # like TradingView's bot protection: the SDK's bare requests get 403
            return await Response(status_code=403)(scope, receive, send)

        if path.startswith("/.well-known/oauth-protected-resource"):
            resp = JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})
        elif path.startswith("/.well-known/oauth-authorization-server"):
            meta = {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
            if self.dcr:
                meta["registration_endpoint"] = f"{base}/register"
            resp = JSONResponse(meta)
        elif path == "/register" and self.dcr:
            body = await request.json()
            self.registrations.append(body)
            resp = JSONResponse({**body, "client_id": "client-123"}, status_code=201)
        elif path == "/token":
            form = dict(await request.form())
            self.token_requests.append(form)
            resp = JSONResponse({"access_token": self.TOKEN, "token_type": "Bearer",
                                 "expires_in": 3600, "refresh_token": "refresh-1"})
        elif path.startswith("/mcp"):
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if auth != f"Bearer {self.TOKEN}":
                resp = Response(status_code=401, headers={
                    "WWW-Authenticate":
                        f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'})
            else:
                return await self._mcp_app(scope, receive, send)
        else:
            resp = Response(status_code=404)
        await resp(scope, receive, send)

    def start(self):
        import uvicorn

        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                                     log_level="warning", lifespan="on",
                                                     interface="asgi3"))
        threading.Thread(target=self._server.run, daemon=True).start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("fake server did not start")
            time.sleep(0.05)

    def stop(self):
        self._server.should_exit = True


@pytest.fixture
def auth_server():
    server = FakeAuthServer()
    server.start()
    yield server
    server.stop()


def _browser_that_approves(auth_url: str) -> None:
    """Plays the user: follows the authorize URL and approves straight away."""
    query = parse_qs(urlparse(auth_url).query)
    redirect, state = query["redirect_uri"][0], query["state"][0]

    def hit():
        time.sleep(0.2)
        urllib.request.urlopen(f"{redirect}?{urlencode({'code': 'auth-code-xyz', 'state': state})}")

    threading.Thread(target=hit, daemon=True).start()


def _client(server, tmp_path, *, interactive, browser=_browser_that_approves, port=None):
    return TradingViewMCP(url=f"{server.base}/mcp", token_path=tmp_path / "tv_tokens.json",
                          callback_port=port or _free_port(), interactive=interactive,
                          open_browser=browser)


# ------------------------------------------------------------------ OAuth flow
def test_headless_run_without_tokens_asks_for_sign_in_instead_of_hanging(auth_server, tmp_path):
    started = time.time()
    with pytest.raises(AuthorizationRequired, match="--auth-tradingview"):
        _client(auth_server, tmp_path, interactive=False).list_tools()
    assert time.time() - started < 15


def test_sign_in_stores_tokens_and_headless_runs_reuse_them(auth_server, tmp_path):
    tools = _client(auth_server, tmp_path, interactive=True).list_tools()
    assert [t.name for t in tools] == ["get_quote"]

    # dynamic registration used our loopback redirect and asked for refresh tokens
    reg = auth_server.registrations[0]
    assert reg["redirect_uris"][0].startswith("http://127.0.0.1:")
    assert "refresh_token" in reg["grant_types"]
    # the code was exchanged with PKCE
    assert auth_server.token_requests[0]["code"] == "auth-code-xyz"
    assert auth_server.token_requests[0]["code_verifier"]

    stored = json.loads((tmp_path / "tv_tokens.json").read_text())
    assert stored["tokens"]["access_token"] == FakeAuthServer.TOKEN
    assert stored["client_info"]["client_id"] == "client-123"

    def no_browser(url):
        raise AssertionError("a headless run must not open a browser")

    headless = _client(auth_server, tmp_path, interactive=False, browser=no_browser)
    result = headless.call_tool("get_quote", {"symbol": "NASDAQ:NVDA"})
    assert not result.is_error
    assert json.loads(result.content[0].text)["close"] == 101.25
    assert len(auth_server.token_requests) == 1          # no second sign-in


def test_unreachable_server_is_a_provider_error_not_a_crash(tmp_path):
    client = TradingViewMCP(url=f"http://127.0.0.1:{_free_port()}/mcp",
                            token_path=tmp_path / "t.json", interactive=False)
    with pytest.raises(ProviderError) as info:
        client.list_tools()
    # a construction bug (bad kwargs) would also end up here — make sure it is not one
    assert "TypeError" not in str(info.value) and "ValidationError" not in str(info.value)


# ------------------------------------------------------------------ components
def test_token_storage_round_trip(tmp_path):
    from mcp.shared.auth import OAuthToken

    storage = FileTokenStorage(tmp_path / "nested" / "tokens.json")
    assert asyncio.run(storage.get_tokens()) is None
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", refresh_token="r", expires_in=60)))
    again = asyncio.run(FileTokenStorage(tmp_path / "nested" / "tokens.json").get_tokens())
    assert again.access_token == "a" and again.refresh_token == "r"


def test_callback_reports_a_denied_sign_in(tmp_path):
    cb = LocalCallback("127.0.0.1", _free_port())
    cb.start()
    try:
        threading.Thread(target=lambda: _get_ignoring_errors(
            f"{cb.redirect_uri}?error=access_denied"), daemon=True).start()
        with pytest.raises(AuthorizationRequired, match="access_denied"):
            asyncio.run(cb.wait(timeout=5))
    finally:
        cb.stop()


def _get_ignoring_errors(url):
    time.sleep(0.1)
    try:
        urllib.request.urlopen(url)
    except Exception:  # noqa: BLE001 — 400 response is expected
        pass


def test_in_process_server_listing_and_call_are_readable():
    client = TradingViewMCP(server=_fake_tradingview_mcp())
    listing = describe_tools(client.list_tools())
    assert "get_quote" in listing and "symbol: string (required)" in listing
    text = describe_result(client.call_tool("get_quote", {"symbol": "NASDAQ:NVDA"}))
    assert "is_error: False" in text and "101.25" in text


def test_parse_tool_args_handles_json_and_strings():
    assert parse_tool_args(["symbol=NASDAQ:NVDA", "limit=5", "adjusted=true"]) == {
        "symbol": "NASDAQ:NVDA", "limit": 5, "adjusted": True}
    with pytest.raises(ValueError):
        parse_tool_args(["oops"])


# ----------------------------------------------------------------- diagnostics
def test_diagnose_reports_the_sign_in_routes(auth_server):
    from pipeline.providers.tradingview_mcp import diagnose

    text = "\n".join(diagnose(f"{auth_server.base}/mcp"))
    assert "authorization_servers" in text
    assert f"dynamic client registration : yes -> {auth_server.base}/register" in text
    assert "client ID metadata document : NO" in text


def test_server_without_registration_gets_an_actionable_error(tmp_path):
    """What the first live sign-in hit: TradingView answered /register with 404."""
    from pipeline.providers.tradingview_mcp import diagnose

    server = FakeAuthServer(dcr=False)
    server.start()
    try:
        assert "dynamic client registration : NO" in "\n".join(diagnose(f"{server.base}/mcp"))
        with pytest.raises(ProviderError, match="--tradingview-diagnose"):
            _client(server, tmp_path, interactive=True).list_tools()
    finally:
        server.stop()


# ------------------------------------------------------------- bot protection
def test_sdk_bare_request_is_blocked_but_ours_passes():
    """Reproduces the live finding: 403 for the SDK's discovery request, 200 for ours."""
    from pipeline.providers.tradingview_mcp import _compare_headers

    server = FakeAuthServer(waf=True)
    server.start()
    try:
        lines = _compare_headers(f"{server.base}/.well-known/oauth-authorization-server")
        assert "403" in lines[0] and "as the SDK sends it" in lines[0]
        assert "200" in lines[1] and "with our headers" in lines[1]
    finally:
        server.stop()


def test_sign_in_succeeds_behind_bot_protection(tmp_path):
    server = FakeAuthServer(waf=True)
    server.start()
    try:
        tools = _client(server, tmp_path, interactive=True).list_tools()
        assert [t.name for t in tools] == ["get_quote"]
        assert server.registrations, "registration must reach the real endpoint"
        assert all(a.startswith("market-research-pipeline") for a in server.user_agents)
    finally:
        server.stop()
