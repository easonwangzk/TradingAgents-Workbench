"""Email-authenticated HTTP boundary for the hosted research workbench."""

import asyncio
import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import NotModifiedResponse, StaticFiles

from tradingagents.workbench.public_accounts import AccountError
from tradingagents.workbench.public_admin import PublicAdmin
from tradingagents.workbench.public_assistant import AssistantError, ResearchAssistant
from tradingagents.workbench.public_auth import public_origin
from tradingagents.workbench.public_budget import BudgetError
from tradingagents.workbench.public_gateway import GatewayError
from tradingagents.workbench.public_hosted_runs import HostedRunsError
from tradingagents.workbench.public_market import MarketData, MarketError
from tradingagents.workbench.public_registration import EmailRegistration
from tradingagents.workbench.public_server import (
    ASSETS,
    BODY_LIMIT,
    CSP,
    STATIC,
    BodyTooLargeError,
    _delete_cookie,
    _set_cookie,
    append_security_headers,
    read_bounded_body,
)
from tradingagents.workbench.public_settings import SettingsError
from tradingagents.workbench.server import _json_bytes, _markdown

SESSION_COOKIE = "__Host-tawb-email-session"
EMAIL_ASSETS = frozenset(ASSETS) | {"market-data.mjs", "market-chart.mjs", "admin-workspace.mjs"}
PUBLIC_PATHS = frozenset({"/auth/login", "/auth/register", "/auth/register/code", "/auth/redeem", "/auth/reset", "/signed-out", "/healthz"})
BROWSER_AUTH_WRITE_PATHS = frozenset({"/auth/login", "/auth/register", "/auth/register/code", "/auth/redeem", "/auth/reset"})
CAPABILITY_PATHS = frozenset({"/api/internal/worker/provider-dispatch", "/api/internal/worker/events"})
WORKER_BODY_LIMIT = 2 * 1024 * 1024
MAX_JSON_DEPTH = 64
MISSING_PEER_SOURCE = "browser-login:missing-peer"
_STREAM_POLL_SECONDS = 1.0
_STREAM_HEARTBEAT_SECONDS = 15.0
_STREAM_LIFETIME_SECONDS = 300.0


def _body_limit_for_path(path: str) -> int:
    return WORKER_BODY_LIMIT if path in CAPABILITY_PATHS else BODY_LIMIT


def _theme_bootstrap(nonce: str) -> str:
    return (
        f'<script nonce="{nonce}">'
        '(() => {'
        'const param = new URLSearchParams(window.location.search).get("scoutTheme");'
        'const theme = param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");'
        'document.documentElement.setAttribute("data-theme", theme);'
        'const root = document.documentElement;'
        'let language = "auto";'
        'try { language = localStorage.getItem("tradingagents.studio.language") || "auto"; } catch {}'
        'const choice = ["auto","zh","en"].includes(language) ? language : "auto";'
        'root.lang = (choice === "auto" ? /^zh(?:-|$)/i.test(navigator.language || "") : choice === "zh") ? "zh-CN" : "en";'
        'root.dataset.palette = "mono";'
        'try {'
        'const palette = localStorage.getItem("tradingagents.studio.palette");'
        'if (["mono", "ice", "pine", "coral", "amber"].includes(palette)) root.dataset.palette = palette;'
        '} catch {}'
        'try {'
        'const saved = JSON.parse(sessionStorage.getItem("ta-studio-appearance") || "{}");'
        'for (const [key, choices] of Object.entries({theme:["light","dark"],motion:["full","reduced"],density:["comfortable","compact"],grid:["visible","hidden"]})) {'
        'if (choices.includes(saved[key]) && !(key === "theme" && new URLSearchParams(location.search).has("scoutTheme"))) root.dataset[key] = saved[key];'
        '}'
        '} catch {}'
        '})();'
        '</script>'
    )


def _page_policy(nonce: str) -> str:
    return CSP.replace("script-src 'self'", f"script-src 'self' 'nonce-{nonce}'")


def _auth_head(title: str, nonce: str, *, route: str = "login") -> str:
    layout = "register" if route == "register" else "login"
    return (
        f"<!doctype html><html lang=\"zh-CN\" data-screen=\"login\" data-auth=\"{layout}\" data-auth-route=\"{route}\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title>{_theme_bootstrap(nonce)}"
        '<link rel="modulepreload" href="/static/vendor.js">'
        "<link rel=\"stylesheet\" href=\"/static/styles.css\">"
        "<link rel=\"stylesheet\" href=\"/static/auth-studio.css\">"
        "<script type=\"module\" src=\"/static/account-ui.js\"></script>"
        "<script type=\"module\" src=\"/static/auth-locale.mjs\"></script>"
        f"<script type=\"module\" nonce=\"{nonce}\" src=\"/static/auth-studio.js\"></script></head>"
    )


