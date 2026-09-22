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
from datetime import datetime, timezone
import json
import logging
import os
import re
import socket
import threading
import time
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


class _HideExpectedSignInError(logging.Filter):
    """The SDK logs every aborted OAuth flow as an ERROR with a traceback. A
    headless run stopping to ask for a sign-in is expected, and is reported once,
    in plain words, by the caller."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, AuthorizationRequired)


logging.getLogger("mcp.client.auth.oauth2").addFilter(_HideExpectedSignInError())


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

    def _write(self, key: str, value: Any) -> None:
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

    def clear(self) -> None:
        """Forget the registered client and its tokens (forces a fresh registration)."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        self._write("tokens", tokens.model_dump(mode="json", exclude_none=True))
        self._write("tokens_saved_at", time.time())

    def expires_at(self) -> float | None:
        """When the stored access token expires (unix time), or None if unknown.

        The SDK forgets this between runs: it loads tokens without their expiry,
        treats an expired token as valid, gets a 401 and asks for a full browser
        sign-in instead of using the refresh token. `_StoredExpiryAuth` feeds this
        value back in so the refresh happens.
        """
        data = self._read()
        expires_in = (data.get("tokens") or {}).get("expires_in")
        if expires_in is None:
            return None
        saved_at = data.get("tokens_saved_at")
        if saved_at is None:           # files written before this was recorded
            try:
                saved_at = self.path.stat().st_mtime
            except OSError:
                return None
        return float(saved_at) + float(expires_in)

    def status(self) -> dict:
        """What is stored, without any secret: for messages and diagnostics."""
        tokens = self._read().get("tokens") or {}
        expires = self.expires_at()
        return {
            "file": str(self.path),
            "access_token": bool(tokens.get("access_token")),
            "refresh_token": bool(tokens.get("refresh_token")),
            "scope": tokens.get("scope"),
            "expires_in": tokens.get("expires_in"),
            "expires_at": (datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds")
                           if expires is not None else None),
            "expired": None if expires is None else time.time() > expires,
        }

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._write("client_info", client_info.model_dump(mode="json", exclude_none=True))


# ------------------------------------------------------------------ OAuth callback
class _IPv6Server(HTTPServer):
    address_family = socket.AF_INET6


class LocalCallback:
    """One-shot HTTP server on the loopback interface that catches the OAuth redirect.

    The redirect URI says `localhost`, not `127.0.0.1`: TradingView's CDN answered
    the authorize request with a CloudFront 403 when the redirect carried the IP
    literal, and `localhost` is what known-good MCP clients register. Browsers may
    resolve `localhost` to IPv6 first on Windows, so both loopback addresses listen.
    """

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self._event = threading.Event()
        self._params: dict[str, str] = {}
        self._servers: list[HTTPServer] = []

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

        if self.host == "localhost":
            binds = [(HTTPServer, "127.0.0.1"), (_IPv6Server, "::1")]
        else:
            binds = [(_IPv6Server if ":" in self.host else HTTPServer, self.host)]

        for server_cls, address in binds:
            try:
                server = server_cls((address, self.port), Handler)
            except OSError as exc:
                if address == "::1":            # no IPv6 loopback on this machine — fine
                    log.debug("IPv6 loopback unavailable for the sign-in callback: %s", exc)
                    continue
                raise
            self._servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()

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
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers = []


