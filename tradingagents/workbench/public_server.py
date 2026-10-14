"""Authenticated HTTP boundary for the public research workbench."""

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tradingagents.workbench import contracts
from tradingagents.workbench.public_auth import (
    ARM_SCOPE,
    FLOW_SECONDS,
    SESSION_SECONDS,
    AuthenticationRequired,
    LoginThrottled,
)
from tradingagents.workbench.public_keys import ProviderKeys
from tradingagents.workbench.public_profiles import AzureResources, Profiles, ResourceError
from tradingagents.workbench.public_provisioning import Provisioning
from tradingagents.workbench.public_runs import DelegatedAzureController, PublicRuns
from tradingagents.workbench.public_store import StoreConflict, StoreUnavailable
from tradingagents.workbench.server import _json_bytes, _markdown

SESSION_COOKIE = "__Host-tawb-session"
FLOW_COOKIE = "__Host-tawb-login"
BODY_LIMIT = 16384
PUBLIC_PATHS = frozenset({"/auth/login", "/auth/callback", "/signed-out"})
STATIC = Path(__file__).with_name("static")
ASSETS = frozenset({
    "account-ui.js",
    "agent-process.mjs",
    "app.js",
    "auth-studio.css",
    "auth-studio.js",
    "auth-locale.mjs",
    "locale.mjs",
    "boot.js",
    "model-settings.js",
    "public-init.js",
    "public-ui.js",
    "research-assistant.mjs",
    "styles.css",
    "studio-ui.js",
    "ui-core.mjs",
    "vendor.js",
    "vendor.js.LEGAL.txt",
})
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'; object-src 'none'")


class BodyTooLargeError(RuntimeError):
    pass


def append_security_headers(message, *, policy=CSP, cache_control=b"no-store"):
    headers = list(message.get("headers", []))
    headers.extend([
        (b"cache-control", cache_control),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"strict-transport-security", b"max-age=31536000"),
    ])
    if not any(name.lower() == b"content-security-policy" for name, _ in headers):
        headers.append((b"content-security-policy", policy.encode("ascii")))
    message["headers"] = headers
    return message


def host_header_is_valid(headers, expected_host):
    return headers.getlist("host") == [expected_host]


async def read_bounded_body(request, *, limit=BODY_LIMIT):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise BodyTooLargeError()
    return bytes(body)


class PublicAccessMiddleware:
    def __init__(self, app, *, auth):
        self.app = app
        self.auth = auth
        self.host = urlsplit(auth.origin).netloc

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        headers = request.headers
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                append_security_headers(message)
            await send(message)

        try:
            if scope["path"] != "/healthz":
                if not host_header_is_valid(headers, self.host):
                    await JSONResponse({"detail": "Invalid request host."}, 400)(scope, receive, safe_send)
                    return
                origins = headers.getlist("origin")
                if origins and origins != [self.auth.origin]:
                    raise AuthenticationRequired(status_code=403)
                write = scope["method"] not in {"GET", "HEAD", "OPTIONS"}
                if scope["path"] not in PUBLIC_PATHS:
                    session = await run_in_threadpool(
                        self.auth.session, request.cookies.get(SESSION_COOKIE)
                    )
                    scope.setdefault("state", {})["session"] = session
                    if write:
                        tokens = headers.getlist("x-workbench-token")
                        if origins != [self.auth.origin] or len(tokens) != 1:
                            raise AuthenticationRequired(status_code=403)
                        self.auth.check_csrf(session, tokens[0])
                if write:
                    try:
                        body = await read_bounded_body(request)
                    except BodyTooLargeError:
                        await JSONResponse({"detail": "Request body is too large."}, 413)(scope, receive, safe_send)
                        return

                    async def body_receive():
                        return {"type": "http.request", "body": bytes(body), "more_body": False}

                    await self.app(scope, body_receive, safe_send)
                    return
            await self.app(scope, receive, safe_send)
        except LoginThrottled:
            if not started:
                await JSONResponse(
                    {"detail": "Too many login attempts. Retry shortly."},
                    429, headers={"Retry-After": "60"},
                )(scope, receive, safe_send)
        except AuthenticationRequired as error:
            if not started:
                if scope["path"] == "/" and scope["method"] == "GET" and error.status_code == 401:
                    response = RedirectResponse("/auth/login", status_code=303)
                else:
                    response = JSONResponse(
                        {"detail": str(error), "code": "authentication_required", "scope": error.scope},
                        error.status_code,
                    )
                await response(scope, receive, safe_send)
        except ResourceError as error:
            if not started:
                await JSONResponse({"detail": str(error)}, error.status_code)(scope, receive, safe_send)
        except StoreConflict:
            if not started:
                await JSONResponse({"detail": "State changed. Refresh before retrying."}, 409)(scope, receive, safe_send)
        except StoreUnavailable:
            if not started:
                await JSONResponse({"detail": "Secure application storage is unavailable."}, 503)(scope, receive, safe_send)
        except Exception:
            if not started:
                await JSONResponse({"detail": "Workbench request could not be completed."}, 503)(scope, receive, safe_send)


