"""TradingView's official MCP server — the phase 2 second source.

Remote MCP over streamable HTTP with OAuth 2.1 (no API key). Verified against
mcp 2.2.0 by introspecting the installed package:

    Client(transport)                        high-level client
    streamable_http_client(url, http_client=...)   transport
    create_mcp_http_client(auth=...)         httpx2.AsyncClient with MCP timeouts
    OAuthClientProvider(server_url, client_metadata, storage,
                        redirect_handler, callback_handler)
    TokenStorage: async get_tokens / set_tokens / get_client_info / set_client_info

Two modes:

- **interactive** (`run.py --auth-tradingview`): opens the browser, catches the
  redirect on a one-shot localhost server, and stores tokens on disk;
- **headless** (every pipeline run): uses and refreshes the stored tokens. If the
  server wants a fresh sign-in, it raises AuthorizationRequired instead of
  waiting forever for a browser nobody is watching.

The server is in public beta and its tools answer in model-facing text/JSON,
not a typed API. Tool names and response shapes are therefore discovered from
the live server (`run.py --tradingview-tools`) rather than assumed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import webbrowser
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import parse_qs, urlparse

from ..errors import PipelineError, ProviderError

log = logging.getLogger(__name__)

DEFAULT_URL = "https://mcp.tradingview.com/mcp"

# TradingView's sign-in host (www.tradingview.com) sits behind bot protection. The
# mcp SDK builds its OAuth discovery/registration requests as bare Requests with
# no User-Agent and no Accept header, and the first live sign-in got 403 on
# discovery — after which the SDK guessed a /register URL and failed with 404.
# The same metadata URL returned 200 to a request carrying these headers, so
# every request this client sends gets them.
USER_AGENT = "market-research-pipeline/0.2 (personal research)"
_CALLBACK_PATH = "/callback"
_SIGN_IN_TIMEOUT_S = 300


class AuthorizationRequired(PipelineError):
    """Stored TradingView tokens are missing or no longer accepted."""


# --------------------------------------------------------------------------- tokens
class FileTokenStorage:
    """Persists OAuth tokens and the registered client between runs.

    Implements mcp.client.auth.TokenStorage. The file holds a refresh token, so
    it lives outside the project (never committed) and is written atomically.
    """

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("token file %s is unreadable; a new sign-in will be needed", self.path)
            return {}

    def _write(self, key: str, value: dict) -> None:
        data = self._read()
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)          # best effort; Windows ignores most modes
        except OSError:
            pass
        os.replace(tmp, self.path)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        self._write("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._write("client_info", client_info.model_dump(mode="json", exclude_none=True))


# ------------------------------------------------------------------ OAuth callback
class LocalCallback:
    """One-shot HTTP server on the loopback interface that catches the OAuth redirect."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self._event = threading.Event()
        self._params: dict[str, str] = {}
        self._server: HTTPServer | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}{_CALLBACK_PATH}"

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — stdlib naming
                parsed = urlparse(self.path)
                if parsed.path != _CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                outer._params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                ok = "code" in outer._params
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                message = ("Signed in to TradingView. You can close this tab."
                           if ok else f"Sign-in failed: {outer._params.get('error', 'no code')}")
                self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode())
                outer._event.set()

            def log_message(self, *args):  # keep the console clean
                pass

        self._server = HTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    async def wait(self, timeout: float = _SIGN_IN_TIMEOUT_S):
        from mcp.shared.auth import AuthorizationCodeResult

        got = await asyncio.to_thread(self._event.wait, timeout)
        if not got:
            raise AuthorizationRequired(f"no sign-in completed within {int(timeout)} seconds")
        if "code" not in self._params:
            raise AuthorizationRequired(
                f"TradingView returned no authorization code: {self._params.get('error_description') or self._params.get('error') or self._params}"
            )
        return AuthorizationCodeResult(code=self._params["code"], state=self._params.get("state"),
                                       iss=self._params.get("iss"))

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# -------------------------------------------------------------------------- client
class TradingViewMCP:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        token_path: str | Path = "~/.mrp/tv_tokens.json",
        callback_host: str = "127.0.0.1",
        callback_port: int = 8765,
        *,
        interactive: bool = False,
        server: Any = None,
        open_browser: Callable[[str], Any] = webbrowser.open,
    ):
        """`server` replaces the network connection with an in-process MCP server (tests)."""
        self.url = url
        self.storage = FileTokenStorage(Path(token_path))
        self.interactive = interactive
        self._server = server
        self._open_browser = open_browser
        self._callback = LocalCallback(callback_host, callback_port)

    async def _on_redirect(self, auth_url: str) -> None:
        if not self.interactive:
            raise AuthorizationRequired(
                "TradingView needs you to sign in. Run once: python run.py --auth-tradingview"
            )
        print("\nOpening TradingView sign-in in your browser. If it does not open, visit:\n"
              f"  {auth_url}\n")
        self._open_browser(auth_url)

    async def _on_callback(self):
        return await self._callback.wait()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        from mcp import Client

        if self._server is not None:
            async with Client(self._server, cache=None) as client:
                yield client
            return

        from mcp.client.auth import OAuthClientProvider
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata

        auth = OAuthClientProvider(
            server_url=self.url,
            client_metadata=OAuthClientMetadata(
                client_name="market-research-pipeline (personal use)",
                redirect_uris=[self._callback.redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                application_type="native",
            ),
            storage=self.storage,
            redirect_handler=self._on_redirect,
            callback_handler=self._on_callback,
        )
        if self.interactive:
            self._callback.start()
        try:
            http = create_mcp_http_client(auth=auth)
            # Request hooks run on every send, including the OAuth flow's own
            # requests, which is the only way to reach those headers.
            http.event_hooks["request"].append(_identify_request)
            async with http:
                async with Client(streamable_http_client(self.url, http_client=http),
                                  cache=None) as client:
                    yield client
        finally:
            self._callback.stop()

    # ---------------------------------------------------------------- async API
    async def list_tools_async(self) -> list[Any]:
        async with self._client() as client:
            tools, cursor = [], None
            while True:
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = getattr(page, "next_cursor", None)
                if not cursor:
                    return tools

    async def call_tool_async(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        async with self._client() as client:
            return await client.call_tool(name, arguments or {})

    # ----------------------------------------------------------------- sync API
    def list_tools(self) -> list[Any]:
        return _run(self.list_tools_async())

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return _run(self.call_tool_async(name, arguments))


async def _identify_request(request) -> None:
    request.headers["User-Agent"] = USER_AGENT
    if "accept" not in request.headers:
        request.headers["Accept"] = "application/json"


def _run(coro):
    try:
        return asyncio.run(coro)
    except AuthorizationRequired:
        raise
    except BaseExceptionGroup as group:  # anyio task groups wrap the real error
        leaves = _flatten(group)
        auth = [e for e in leaves if isinstance(e, AuthorizationRequired)]
        if auth:
            raise auth[0] from None
        raise _explain(leaves[0] if leaves else group) from group
    except Exception as exc:  # noqa: BLE001 — surface transport/auth failures uniformly
        raise _explain(exc) from exc


def _explain(exc: BaseException) -> ProviderError:
    from mcp.client.auth import OAuthRegistrationError

    if isinstance(exc, OAuthRegistrationError):
        return ProviderError(
            "TradingView refused to register this program as an OAuth client "
            f"({exc}). Its server may not allow dynamic registration. Run "
            "`python run.py --tradingview-diagnose` to see which sign-in routes it offers."
        )
    return ProviderError(f"TradingView MCP: {type(exc).__name__}: {exc}")


def _flatten(group: BaseException) -> list[BaseException]:
    if isinstance(group, BaseExceptionGroup):
        out: list[BaseException] = []
        for inner in group.exceptions:
            out.extend(_flatten(inner))
        return out
    return [group]


def _first_message(group: BaseException) -> str:
    leaves = _flatten(group)
    first = leaves[0] if leaves else group
    return f"{type(first).__name__}: {first}"


# ------------------------------------------------------------------ diagnostics
def _fetch_json(url: str, timeout: float = 20) -> tuple[int | None, Any]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _metadata_urls(issuer: str) -> list[str]:
    """RFC 8414 / OIDC discovery locations, in the order the SDK tries them."""
    parsed = urlparse(issuer)
    root = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    if path:
        return [f"{root}/.well-known/oauth-authorization-server{path}",
                f"{root}/.well-known/openid-configuration{path}",
                f"{root}{path}/.well-known/openid-configuration"]
    return [f"{root}/.well-known/oauth-authorization-server",
            f"{root}/.well-known/openid-configuration"]


def _compare_headers(url: str) -> list[str]:
    """Send one metadata request exactly as the mcp SDK builds it, and once with
    this client's headers, and report both statuses."""
    import httpx2
    from mcp.client.auth.utils import create_oauth_metadata_request

    async def probe() -> list[str]:
        lines = []
        async with httpx2.AsyncClient(timeout=20) as client:
            bare = create_oauth_metadata_request(url)       # the SDK's own builder
            ours = create_oauth_metadata_request(url)
            await _identify_request(ours)
            for label, request in (("as the SDK sends it  ", bare), ("with our headers      ", ours)):
                try:
                    response = await client.send(request)
                    lines.append(f"{label}-> {response.status_code}")
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"{label}-> {type(exc).__name__}: {exc}")
        return lines

    try:
        return asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        return [f"header check failed: {type(exc).__name__}: {exc}"]


def diagnose(url: str = DEFAULT_URL) -> list[str]:
    """What the server advertises about sign-in, and which client-identification
    route it allows: dynamic registration, a client ID metadata document, or neither."""
    out: list[str] = []
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    candidates = [f"{root}/.well-known/oauth-protected-resource{parsed.path.rstrip('/')}",
                  f"{root}/.well-known/oauth-protected-resource"]

    resource = None
    for candidate in candidates:
        status, body = _fetch_json(candidate)
        out.append(f"GET {candidate} -> {status}")
        if isinstance(body, dict):
            resource = body
            out.append(json.dumps(body, indent=2))
            break
    if resource is None:
        out.append("No protected-resource metadata found.")
        return out

    for issuer in resource.get("authorization_servers") or []:
        meta = None
        for candidate in _metadata_urls(issuer):
            status, body = _fetch_json(candidate)
            out.append(f"GET {candidate} -> {status}")
            if isinstance(body, dict):
                meta = body
                out.append(json.dumps(body, indent=2))
                break
        if meta is None:
            out.append(f"No authorization-server metadata found for {issuer}.")
            continue
        out.append("")
        out.append(f"header check on {candidate}:")
        out.extend(f"  {line}" for line in _compare_headers(candidate))
        out.append("")
        out.append(f"summary for {issuer}:")
        out.append(f"  dynamic client registration : "
                   f"{'yes -> ' + meta['registration_endpoint'] if meta.get('registration_endpoint') else 'NO'}")
        out.append(f"  client ID metadata document : "
                   f"{'yes' if meta.get('client_id_metadata_document_supported') else 'NO'}")
        out.append(f"  PKCE methods                : {meta.get('code_challenge_methods_supported')}")
        out.append(f"  scopes                      : {meta.get('scopes_supported')}")
    return out


# ---------------------------------------------------------------- presentation
def describe_tools(tools: list[Any]) -> str:
    """Compact listing: name, first line of the description, and parameters."""
    lines = []
    for tool in sorted(tools, key=lambda t: t.name):
        summary = (tool.description or tool.title or "").strip().splitlines()
        lines.append(f"{tool.name}")
        if summary:
            lines.append(f"    {summary[0][:110]}")
        props = (tool.input_schema or {}).get("properties", {}) or {}
        required = set((tool.input_schema or {}).get("required", []) or [])
        for pname, spec in props.items():
            kind = spec.get("type") or spec.get("anyOf") and "anyOf" or "?"
            flag = "required" if pname in required else "optional"
            hint = (spec.get("description") or "").strip().splitlines()
            hint_text = f" — {hint[0][:70]}" if hint else ""
            lines.append(f"      {pname}: {kind} ({flag}){hint_text}")
    return "\n".join(lines)


def describe_result(result: Any, limit: int = 4000) -> str:
    """A call result as text: error flag, structured content, then text blocks."""
    parts = [f"is_error: {result.is_error}"]
    if result.structured_content is not None:
        parts.append("structured_content:\n" + json.dumps(result.structured_content, indent=2,
                                                          default=str)[:limit])
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(f"[{type(block).__name__}]\n" + (text[:limit] if text else "(no text)"))
    return "\n".join(parts)


def parse_tool_args(pairs: list[str]) -> dict[str, Any]:
    """key=value pairs from the command line. Values are JSON when they parse as
    JSON (numbers, booleans, lists), otherwise plain strings. Avoids passing raw
    JSON on the command line, which Windows PowerShell 5.1 mangles."""
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"expected key=value, got '{pair}'")
        key, value = pair.split("=", 1)
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out