# -------------------------------------------------------------------------- client
class TradingViewMCP:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        token_path: str | Path = "~/.mrp/tv_tokens.json",
        callback_host: str = "localhost",
        callback_port: int = 8765,
        *,
        interactive: bool = False,
        sign_in_timeout: float = _SIGN_IN_TIMEOUT_S,
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
        self._sign_in_timeout = sign_in_timeout

    async def _on_redirect(self, auth_url: str) -> None:
        if not self.interactive:
            raise AuthorizationRequired(
                f"TradingView needs you to sign in ({_why_sign_in(self.storage.status())}). "
                "Sign in on tradingview.com in your browser, then run once: "
                "python run.py --auth-tradingview"
            )
        # TradingView's CDN blocks /accounts/signin/ when it arrives as a redirect from
        # this authorize URL, so the sign-in must already exist in the browser.
        print("\nSign in at https://www.tradingview.com in your default browser FIRST.\n"
              "If the page shows 'ERROR: The request could not be satisfied', you were not\n"
              "signed in: sign in on the main site, then run this command again.\n\n"
              "Opening TradingView approval in your browser. If it does not open, visit:\n"
              f"  {auth_url}\n")
        self._open_browser(auth_url)

    async def _on_callback(self):
        return await self._callback.wait(self._sign_in_timeout)

    async def _forget_client_if_redirect_changed(self) -> None:
        """A client registered with another redirect URI cannot complete a sign-in
        here (the server checks the redirect against the registration), so drop it
        and let the SDK register afresh."""
        info = await self.storage.get_client_info()
        if info is None:
            return
        registered = [str(u) for u in (info.redirect_uris or [])]
        if self._callback.redirect_uri not in registered:
            log.info("stored TradingView client uses %s; re-registering with %s",
                     registered, self._callback.redirect_uri)
            self.storage.clear()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        from mcp import Client

        if self._server is not None:
            async with Client(self._server, cache=None) as client:
                yield client
            return

        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        from mcp.shared.auth import OAuthClientMetadata

        auth = _stored_expiry_auth()(
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
            await self._forget_client_if_redirect_changed()
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
        if not is_read_only(name):
            raise ProviderError(
                f"refusing to call TradingView tool '{name}': the pipeline only reads. "
                "The same account can create/delete alerts and edit watchlists, so any tool "
                "that is not a get/list/search/screener call is blocked here."
            )
        async with self._client() as client:
            return await client.call_tool(name, arguments or {})

    # ----------------------------------------------------------------- sync API
    def list_tools(self) -> list[Any]:
        return _run(self.list_tools_async())

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return _run(self.call_tool_async(name, arguments))


def _why_sign_in(status: dict) -> str:
    if not status["access_token"]:
        return "no stored sign-in"
    if not status["refresh_token"]:
        return "the stored token has no refresh token, so it cannot be renewed"
    if status["expired"]:
        return f"the access token expired at {status['expires_at']} and refreshing it failed"
    return "TradingView rejected the stored token"


def _stored_expiry_auth():
    """OAuthClientProvider that restores the stored token's expiry on load.

    Without this, a run after the access token expired sends the stale token,
    gets 401, and the SDK goes straight to a browser sign-in — the refresh token
    is never tried. With the expiry known, the SDK refreshes first.
    """
    from mcp.client.auth import OAuthClientProvider

    class _StoredExpiryAuth(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            expires_at = getattr(self.context.storage, "expires_at", None)
            if self.context.current_tokens is not None and expires_at is not None:
                when = expires_at()
                if when is not None:
                    self.context.token_expiry_time = when

    return _StoredExpiryAuth


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

    if isinstance(exc, PipelineError):   # already explained, e.g. a refused write tool
        return exc if isinstance(exc, ProviderError) else ProviderError(str(exc))
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


def _cdn_verdict(status: int, headers: Any, body: bytes) -> tuple[bool, str]:
    """Did CloudFront itself answer (its error page), and what does the page say?

    Every response through CloudFront carries x-amz-cf-id, including the origin's
    own errors, so that header proves nothing. CloudFront's generated error pages
    say "Generated by cloudfront" and come with `X-Cache: Error from cloudfront`;
    those are the signals used here, for any status code.
    """
    cache = (headers.get("X-Cache") or headers.get("x-cache") or "").lower()
    blocked = b"generated by cloudfront" in body.lower() or cache.startswith("error from cloudfront")
    text = " ".join(body.decode("utf-8", "replace").split())
    low = text.lower()
    if "<title>" in low and "</title>" in low:
        snippet = text[low.find("<title>") + 7:low.find("</title>")]
    else:
        snippet = text
    return blocked, snippet[:120]


def _probe_authorize(authorization_endpoint: str, resource: str, port: int = 8765) -> list[str]:
    """Send the authorize request with each redirect host and report whether the CDN
    blocks it. The client_id is a placeholder: past the CDN the server should answer
    with its own error or a redirect, which is exactly the 'not blocked' signal."""
    import urllib.error
    import urllib.request
    from urllib.parse import urlencode

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    lines = []
    for host in ("127.0.0.1", "localhost"):
        query = urlencode({
            "response_type": "code", "client_id": "diagnostic-probe",
            "redirect_uri": f"http://{host}:{port}{_CALLBACK_PATH}",
            "state": "diagnostic", "code_challenge": "A" * 43, "code_challenge_method": "S256",
            "resource": resource, "scope": "mcp:read mcp:tools",
        })
        req = urllib.request.Request(f"{authorization_endpoint}?{query}",
                                     headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(req, timeout=20) as resp:
                status, headers, body = resp.status, resp.headers, b""
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            body = exc.read() or b""
        except OSError as exc:
            lines.append(f"redirect host {host:<10} -> {type(exc).__name__}: {exc}")
            continue
        blocked, snippet = _cdn_verdict(status, headers, body)
        verdict = "BLOCKED by CDN" if blocked else "reached the sign-in server"
        lines.append(f"redirect host {host:<10} -> {status}  {verdict}")
        if snippet:
            lines.append(f"{'':<25}page: {snippet}")
    return lines


def probe_url(url: str, max_hops: int = 8) -> list[str]:
    """Follow a sign-in URL hop by hop, as this program (not a browser) would, and
    report each hop's status, whether the CDN blocked it, and where it redirects.

    The browser got a CloudFront block on the authorize page while a scripted
    request with a placeholder client passed; following the real URL shows
    whether the block sits on the authorize request itself or a later hop.
    Cookies are not sent and nothing sensitive is printed.
    """
    import urllib.error
    import urllib.request
    from urllib.parse import urljoin

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    lines = []
    for hop in range(max_hops):
        shown = urlparse(url)
        label = f"{shown.scheme}://{shown.netloc}{shown.path}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with opener.open(req, timeout=20) as resp:
                status, headers, body = resp.status, resp.headers, resp.read(4000)
        except urllib.error.HTTPError as exc:
            status, headers, body = exc.code, exc.headers, (exc.read() or b"")[:4000]
        except OSError as exc:
            lines.append(f"hop {hop}: {label} -> {type(exc).__name__}: {exc}")
            break
        blocked, snippet = _cdn_verdict(status, headers, body)
        location = headers.get("Location")
        note = "BLOCKED by CDN" if blocked else ""
        lines.append(f"hop {hop}: {label} -> {status} {note}".rstrip())
        if location and 300 <= status < 400:
            nxt = urlparse(urljoin(url, location))
            if nxt.hostname in ("localhost", "127.0.0.1", "::1"):
                lines.append(f"        redirects to the local callback ({nxt.scheme}://{nxt.netloc}{nxt.path})")
                break
            url = urljoin(url, location)
            continue
        lines.append(f"        page: {snippet}")
        break
    return lines


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
        if meta.get("authorization_endpoint"):
            out.append("")
            out.append("authorize request, by redirect host (a CDN block means the browser "
                       "sign-in page will fail the same way):")
            out.extend(f"  {line}" for line in _probe_authorize(
                meta["authorization_endpoint"], resource.get("resource", url)))
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


_READ_PREFIXES = ("get-", "get_", "list-", "list_", "search-", "search_")
_READ_NAMES = ("run-screener", "run_screener")


def is_read_only(name: str) -> bool:
    """True for tools that only read. TradingView's server mixes these with tools that
    change the account (alerts, watchlists); names are `mcp-tv-<verb>-...` or
    `mcp-watchlist-<verb>-...`, so the verb decides."""
    base = re.sub(r"^mcp-(tv|watchlist)-", "", name)
    return base.startswith(_READ_PREFIXES) or base in _READ_NAMES


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
            if value.startswith("[") and value.endswith("]"):
                # PowerShell strips the inner quotes of ["A","B"], leaving [A,B]
                out[key] = [item.strip() for item in value[1:-1].split(",") if item.strip()]
            else:
                out[key] = value
    return out