def _auth_frame(content: str, *, title: str, nonce: str, route: str = "login") -> str:
    return (
        f"{_auth_head(title, nonce, route=route)}<body class=\"studio-auth auth-shell\">"
        "<div class=\"auth-backdrop\" aria-hidden=\"true\"><span id=\"login-grid-hover\" class=\"login-grid-hover\" hidden></span></div>"
        "<div class=\"auth-layout\">"
        "<header class=\"auth-header\"><a class=\"auth-wordmark\" href=\"/auth/login\" aria-label=\"TradingAgents 登录\">"
        "<i data-lucide=\"activity\"></i><span>Research Studio</span></a><div class=\"auth-tools\" data-studio-tools></div></header>"
        f"<main class=\"auth-main\">{content}</main>"
        "<footer class=\"auth-footer\"><span>TradingAgents / Research Studio</span><span>EMAIL ACCESS</span></footer>"
        "<aside class=\"auth-art\" aria-label=\"行情研究插画\">"
        "<div class=\"art-index\"><span>TRADINGAGENTS</span><span>FIG. 01 / RESEARCH</span></div>"
        "<div id=\"auth-scene\" class=\"motion-scene auth-scene\" data-scene=\"magnifier\" aria-hidden=\"true\">"
        "<div class=\"scene-fallback lens-fallback\"><i></i><i></i><i></i><i></i><b></b></div></div>"
        "<div class=\"art-caption\"><span>See the signal.<br>Study the whole picture.</span><i data-lucide=\"arrow-up-right\"></i></div>"
        "</aside></div></body></html>"
    )


def _login_page(nonce: str) -> str:
    content = (
        "<div class=\"auth-identity\"><span class=\"auth-mark\"><i data-lucide=\"activity\"></i></span>"
        "<h1>TradingAgents</h1><p>RESEARCH STUDIO / 研究工作台</p></div>"
        "<section class=\"auth-card\" aria-labelledby=\"login-title\"><h2 id=\"login-title\">欢迎回来</h2>"
        "<p class=\"auth-copy auth-restriction\"><i data-lucide=\"lock-keyhole\"></i>使用邮箱和密码登录研究工作台</p>"
        "<form id=\"email-login-form\" class=\"auth-form\" method=\"post\" action=\"/auth/login\" novalidate>"
        "<p id=\"auth-message\" class=\"error-message auth-error\" role=\"alert\" hidden></p>"
        "<div class=\"auth-field\"><label for=\"email\">邮箱地址</label><div class=\"auth-input\"><i data-lucide=\"mail\"></i>"
        "<input id=\"email\" name=\"email\" type=\"email\" autocomplete=\"username email\" inputmode=\"email\" autocapitalize=\"none\" spellcheck=\"false\" required></div></div>"
        "<div class=\"auth-field\"><label for=\"password\">密码</label><div class=\"auth-input\"><i data-lucide=\"lock-keyhole\"></i>"
        "<input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"current-password\" required>"
        "<button id=\"password-toggle\" class=\"icon-button auth-password-toggle\" type=\"button\" aria-label=\"显示密码\" title=\"显示密码\" aria-pressed=\"false\"><i data-lucide=\"eye\"></i></button></div>"
        "<p id=\"auth-caps-lock\" class=\"auth-caps\" hidden>大写锁定已开启</p></div>"
        "<button id=\"email-login-submit\" class=\"primary-button auth-submit\" type=\"submit\" disabled><span>登录工作台</span><i data-lucide=\"arrow-right\"></i></button>"
        "</form>"
        "<div class=\"auth-links\"><a href=\"/auth/register\">注册账户</a><a href=\"/auth/reset\">重置密码</a></div>"
        "<section id=\"email-redeem-hook\" data-route=\"/auth/redeem\" hidden></section>"
        "<section id=\"email-reset-hook\" data-route=\"/auth/reset\" hidden></section>"
        "</section><p class=\"auth-note\"><i data-lucide=\"sparkles\"></i><span>使用邮箱登录，或通过验证码创建账户。</span></p>"
    )
    return _auth_frame(content, title="TradingAgents", nonce=nonce)