def _set_cookie(response, name, value, max_age):
    response.set_cookie(name, value, max_age=max_age, secure=True, httponly=True, samesite="lax", path="/")


def _delete_cookie(response, name):
    response.delete_cookie(name, secure=True, httponly=True, samesite="lax", path="/")


def create_public_app(*, auth, resource_factory=AzureResources, controller_factory=DelegatedAzureController, source=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(PublicAccessMiddleware, auth=auth)
    profiles = Profiles(auth.store)
    provisioning = Provisioning(auth=auth, resource_factory=resource_factory, source=source, clock=auth.clock)
    runs = PublicRuns(auth=auth, profiles=profiles, resource_factory=resource_factory,
                      controller_factory=controller_factory, clock=auth.clock)
    provider_keys = ProviderKeys(auth=auth, profiles=profiles, runs=runs, resource_factory=resource_factory)

    @contextmanager
    def resources(request):
        session = request.state.session
        cloud = resource_factory(session.principal, auth.credential(session.session_id))
        try:
            yield cloud
        finally:
            cloud.close()

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index():
        nonce = token_urlsafe(24)
        content = (STATIC / "index.html").read_text(encoding="utf-8")
        content = content.replace("<script>", f'<script nonce="{nonce}">')
        policy = CSP.replace("script-src 'self'", f"script-src 'self' 'nonce-{nonce}'")
        return HTMLResponse(content, headers={"Content-Security-Policy": policy})

    @app.get("/static/{asset:path}")
    def static_asset(asset: str):
        if asset not in ASSETS:
            raise HTTPException(404, "Asset not found.")
        media_type = "text/javascript" if asset.endswith((".js", ".mjs")) else "text/css" if asset.endswith(".css") else "text/plain"
        return FileResponse(STATIC / asset, media_type=media_type)

    @app.get("/signed-out", response_class=HTMLResponse)
    def signed_out():
        return HTMLResponse('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width, initial-scale=1">'
                            '<title>TradingAgents</title></head><body><main>'
                            '<h1>TradingAgents</h1><p>\u5df2\u9000\u51fa\u5de5\u4f5c\u53f0</p>'
                            '<a href="/auth/login">\u4f7f\u7528 Entra ID \u767b\u5f55</a>'
                            '</main></body></html>')

    @app.get("/auth/login")
    def login(request: Request):
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            try:
                auth.session(session_id)
            except AuthenticationRequired:
                session_id = None
        try:
            flow = auth.begin_login(
                scope=request.query_params.get("scope", ARM_SCOPE), session_id=session_id,
            )
        except ValueError:
            raise HTTPException(400, "Unsupported authorization resource.") from None
        response = RedirectResponse(flow.authorize_url, status_code=303)
        _set_cookie(response, FLOW_COOKIE, flow.flow_id, FLOW_SECONDS)
        return response

    @app.get("/auth/callback")
    def callback(request: Request):
        pairs = request.query_params.multi_items()
        if len(pairs) != len(dict(pairs)):
            raise HTTPException(400, "Duplicate authorization response parameter.")
        previous = request.cookies.get(SESSION_COOKIE)
        session = auth.complete_login(
            request.cookies.get(FLOW_COOKIE), dict(pairs), session_id=previous,
        )
        if previous:
            auth.logout(previous)
        response = RedirectResponse("/", status_code=303)
        _delete_cookie(response, FLOW_COOKIE)
        _set_cookie(response, SESSION_COOKIE, session.session_id, SESSION_SECONDS)
        return response

    @app.get("/api/me")
    def me(request: Request):
        session = request.state.session
        return {"user": asdict(session.principal), "csrf_token": session.csrf_token}

    @app.post("/auth/logout")
    def logout(request: Request):
        auth.logout(request.state.session.session_id)
        response = JSONResponse({"signed_out": True})
        _delete_cookie(response, SESSION_COOKIE)
        _delete_cookie(response, FLOW_COOKIE)
        return response

    @app.get("/api/profiles")
    def profile_list(request: Request):
        return {"profiles": profiles.list(request.state.session.principal)}

    @app.get("/api/profiles/{profile_id}")
    def profile_get(profile_id: str, request: Request):
        return profiles.get(request.state.session.principal, profile_id)

    @app.post("/api/profiles", status_code=201)
    def profile_create(payload: dict, request: Request):
        with resources(request) as cloud:
            return profiles.save(request.state.session.principal, payload, cloud)

    @app.put("/api/profiles/{profile_id}")
    def profile_update(profile_id: str, payload: dict, request: Request):
        with resources(request) as cloud:
            return profiles.save(request.state.session.principal, payload, cloud, profile_id,
                                 request.headers.get("if-match"))

    @app.delete("/api/profiles/{profile_id}")
    def profile_delete(profile_id: str, request: Request):
        profiles.delete(request.state.session.principal, profile_id, request.headers.get("if-match"))
        return {"deleted": True}

    @app.post("/api/resources/validate")
    def resource_validate(payload: dict, request: Request):
        with resources(request) as cloud:
            return cloud.validate(payload)

    @app.get("/api/profiles/{profile_id}/providers")
    def provider_status(profile_id: str, request: Request):
        return provider_keys.get(request.state.session.session_id, profile_id)

    @app.put("/api/profiles/{profile_id}/providers")
    def provider_update(profile_id: str, payload: dict, request: Request):
        return provider_keys.update(request.state.session.session_id, profile_id, payload)

    @app.get("/api/resources/subscriptions")
    def subscriptions(request: Request):
        with resources(request) as cloud:
            return {"subscriptions": cloud.subscriptions()}

    @app.get("/api/resources/groups")
    def groups(subscription_id: str, request: Request):
        with resources(request) as cloud:
            return {"groups": cloud.groups(subscription_id)}

    @app.get("/api/resources/catalog")
    def catalog(subscription_id: str, group: str, request: Request):
        with resources(request) as cloud:
            return cloud.catalog(subscription_id, group)

    @app.get("/api/resources/deployments")
    def deployments(account_id: str, request: Request):
        with resources(request) as cloud:
            return {"deployments": cloud.deployments(account_id)}

    @app.get("/api/config")
    def configuration(request: Request):
        owned = profiles.list(request.state.session.principal)
        return {"defaults": contracts.default_request(), "public_mode": True,
                "analyst_choices": [{"value": name, "label": name} for name in contracts.ANALYSTS],
                "vendor_choices": {category: list(vendors) for category, vendors in contracts.VENDOR_OPTIONS.items()},
                "model": owned[0]["config"]["model"] if owned else None,
                "deployment_ready": bool(owned), "csrf_token": request.state.session.csrf_token}

    @app.get("/api/initializations")
    def initialization_list(request: Request):
        return {"initializations": provisioning.list(request.state.session.session_id)}

    @app.post("/api/initializations", status_code=201)
    def initialization_preview(payload: dict, request: Request):
        return provisioning.preview(request.state.session.session_id, payload)

    @app.get("/api/initializations/{operation_id}")
    def initialization_get(operation_id: str, request: Request):
        return provisioning.get(request.state.session.session_id, operation_id)

    @app.post("/api/initializations/{operation_id}/confirm")
    def initialization_confirm(operation_id: str, payload: dict, request: Request):
        if set(payload) != {"confirmation"}:
            raise ResourceError("Only the exact preview confirmation is accepted.")
        return provisioning.confirm(request.state.session.session_id, operation_id, payload["confirmation"])

    @app.post("/api/initializations/{operation_id}/apply", status_code=202)
    def initialization_apply(operation_id: str, payload: dict, request: Request):
        if payload:
            raise ResourceError("Initialization uses the confirmed preview; no changes are accepted.")
        return provisioning.apply(request.state.session.session_id, operation_id)

    @app.get("/api/runs")
    def run_list(request: Request):
        return {"runs": runs.list(request.state.session.session_id)}

    @app.post("/api/runs", status_code=202)
    def run_create(payload: dict, request: Request):
        if set(payload) != {"profile_id", "submission_id", "request"}:
            raise ResourceError("Select a profile and submit a unique research request.")
        return runs.create(request.state.session.session_id, payload["profile_id"],
                           payload["request"], payload["submission_id"])

    @app.get("/api/runs/{run_id}")
    def run_get(run_id: str, request: Request):
        return runs.refresh(request.state.session.session_id, run_id)

    @app.post("/api/runs/{run_id}/stop")
    def run_stop(run_id: str, request: Request):
        return runs.stop(request.state.session.session_id, run_id)

    @app.post("/api/runs/{run_id}/reconcile")
    def run_reconcile(run_id: str, payload: dict, request: Request):
        if set(payload) != {"execution_name"}:
            raise ResourceError("Only the Azure execution name is accepted.")
        return runs.reconcile(request.state.session.session_id, run_id, payload["execution_name"])

    @app.get("/api/runs/{run_id}/download")
    def run_download(run_id: str, request: Request):
        record = runs.get(request.state.session.session_id, run_id)
        output_format = request.query_params.get("format", "json")
        if output_format not in {"json", "markdown"}:
            raise ResourceError("Invalid download format.")
        content = _json_bytes(record) if output_format == "json" else _markdown(record)
        extension = "json" if output_format == "json" else "md"
        return Response(content, media_type="application/json" if output_format == "json" else "text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="workbench-{run_id}.{extension}"'})

    return app