def _register_page(nonce: str, *, available: bool) -> str:
    disabled = "" if available else " disabled"
    status = "验证码将发送到你的邮箱。" if available else "邮箱注册暂不可用，请稍后再试。"
    content = (
        "<div class=\"auth-identity\"><span class=\"auth-mark\"><i data-lucide=\"activity\"></i></span>"
        "<h1>注册账户</h1><p>RESEARCH STUDIO / 邮箱注册</p></div>"
        f"<section class=\"auth-card\" aria-labelledby=\"register-title\" data-registration-available=\"{str(available).lower()}\"><h2 id=\"register-title\">创建账户</h2>"
        "<p class=\"auth-copy auth-restriction\"><i data-lucide=\"mail\"></i>验证邮箱</p>"
        "<p id=\"auth-message\" class=\"error-message auth-error\" role=\"alert\" hidden></p>"
        f"<p id=\"register-status\" class=\"auth-copy auth-status\" role=\"status\">{status}</p>"
        "<form id=\"email-register-code-form\" class=\"auth-form\" method=\"post\" action=\"/auth/register/code\" novalidate>"
        "<div class=\"auth-field\"><label for=\"email\">邮箱地址</label><div class=\"auth-input\"><i data-lucide=\"mail\"></i>"
        f"<input id=\"email\" name=\"email\" type=\"email\" autocomplete=\"username email\" inputmode=\"email\" autocapitalize=\"none\" spellcheck=\"false\" required{disabled}></div></div>"
        "</form>"
        "<form id=\"email-register-form\" class=\"auth-form\" method=\"post\" action=\"/auth/register\" novalidate>"
        "<div class=\"auth-field\"><label for=\"code\">6 位验证码</label><div class=\"auth-code-row\"><div class=\"auth-input\"><i data-lucide=\"shield-check\"></i>"
        f"<input id=\"code\" name=\"code\" type=\"text\" inputmode=\"numeric\" autocomplete=\"one-time-code\" pattern=\"[0-9]{{6}}\" maxlength=\"6\" required{disabled}></div>"
        "<button id=\"register-send-code\" class=\"secondary-button auth-submit\" type=\"submit\" form=\"email-register-code-form\" disabled><span>发送验证码</span><i data-lucide=\"mail\"></i></button></div></div>"
        "<div class=\"auth-field\"><label for=\"password\">密码</label><div class=\"auth-input\"><i data-lucide=\"lock-keyhole\"></i>"
        f"<input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"new-password\" minlength=\"12\" maxlength=\"128\" required{disabled}>"
        f"<button id=\"password-toggle\" class=\"icon-button auth-password-toggle\" type=\"button\" aria-label=\"显示密码\" title=\"显示密码\" aria-pressed=\"false\"{disabled}><i data-lucide=\"eye\"></i></button></div>"
        "<p id=\"auth-caps-lock\" class=\"auth-caps\" hidden>大写锁定已开启</p></div>"
        "<div class=\"auth-field\"><label for=\"password-confirm\">确认密码</label><div class=\"auth-input\"><i data-lucide=\"lock-keyhole\"></i>"
        f"<input id=\"password-confirm\" name=\"password_confirm\" type=\"password\" autocomplete=\"new-password\" minlength=\"12\" maxlength=\"128\" required{disabled}></div></div>"
        "<button id=\"email-register-submit\" class=\"primary-button auth-submit\" type=\"submit\" disabled><span>注册账户</span><i data-lucide=\"arrow-right\"></i></button>"
        "</form><div class=\"auth-links\"><a href=\"/auth/login\">返回登录</a></div></section>"
    )
    return _auth_frame(content, title="注册账户", nonce=nonce, route="register")


def _auth_page(name: str, action: str, nonce: str) -> str:
    action_name = action.split("/")[-1]
    content = (
        "<div class=\"auth-identity\"><span class=\"auth-mark\"><i data-lucide=\"activity\"></i></span>"
        f"<h1>{name}</h1><p>RESEARCH STUDIO / 账户安全</p></div>"
        f"<section class=\"auth-card\" aria-labelledby=\"{action_name}-title\"><h2 id=\"{action_name}-title\">{name}</h2>"
        "<p class=\"auth-copy auth-restriction\"><i data-lucide=\"lock-keyhole\"></i>请使用管理员提供的专属链接继续</p>"
        f"<form id=\"email-{action_name}-form\" class=\"auth-form\" method=\"post\" action=\"{action}\" novalidate>"
        "<p id=\"auth-message\" class=\"error-message auth-error\" role=\"alert\" hidden></p>"
        "<p id=\"auth-fragment-status\" class=\"auth-copy auth-status\" role=\"status\">正在验证链接…</p>"
        "<div class=\"auth-field\"><label for=\"password\">新密码</label><div class=\"auth-input\"><i data-lucide=\"lock-keyhole\"></i>"
        "<input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"new-password\" required>"
        "<button id=\"password-toggle\" class=\"icon-button auth-password-toggle\" type=\"button\" aria-label=\"显示密码\" title=\"显示密码\" aria-pressed=\"false\"><i data-lucide=\"eye\"></i></button></div>"
        "<p id=\"auth-caps-lock\" class=\"auth-caps\" hidden>大写锁定已开启</p></div>"
        f"<button id=\"email-{action_name}-submit\" class=\"primary-button auth-submit\" type=\"submit\" disabled><span>提交</span><i data-lucide=\"arrow-right\"></i></button>"
        "</form><div class=\"auth-links\"><a href=\"/auth/login\">返回登录</a></div></section>"
    )
    return _auth_frame(content, title=name, nonce=nonce, route=action_name)


def _error_payload(error: Exception) -> dict[str, Any]:
    detail = str(error)
    payload = {"detail": detail}
    if isinstance(error, AccountError):
        payload["code"] = "authentication_required" if error.status_code in {401, 403} else "account_error"
    return payload


def _registration_error_response(error: AccountError) -> JSONResponse:
    code = "registration_unavailable" if error.status_code == 503 else _error_payload(error).get("code", "account_error")
    headers = {"Retry-After": "60"} if error.status_code == 429 else None
    if headers is not None:
        return JSONResponse({"detail": str(error), "code": code}, error.status_code, headers=headers)
    return JSONResponse({"detail": str(error), "code": code}, error.status_code)


def _strict_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError


def _finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _validate_browser_origin(headers, expected_origins) -> None:
    origins = headers.getlist("origin")
    hosts = headers.getlist("host")
    if (len(origins) != 1 or origins[0] not in expected_origins
            or len(hosts) != 1 or urlsplit(origins[0]).netloc != hosts[0]):
        raise AccountError(status_code=403)


def _require_json_content_type(request: Request) -> None:
    values = request.headers.getlist("content-type")
    if len(values) != 1 or values[0].split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON request body is required.")


def _login_source(request: Request) -> str:
    client = request.client
    host = getattr(client, "host", None)
    if type(host) is not str:
        return MISSING_PEER_SOURCE
    host = host.strip()
    if not host or not host.isprintable() or len(host.encode("utf-8")) > 200:
        return MISSING_PEER_SOURCE
    return f"browser-login:{host}"


def _json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        text = body.decode("utf-8")
        depth = 0
        quoted = False
        escaped = False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ValueError
            elif character in "]}":
                depth -= 1
        value = json.loads(
            text,
            object_pairs_hook=_strict_json_pairs,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise HTTPException(400, "Invalid JSON body.") from None
    if type(value) is not dict:
        raise HTTPException(400, "Invalid JSON body.")
    return value


def _capability_header(request: Request) -> str:
    values = request.headers.getlist("x-tawb-run-capability")
    if len(values) != 1 or type(values[0]) is not str:
        raise HTTPException(403, "Invalid worker capability.")
    return values[0]


def _require_admin(session) -> None:
    if not session.principal.is_admin:
        raise AccountError(status_code=403)


def _fragment(path: str, *, token: str, email: str) -> str:
    return f"{path}#{urlencode({'token': token, 'email': email})}"


def _worker_request(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"method", "path", "headers", "body"}:
        raise HTTPException(400, "Invalid worker request.")
    method = payload["method"]
    path = payload["path"]
    headers = payload["headers"]
    body = payload["body"]
    if type(method) is not str or method not in {"GET", "POST"}:
        raise HTTPException(400, "Invalid worker request.")
    if (
        type(path) is not str
        or not path.startswith("/")
        or len(path) > 2048
        or "?" in path
        or "#" in path
    ):
        raise HTTPException(400, "Invalid worker request.")
    if type(headers) is not dict or any(type(key) is not str or type(value) is not str for key, value in headers.items()):
        raise HTTPException(400, "Invalid worker request.")
    if type(body) is not dict:
        raise HTTPException(400, "Invalid worker request.")
    return {"method": method, "path": path, "headers": headers, "body": body}


def _worker_events_request(payload: dict[str, Any]) -> list[str]:
    lines = payload.get("lines")
    if (
        set(payload) != {"lines"}
        or type(lines) is not list
        or not lines
        or len(lines) > 1000
        or any(type(line) is not str for line in lines)
    ):
        raise HTTPException(400, "Invalid worker event batch.")
    return lines


async def _run_events_stream(auth, runs, session_id: str, run_id: str, *, now_fn=time.monotonic, sleep_fn=asyncio.sleep):
    last_payload = None
    started = last_heartbeat = now_fn()
    while now_fn() - started < _STREAM_LIFETIME_SECONDS:
        current = await run_in_threadpool(auth.session, session_id)
        snapshot = await run_in_threadpool(runs.event_snapshot, current.session_id, run_id)
        payload = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":"))
        if payload != last_payload:
            yield f"event: progress\ndata: {payload}\n\n"
            last_payload = payload
            last_heartbeat = now_fn()
        now = now_fn()
        if now - last_heartbeat >= _STREAM_HEARTBEAT_SECONDS:
            yield ": keepalive\n\n"
            last_heartbeat = now
        await sleep_fn(_STREAM_POLL_SECONDS)


def _is_public_static_path(path: str) -> bool:
    return path.startswith("/static/")


def _static_asset(asset: str, request: Request):
    if asset not in EMAIL_ASSETS:
        raise HTTPException(404, "Asset not found.")
    media_type = "text/javascript" if asset.endswith((".js", ".mjs")) else "text/css" if asset.endswith(".css") else "text/plain"
    content = Path(STATIC / asset).read_bytes()
    response = Response(content, media_type=media_type, headers={
        "ETag": f'"{hashlib.sha256(content).hexdigest()}"',
    })
    unchanged = StaticFiles().is_not_modified(response.headers, request.headers)
    response.headers["etag"] = "W/" + response.headers["etag"]
    return NotModifiedResponse(response.headers) if unchanged else response


class EmailAccessMiddleware:
    def __init__(self, app, *, auth, compatible_origins=()):
        self.app = app
        self.static_app = GZipMiddleware(app, minimum_size=500, compresslevel=6)
        self.auth = auth
        self.origins = frozenset((auth.origin, *compatible_origins))
        self.hosts = frozenset(urlsplit(origin).netloc for origin in self.origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        public_asset = scope["method"] == "GET" and scope["path"].removeprefix("/static/") in EMAIL_ASSETS and scope["path"].startswith("/static/")
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                cache_control = b"public, no-cache" if public_asset and message["status"] in {200, 304} else b"no-store"
                if public_asset and message["status"] in {200, 304}:
                    MutableHeaders(scope=message)["Vary"] = "Accept-Encoding"
                append_security_headers(message, cache_control=cache_control)
            await send(message)

        try:
            request_hosts = request.headers.getlist("host")
            if len(request_hosts) != 1 or request_hosts[0] not in self.hosts:
                await JSONResponse({"detail": "Invalid request host."}, 400)(scope, receive, safe_send)
                return
            write = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
            if write:
                try:
                    body = await read_bounded_body(request, limit=_body_limit_for_path(scope["path"]))
                except BodyTooLargeError:
                    await JSONResponse({"detail": "Request body is too large."}, 413)(scope, receive, safe_send)
                    return

                async def body_receive():
                    return {"type": "http.request", "body": body, "more_body": False}

                receive = body_receive
                request = Request(scope, receive)
            path = scope["path"]
            if write and path in BROWSER_AUTH_WRITE_PATHS:
                _validate_browser_origin(request.headers, self.origins)
            if path not in PUBLIC_PATHS and path not in CAPABILITY_PATHS and not _is_public_static_path(path):
                session = await run_in_threadpool(self.auth.session, request.cookies.get(SESSION_COOKIE))
                scope.setdefault("state", {})["session"] = session
                if write:
                    tokens = request.headers.getlist("x-workbench-token")
                    _validate_browser_origin(request.headers, self.origins)
                    if len(tokens) != 1:
                        raise AccountError(status_code=403)
                    self.auth.check_csrf(session, tokens[0])
            await (self.static_app if public_asset else self.app)(scope, receive, safe_send)
        except AccountError as error:
            if not started:
                if scope["path"] == "/" and scope["method"] == "GET" and error.status_code == 401:
                    response = RedirectResponse("/auth/login", status_code=303)
                else:
                    response = JSONResponse(_error_payload(error), error.status_code)
                await response(scope, receive, safe_send)
        except (SettingsError, HostedRunsError, GatewayError, MarketError) as error:
            if not started:
                await JSONResponse({"detail": str(error)}, error.status_code)(scope, receive, safe_send)
        except AssistantError as error:
            if not started:
                await JSONResponse({"detail": str(error)}, error.status_code)(scope, receive, safe_send)
        except BudgetError as error:
            if not started:
                code = "budget_exhausted" if error.status_code == 402 else "budget_unavailable"
                status = 429 if error.status_code == 402 else error.status_code
                await JSONResponse({"detail": str(error), "code": code}, status)(scope, receive, safe_send)
        except Exception:
            if not started:
                await JSONResponse({"detail": "Workbench request could not be completed."}, 503)(scope, receive, safe_send)


def create_email_app(*, auth, settings, budget, runs, gateway, registration=None, compatible_origins=(), assistant=None, market=None):
    if type(compatible_origins) not in (tuple, list) or len(compatible_origins) > 8:
        raise ValueError("Invalid compatible origins")
    normalized_origins = tuple(public_origin(origin) for origin in compatible_origins)
    if (len(set(normalized_origins)) != len(normalized_origins)
            or auth.origin in normalized_origins
            or any(origin != value for origin, value in zip(normalized_origins, compatible_origins, strict=True))):
        raise ValueError("Invalid compatible origins")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(EmailAccessMiddleware, auth=auth, compatible_origins=normalized_origins)
    registration = registration if registration is not None else EmailRegistration(auth)
    assistant = assistant if assistant is not None else ResearchAssistant(
        store=auth.store, auth=auth, runs=runs, gateway=gateway,
    )
    market = market if market is not None else MarketData(store=auth.store, settings=settings)
    admin = PublicAdmin(auth=auth, budget=budget)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        del request
        nonce = token_urlsafe(24)
        content = (STATIC / "index.html").read_text(encoding="utf-8")
        content = content.replace("<script>", f'<script nonce="{nonce}">', 1)
        policy = CSP.replace("script-src 'self'", f"script-src 'self' 'nonce-{nonce}'")
        return HTMLResponse(content, headers={"Content-Security-Policy": policy})

    @app.get("/static/{asset:path}")
    def static_asset(asset: str, request: Request):
        return _static_asset(asset, request)

    @app.get("/auth/login", response_class=HTMLResponse)
    def login_page():
        nonce = token_urlsafe(24)
        return HTMLResponse(_login_page(nonce), headers={"Content-Security-Policy": _page_policy(nonce)})

    @app.post("/auth/login")
    async def login(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"email", "password"}:
            raise HTTPException(400, "Invalid account input.")
        session = await run_in_threadpool(
            auth.login,
            payload["email"],
            payload["password"],
            source=_login_source(request),
        )
        response = JSONResponse({"user": asdict(session.principal), "csrf_token": session.csrf_token})
        _set_cookie(response, SESSION_COOKIE, session.session_id, session.expires_at - int(auth.clock()))
        return response

    @app.get("/auth/register", response_class=HTMLResponse)
    def register_page():
        nonce = token_urlsafe(24)
        return HTMLResponse(
            _register_page(nonce, available=bool(getattr(registration, "available", False))),
            headers={"Content-Security-Policy": _page_policy(nonce)},
        )

    @app.post("/auth/register/code")
    async def register_code(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"email"}:
            raise HTTPException(400, "Invalid account input.")
        try:
            result = await run_in_threadpool(
                registration.request_code,
                payload["email"],
                source=_login_source(request),
            )
        except AccountError as error:
            return _registration_error_response(error)
        if set(result) != {"accepted", "expires_in", "retry_after"} or result.get("accepted") is not True:
            return _registration_error_response(AccountError(status_code=503))
        return result

    @app.post("/auth/register")
    async def register(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"email", "code", "password"}:
            raise HTTPException(400, "Invalid account input.")
        try:
            session = await run_in_threadpool(
                registration.register,
                payload["email"],
                payload["code"],
                payload["password"],
                source=_login_source(request),
            )
        except AccountError as error:
            return _registration_error_response(error)
        response = JSONResponse({"user": asdict(session.principal), "csrf_token": session.csrf_token})
        _set_cookie(response, SESSION_COOKIE, session.session_id, session.expires_at - int(auth.clock()))
        return response

    @app.get("/auth/redeem", response_class=HTMLResponse)
    def redeem_page():
        nonce = token_urlsafe(24)
        return HTMLResponse(_auth_page("兑换邀请", "/auth/redeem", nonce), headers={"Content-Security-Policy": _page_policy(nonce)})

    @app.post("/auth/redeem")
    async def redeem(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"token", "email", "password"}:
            raise HTTPException(400, "Invalid account input.")
        session = await run_in_threadpool(
            auth.redeem,
            payload["token"],
            payload["email"],
            payload["password"],
        )
        response = JSONResponse({"user": asdict(session.principal), "csrf_token": session.csrf_token})
        _set_cookie(response, SESSION_COOKIE, session.session_id, session.expires_at - int(auth.clock()))
        return response

    @app.get("/auth/reset", response_class=HTMLResponse)
    def reset_page():
        nonce = token_urlsafe(24)
        return HTMLResponse(_auth_page("重置密码", "/auth/reset", nonce), headers={"Content-Security-Policy": _page_policy(nonce)})

    @app.post("/auth/reset")
    async def reset(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"token", "email", "password"}:
            raise HTTPException(400, "Invalid account input.")
        session = await run_in_threadpool(
            auth.reset,
            payload["token"],
            payload["email"],
            payload["password"],
        )
        response = JSONResponse({"user": asdict(session.principal), "csrf_token": session.csrf_token})
        _set_cookie(response, SESSION_COOKIE, session.session_id, session.expires_at - int(auth.clock()))
        return response

    @app.get("/signed-out", response_class=HTMLResponse)
    def signed_out():
        nonce = token_urlsafe(24)
        return HTMLResponse(
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>TradingAgents</title>{_theme_bootstrap(nonce)}<link rel=\"stylesheet\" href=\"/static/styles.css\"></head>"
            "<body class=\"auth-shell\"><main class=\"auth-card\"><p class=\"auth-eyebrow\">TradingAgents 工作台</p>"
            "<h1>已退出工作台</h1><p class=\"auth-copy\">当前邮箱会话已结束。</p><div class=\"auth-links\">"
            "<a href=\"/auth/login\">使用邮箱登录</a></div></main></body></html>",
            headers={"Content-Security-Policy": _page_policy(nonce)},
        )

    @app.get("/api/me")
    def me(request: Request):
        session = request.state.session
        return {"user": asdict(session.principal), "csrf_token": session.csrf_token,
            "permissions": auth.feature_permissions(session.principal)}

    @app.post("/auth/logout")
    def logout(request: Request):
        auth.logout(request.state.session.session_id)
        response = JSONResponse({"signed_out": True})
        _delete_cookie(response, SESSION_COOKIE)
        return response

    @app.post("/auth/password")
    async def change_password(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"current_password", "new_password"}:
            raise HTTPException(400, "Invalid account input.")
        session = await run_in_threadpool(
            auth.change_password,
            request.state.session.session_id,
            payload["current_password"],
            payload["new_password"],
        )
        response = JSONResponse({"user": asdict(session.principal), "csrf_token": session.csrf_token})
        _set_cookie(response, SESSION_COOKIE, session.session_id, session.expires_at - int(auth.clock()))
        return response

    @app.get("/api/settings/catalog")
    def settings_catalog(request: Request):
        is_owner = request.state.session.principal.is_admin
        configuration = gateway.managed_identity_configuration() if is_owner else None
        configurations = gateway.managed_identity_configurations() if is_owner else []
        available = is_owner and gateway.managed_identity_available()
        catalog = settings.catalog()
        modes = dict(catalog["modes"])
        modes["managed_identity"] = {
            **modes["managed_identity"],
            "available": available,
            "configuration": configuration if available else None,
            "configurations": configurations if is_owner and available else [],
        }
        assistant_available = getattr(gateway, "assistant_available", lambda: False)()
        return {**catalog, "modes": modes, "assistant_available": assistant_available}

    @app.get("/api/settings")
    def settings_get(request: Request):
        return settings.get(request.state.session.principal)

    @app.put("/api/settings")
    async def settings_update(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if payload.get("mode") == "managed_identity" and not gateway.managed_identity_available():
            raise HTTPException(409, "Managed identity is not available in this installation.")
        if "managed_models" in payload:
            if not request.state.session.principal.is_admin:
                raise HTTPException(403, "Settings operation is not permitted.")
            try:
                gateway.validate_managed_models(payload["managed_models"])
            except GatewayError as error:
                raise HTTPException(error.status_code, error.args[0]) from None
        expected = request.headers.get("if-match")
        return await run_in_threadpool(
            settings.update,
            request.state.session.principal,
            payload,
            expected_version=expected,
        )

    @app.get("/api/budget")
    def budget_get(request: Request):
        _require_admin(request.state.session)
        return budget.snapshot()

    @app.get("/api/market/watchlist")
    def market_watchlist(request: Request):
        return market.watchlist(request.state.session.principal)

    @app.put("/api/market/watchlist")
    async def market_watchlist_update(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        versions = request.headers.getlist("if-match")
        if len(versions) > 1:
            raise MarketError()
        return await run_in_threadpool(
            market.update_watchlist, request.state.session.principal, payload,
            expected_version=versions[0] if versions else None,
        )

    @app.post("/api/market/snapshot")
    async def market_snapshot(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        return await run_in_threadpool(market.snapshot, request.state.session.principal, payload)

    @app.get("/api/assistant/sessions")
    def assistant_session_list(request: Request):
        return {"sessions": assistant.list(request.state.session.session_id)}

    @app.post("/api/assistant/sessions", status_code=201)
    async def assistant_session_create(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if payload:
            raise HTTPException(400, "Invalid research assistant input.")
        return await run_in_threadpool(assistant.create, request.state.session.session_id)

    @app.get("/api/assistant/sessions/{assistant_session_id}")
    def assistant_session_get(assistant_session_id: str, request: Request):
        return assistant.get(request.state.session.session_id, assistant_session_id)

    @app.patch("/api/assistant/sessions/{assistant_session_id}")
    async def assistant_session_rename(assistant_session_id: str, request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"title"}:
            raise HTTPException(400, "Invalid research assistant input.")
        return await run_in_threadpool(
            assistant.rename, request.state.session.session_id, assistant_session_id, payload["title"],
        )

    @app.delete("/api/assistant/sessions/{assistant_session_id}")
    async def assistant_session_delete(assistant_session_id: str, request: Request):
        return await run_in_threadpool(assistant.delete, request.state.session.session_id, assistant_session_id)

    @app.post("/api/assistant/sessions/{assistant_session_id}/messages")
    async def assistant_send(assistant_session_id: str, request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"turn_id", "content"}:
            raise HTTPException(400, "Invalid research assistant input.")
        return await run_in_threadpool(
            assistant.send, request.state.session.session_id, assistant_session_id,
            payload["turn_id"], payload["content"],
        )

    @app.get("/api/admin/accounts")
    def admin_accounts(request: Request):
        return auth.accounts(request.state.session.session_id)

    @app.patch("/api/admin/accounts/{user_id}")
    async def admin_account_update(user_id: str, request: Request):
        _require_admin(request.state.session)
        _require_json_content_type(request)
        versions = request.headers.getlist("if-match")
        if len(versions) != 1 or not versions[0]:
            raise AccountError(status_code=409)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        return await run_in_threadpool(auth.update_account, request.state.session.session_id,
                                      user_id, payload, expected_version=versions[0])

    @app.get("/api/admin/audit")
    def admin_audit(request: Request):
        return auth.audit(request.state.session.session_id)

    @app.get("/api/admin/overview")
    def admin_overview(request: Request):
        return admin.overview(request.state.session.session_id)

    @app.get("/api/admin/usage")
    def admin_usage(request: Request):
        auth.accounts(request.state.session.session_id)
        values = request.query_params
        if set(values) - {"days", "account_id"} or any(len(values.getlist(key)) != 1 for key in values):
            raise AccountError(status_code=400)
        days = values.get("days", "30")
        if days not in {"7", "30", "90"}:
            raise AccountError(status_code=400)
        return admin.usage_report(request.state.session.session_id, days=int(days), account_id=values.get("account_id"))

    @app.get("/api/admin/users")
    def admin_users(request: Request):
        return {"users": auth.users(request.state.session.session_id)}

    @app.get("/api/admin/approved-origins")
    def admin_approved_origins(request: Request):
        _require_admin(request.state.session)
        return settings.policy(request.state.session.principal)

    @app.put("/api/admin/approved-origins")
    async def admin_update_approved_origins(request: Request):
        _require_admin(request.state.session)
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        expected = request.headers.get("if-match")
        return await run_in_threadpool(
            settings.update_policy,
            request.state.session.principal,
            payload,
            expected_version=expected,
        )

    @app.post("/api/admin/invitations")
    async def admin_invite(request: Request):
        _require_admin(request.state.session)
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"email"}:
            raise HTTPException(400, "Invalid account input.")
        invite = await run_in_threadpool(auth.invite, request.state.session.session_id, payload["email"])
        return {
            "token_id": invite.token_id,
            "email": invite.email,
            "expires_at": invite.expires_at,
            "fragment": _fragment("/auth/redeem", token=invite.token, email=invite.email),
        }

    @app.delete("/api/admin/invitations/{token_id}")
    def admin_revoke_invite(token_id: str, request: Request):
        _require_admin(request.state.session)
        auth.revoke_invite(request.state.session.session_id, token_id)
        return {"revoked": True}

    @app.post("/api/admin/recoveries")
    async def admin_recovery(request: Request):
        _require_admin(request.state.session)
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"user_id"}:
            raise HTTPException(400, "Invalid account input.")
        recovery = await run_in_threadpool(auth.recovery, request.state.session.session_id, payload["user_id"])
        return {
            "token_id": recovery.token_id,
            "email": recovery.email,
            "expires_at": recovery.expires_at,
            "fragment": _fragment("/auth/reset", token=recovery.token, email=recovery.email),
        }

    @app.post("/api/admin/users/{user_id}/disable")
    def admin_disable(user_id: str, request: Request):
        _require_admin(request.state.session)
        versions = request.headers.getlist("if-match")
        if len(versions) != 1 or not versions[0]:
            raise AccountError(status_code=409)
        auth.disable(request.state.session.session_id, user_id, expected_version=versions[0])
        return {"disabled": True}

    @app.get("/api/runs")
    def run_list(request: Request):
        return {"runs": runs.list(request.state.session.session_id)}

    @app.post("/api/runs", status_code=202)
    async def run_create(request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if set(payload) != {"submission_id", "request"}:
            raise HTTPException(400, "Invalid hosted run request.")
        return await run_in_threadpool(
            runs.submit,
            request.state.session.session_id,
            payload["request"],
            payload["submission_id"],
        )

    @app.get("/api/runs/{run_id}")
    def run_get(run_id: str, request: Request):
        return runs.refresh(request.state.session.session_id, run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request):
        session = request.state.session
        await run_in_threadpool(runs.event_snapshot, session.session_id, run_id)
        return StreamingResponse(_run_events_stream(auth, runs, session.session_id, run_id), media_type="text/event-stream")

    @app.post("/api/runs/{run_id}/reconcile")
    async def run_reconcile(run_id: str, request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if payload:
            raise HTTPException(400, "Invalid hosted run request.")
        return await run_in_threadpool(runs.reconcile, request.state.session.session_id, run_id)

    @app.post("/api/runs/{run_id}/resume")
    async def run_resume(run_id: str, request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if payload:
            raise HTTPException(400, "Invalid hosted run request.")
        return await run_in_threadpool(runs.resume, request.state.session.session_id, run_id)

    @app.post("/api/runs/{run_id}/stop")
    async def run_stop(run_id: str, request: Request):
        _require_json_content_type(request)
        payload = _json_body(await read_bounded_body(request, limit=BODY_LIMIT))
        if payload:
            raise HTTPException(400, "Invalid hosted run request.")
        return await run_in_threadpool(runs.stop, request.state.session.session_id, run_id)

    @app.get("/api/runs/{run_id}/download")
    def run_download(run_id: str, request: Request):
        record = runs.get(request.state.session.session_id, run_id)
        output_format = request.query_params.get("format", "json")
        if output_format not in {"json", "markdown"}:
            raise HTTPException(400, "Invalid download format.")
        content = _json_bytes(record) if output_format == "json" else _markdown(record)
        extension = "json" if output_format == "json" else "md"
        media = "application/json" if output_format == "json" else "text/markdown"
        return Response(
            content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="workbench-{run_id}.{extension}"'},
        )

    @app.post("/api/internal/worker/provider-dispatch")
    async def worker_dispatch(request: Request):
        capability = _capability_header(request)
        _require_json_content_type(request)
        payload = _worker_request(_json_body(await read_bounded_body(request, limit=WORKER_BODY_LIMIT)))
        result = await run_in_threadpool(gateway.dispatch, capability, **payload)
        return JSONResponse(dict(result))

    @app.post("/api/internal/worker/events")
    async def worker_events(request: Request):
        capability = _capability_header(request)
        _require_json_content_type(request)
        lines = _worker_events_request(_json_body(await read_bounded_body(request, limit=WORKER_BODY_LIMIT)))
        result = await run_in_threadpool(runs.ingest_worker_events, capability, lines)
        return JSONResponse(result)

    return app
