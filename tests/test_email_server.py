# Modified for public distribution: installation identifiers replaced with examples.
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_public_accounts import ORIGIN, OWNER_PASSWORD
from tests.test_public_execution import NOW, ArmWire, HostCredential, installation
from tests.test_public_gateway import foundry_installation
from tests.test_public_store import MemoryBackend
from tradingagents.workbench.contracts import default_request, encode_event
from tradingagents.workbench.public_accounts import ADMISSION_KEY, AccountError, EmailAuth
from tradingagents.workbench.public_budget import BudgetLedger
from tradingagents.workbench.public_execution import HostedExecution
from tradingagents.workbench.public_gateway import ProviderGateway
from tradingagents.workbench.public_hosted_runs import HostedRuns
from tradingagents.workbench.public_settings import UserSettings
from tradingagents.workbench.public_store import EncryptedStore

GUEST_PASSWORD = "fixture-guest-password-2026"


def test_market_routes_guest_isolation_cas_empty_snapshot_and_csrf(app_case):
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        assert client.get("/api/market/watchlist").status_code == 401
        assert client.post("/api/market/snapshot", json={}).status_code == 401
        session = login(client, "guest@example.com", GUEST_PASSWORD)
        headers = {"Origin": ORIGIN, "x-workbench-token": session["csrf_token"]}
        response = client.get("/api/market/watchlist")
        assert response.status_code == 200
        assert response.json() == {"symbols": [], "version": None}
        assert response.headers["cache-control"] == "no-store"
        assert client.put("/api/market/watchlist", json={"symbols": []}).status_code == 403
        saved = client.put("/api/market/watchlist", json={"symbols": ["msft"]}, headers=headers)
        assert saved.status_code == 200
        assert saved.json()["symbols"] == ["MSFT"]
        assert client.put("/api/market/watchlist", json={"symbols": []}, headers=headers).status_code == 409
        cleared = client.put("/api/market/watchlist", json={"symbols": []}, headers={**headers, "If-Match": saved.json()["version"]})
        assert cleared.status_code == 200
        result = client.post("/api/market/snapshot", json={"symbols": [], "selected": None, "range": "1m", "source": "alpha_vantage"}, headers=headers)
        assert result.status_code == 200
        assert result.json()["quotes"] == []
        assert result.json()["interval"] == "1d"
        assert result.headers["cache-control"] == "no-store"
        assert client.post("/api/market/snapshot", json={"symbols": ["MSFT"], "selected": "AAPL", "range": "1m", "source": "yfinance"}, headers=headers).status_code == 400
        login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        assert client.get("/api/market/watchlist").json() == {"symbols": [], "version": None}


def test_market_service_injection_and_provider_failures_are_safe(app_case, monkeypatch):
    from tradingagents.workbench.email_server import create_email_app
    from tradingagents.workbench.public_market import MarketData

    def unavailable(symbol):
        raise RuntimeError("secret-do-not-disclose")

    monkeypatch.setattr("yfinance.Ticker", unavailable)
    market = MarketData(store=app_case.store, settings=app_case.settings)
    app = create_email_app(auth=app_case.auth, settings=app_case.settings, budget=app_case.budget,
                           runs=app_case.runs, gateway=app_case.gateway, market=market)
    with TestClient(app, base_url=ORIGIN) as client:
        session = login(client, "guest@example.com", GUEST_PASSWORD)
        headers = {"Origin": ORIGIN, "x-workbench-token": session["csrf_token"]}
        response = client.post("/api/market/snapshot", json={"symbols": ["MSFT"], "selected": "MSFT", "range": "1m", "source": "yfinance"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["errors"] == [{"symbol": "MSFT", "code": "provider_unavailable", "message": "Market data is unavailable."}]
        assert "secret-do-not-disclose" not in response.text


def test_market_static_allowlist_does_not_allow_arbitrary_files(app_case, tmp_path, monkeypatch):
    from tradingagents.workbench import email_server

    monkeypatch.setattr(email_server, "STATIC", tmp_path)
    for name in ("market-data.mjs", "market-chart.mjs", "private.mjs"):
        (tmp_path / name).write_text("export const fixture = true;", encoding="ascii")
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        for name in ("market-data.mjs", "market-chart.mjs"):
            response = client.get(f"/static/{name}")
            assert response.status_code == 200
            assert "javascript" in response.headers["content-type"]
        assert client.get("/static/private.mjs").status_code == 404


@pytest.mark.parametrize("page_name", ["login", "register", "register-unavailable", "redeem", "reset"])
def test_auth_html_blocks_submission_until_client_handlers_are_ready(page_name):
    from tradingagents.workbench.email_server import _auth_page, _login_page, _register_page

    buttons = []

    class SubmitButtons(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "button" and attributes.get("type") == "submit":
                buttons.append(attributes)

    if page_name == "login":
        html = _login_page("fixture-nonce")
    elif page_name.startswith("register"):
        html = _register_page("fixture-nonce", available=page_name == "register")
    else:
        html = _auth_page(page_name, f"/auth/{page_name}", "fixture-nonce")
    SubmitButtons().feed(html)
    assert len(buttons) == (2 if page_name.startswith("register") else 1)
    assert all("disabled" in button for button in buttons)


@pytest.mark.parametrize("page_name", ["login", "register", "reset", "redeem"])
def test_auth_first_html_contains_final_layout_without_javascript(page_name):
    from tradingagents.workbench.email_server import _auth_page, _login_page, _register_page

    elements = []
    parents = []

    class AuthLayout(HTMLParser):
        def handle_starttag(self, tag, attrs):
            element = {"tag": tag, **dict(attrs)}
            elements.append((element, parents[-1] if parents else {}))
            if tag not in {"meta", "link", "input", "br"}:
                parents.append(element)

        def handle_endtag(self, tag):
            assert parents.pop()["tag"] == tag

    html = (_login_page("fixture-nonce") if page_name == "login"
            else _register_page("fixture-nonce", available=True) if page_name == "register"
            else _auth_page(page_name, f"/auth/{page_name}", "fixture-nonce"))
    AuthLayout().feed(html)
    layout_children = [element.get("class") for element, parent in elements if parent.get("class") == "auth-layout"]
    assert layout_children == ["auth-header", "auth-main", "auth-footer", "auth-art"]
    assert sum(element.get("id") == "auth-scene" for element, _ in elements) == 1
    assert any(element.get("class") == "scene-fallback lens-fallback" for element, _ in elements)
    root = next(element for element, _ in elements if element["tag"] == "html")
    assert root["data-screen"] == "login"
    assert root["data-auth"] == ("register" if page_name == "register" else "login")
    if page_name == "register":
        button, parent = next(pair for pair in elements if pair[0].get("id") == "register-send-code")
        assert parent["class"] == "auth-code-row"
        assert button["form"] == "email-register-code-form"


@pytest.fixture
def app_case():
    backend = MemoryBackend()
    store = EncryptedStore(backend, b"e" * 32)
    clock = [NOW]
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: clock[0])
    owner = auth.bootstrap(OWNER_PASSWORD)
    invite = auth.invite(owner.session_id, "guest@example.com")
    guest = auth.redeem(invite.token, invite.email, GUEST_PASSWORD)
    settings = UserSettings(store, approved_origins=("https://relay.example.com",))
    settings.update(owner.principal, {
        "mode": "api_key",
        "provider": "openai",
        "quick_model": "gpt-5",
        "deep_model": "gpt-5-mini",
        "api_key": "fixture-openai-key",
        "data_keys": {"fred": "fixture-fred-key"},
    }, expected_version=None)
    settings.update(guest.principal, {
        "mode": "api_key",
        "provider": "openai",
        "quick_model": "gpt-5",
        "deep_model": "gpt-5-mini",
        "api_key": "fixture-guest-openai-key",
    }, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: clock[0])
    upstream = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=object(),
        installation={"foundry": None},
        http_client=upstream,
        clock=lambda: clock[0],
    )
    wire = ArmWire()
    execution = HostedExecution(
        store,
        gateway=gateway,
        installation=installation(),
        credential=HostCredential(),
        client=httpx.Client(transport=httpx.MockTransport(wire)),
        clock=lambda: clock[0],
    )
    runs = HostedRuns(auth=auth, settings=settings, execution=execution, clock=lambda: clock[0])

    from tradingagents.workbench.email_server import create_email_app

    app = create_email_app(
        auth=auth,
        settings=settings,
        budget=budget,
        runs=runs,
        gateway=gateway,
    )
    return SimpleNamespace(
        app=app,
        owner=owner,
        guest=guest,
        auth=auth,
        settings=settings,
        budget=budget,
        gateway=gateway,
        execution=execution,
        runs=runs,
        wire=wire,
        clock=clock,
        store=store,
        upstream=upstream,
    )


@pytest.fixture
def foundry_app_case():
    backend = MemoryBackend()
    store = EncryptedStore(backend, b"f" * 32)
    auth = EmailAuth(store, origin=ORIGIN, clock=lambda: NOW)
    owner = auth.bootstrap(OWNER_PASSWORD)
    settings = UserSettings(store)
    settings.update(owner.principal, {"mode": "managed_identity"}, expected_version=None)
    budget = BudgetLedger(store, clock=lambda: NOW)
    gateway = ProviderGateway(
        store,
        settings=settings,
        budget=budget,
        managed_credential=object(),
        installation=foundry_installation(
            input_rate=40_000_000_000_000,
            cached_input_rate=40_000_000_000_000,
            output_rate=40_000_000_000_000,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))),
        clock=lambda: NOW,
    )
    execution = HostedExecution(
        store,
        gateway=gateway,
        installation=installation(),
        credential=HostCredential(),
        client=httpx.Client(transport=httpx.MockTransport(ArmWire())),
        clock=lambda: NOW,
    )
    runs = HostedRuns(auth=auth, settings=settings, execution=execution, clock=lambda: NOW)

    from tradingagents.workbench.email_server import create_email_app

    return SimpleNamespace(
        app=create_email_app(auth=auth, settings=settings, budget=budget, runs=runs, gateway=gateway),
        owner=owner,
        gateway=gateway,
    )


def login(client, email, password):
    response = client.post(
        "/auth/login",
        json={"email": email, "password": password},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    return response.json()


def _worker_backend_bridge(client):
    def handler(request: httpx.Request) -> httpx.Response:
        response = client.post(
            request.url.path,
            headers={
                "x-tawb-run-capability": request.headers["x-tawb-run-capability"],
                "content-type": request.headers["content-type"],
                "accept": request.headers["accept"],
                "accept-encoding": request.headers["accept-encoding"],
            },
            content=request.content,
        )
        return httpx.Response(response.status_code, headers=dict(response.headers), content=response.content)

    return httpx.Client(transport=httpx.MockTransport(handler))


class BlockingGateway:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def dispatch(self, capability, *, method, path, headers, body):
        del capability, method, path, headers, body
        self.entered.set()
        assert self.release.wait(5)
        return {"status_code": 200, "body": {"ok": True}}


class FakeRegistration:
    available = True

    def __init__(self, session):
        self.session = session
        self.code_requests = []
        self.register_requests = []

    def request_code(self, email, *, source):
        self.code_requests.append((email, source))
        return {"accepted": True, "expires_in": 600, "retry_after": 60}

    def register(self, email, code, password, *, source):
        self.register_requests.append((email, code, password, source))
        if code != "123456":
            raise AccountError(status_code=401)
        return self.session


def test_email_login_uses_html_and_email_cookie_not_entra_redirect(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        assert client.get("/api/me").status_code == 401

        login_page = client.get("/auth/login")
        assert login_page.status_code == 200
        assert "login.microsoftonline.com" not in login_page.text

        response = client.post(
            "/auth/login",
            json={"email": app_case.owner.principal.email, "password": OWNER_PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert "__Host-tawb-email-session=" in response.headers["set-cookie"]

        me = client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["user"] == {
            "object_id": app_case.owner.principal.object_id,
            "email": app_case.owner.principal.email,
            "name": app_case.owner.principal.name,
            "is_admin": True,
        }
        assert type(me.json()["csrf_token"]) is str


def test_research_assistant_session_routes_allow_users_and_remain_csrf_protected(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        created = client.post("/api/assistant/sessions", json={}, headers=headers)
        assert created.status_code == 201
        session_id = created.json()["id"]
        assert created.json()["title"] == "新对话"
        listed = client.get("/api/assistant/sessions")
        assert listed.status_code == 200
        assert listed.json()["sessions"][0]["id"] == session_id
        assert client.get("/api/assistant/sessions/not-a-uuid").status_code == 404
        assert client.post("/api/assistant/sessions", json={"extra": True}, headers=headers).status_code == 400
        assert client.post(
            "/api/assistant/sessions", json={}, headers={**headers, "Origin": "https://attacker.example"},
        ).status_code == 403
        renamed = client.patch(
            f"/api/assistant/sessions/{session_id}", json={"title": "MSFT 历史"}, headers=headers,
        )
        assert renamed.status_code == 200
        assert client.get(f"/api/assistant/sessions/{session_id}").json()["title"] == "MSFT 历史"
        assert client.delete(f"/api/assistant/sessions/{session_id}", headers=headers).status_code == 200
        assert client.get("/api/assistant/sessions").json() == {"sessions": []}

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as guest_client:
        guest = login(guest_client, app_case.guest.principal.email, GUEST_PASSWORD)
        assert guest_client.get("/api/assistant/sessions").json() == {"sessions": []}
        assert guest_client.post("/api/assistant/sessions", json={}, headers={"Origin": ORIGIN}).status_code == 403
        assert guest_client.post("/api/assistant/sessions", json={}, headers={
            "Origin": ORIGIN, "X-Workbench-Token": guest["csrf_token"],
        }).status_code == 201


def test_research_assistant_message_is_idempotent_and_never_submits_a_research_run(app_case):
    from tradingagents.workbench.email_server import create_email_app

    class AssistantGateway:
        def __init__(self):
            self.calls = []

        def managed_identity_available(self):
            return True

        def assistant_reply(self, principal, conversation_id, messages, *, context):
            self.calls.append((principal, conversation_id, messages, context))
            return "根据运行记录，MSFT 报告提到了市场上涨。"

    gateway = AssistantGateway()
    app = create_email_app(
        auth=app_case.auth,
        settings=app_case.settings,
        budget=app_case.budget,
        runs=app_case.runs,
        gateway=gateway,
    )
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        session_id = client.post("/api/assistant/sessions", json={}, headers=headers).json()["id"]
        history_before = client.get("/api/runs").json()
        turn_id = str(uuid4())
        payload = {"turn_id": turn_id, "content": "What did MSFT history say?"}
        response = client.post(f"/api/assistant/sessions/{session_id}/messages", json=payload, headers=headers)
        assert response.status_code == 200
        repeated = client.post(f"/api/assistant/sessions/{session_id}/messages", json=payload, headers=headers)
        assert repeated.json() == response.json()
        missing_csrf = client.post(
            f"/api/assistant/sessions/{session_id}/messages",
            json={"turn_id": str(uuid4()), "content": "hi"},
            headers={"Origin": ORIGIN},
        )
        history_after = client.get("/api/runs").json()

    assert response.json()["answer"] == "根据运行记录，MSFT 报告提到了市场上涨。"
    assert len(gateway.calls) == 1
    assert history_after == history_before
    assert missing_csrf.status_code == 403


def test_compatible_domain_accepts_matching_host_and_origin_only(app_case):
    from tradingagents.workbench.email_server import create_email_app

    app = create_email_app(
        auth=app_case.auth,
        settings=app_case.settings,
        budget=app_case.budget,
        runs=app_case.runs,
        gateway=app_case.gateway,
        compatible_origins=("https://research.example.com",),
    )
    with TestClient(app, base_url="https://research.example.com", follow_redirects=False) as client:
        login_response = client.post(
            "/auth/login",
            json={"email": app_case.owner.principal.email, "password": OWNER_PASSWORD},
            headers={"Origin": "https://research.example.com"},
        )
        assert login_response.status_code == 200
        assert client.get("/api/me").status_code == 200

        mismatched_origin = client.post(
            "/auth/login",
            json={"email": app_case.owner.principal.email, "password": OWNER_PASSWORD},
            headers={"Origin": ORIGIN},
        )

    assert mismatched_origin.status_code == 403


def test_email_auth_pages_use_studio_login_shell_without_changing_auth_contract(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        login_page = client.get("/auth/login")
        redeem_page = client.get("/auth/redeem")
        reset_page = client.get("/auth/reset")

        assert login_page.status_code == 200
        assert redeem_page.status_code == 200
        assert reset_page.status_code == 200

        assert '<body class="studio-auth auth-shell">' in login_page.text
        assert '<div class="auth-backdrop" aria-hidden="true"><span id="login-grid-hover"' in login_page.text
        assert '<header class="auth-header">' in login_page.text
        assert 'data-studio-tools' in login_page.text
        assert '<main class="auth-main">' in login_page.text
        assert '<div class="auth-identity">' in login_page.text
        assert '<footer class="auth-footer">' in login_page.text
        assert '<link rel="stylesheet" href="/static/auth-studio.css">' in login_page.text
        assert '<script type="module" src="/static/account-ui.js"></script>' in login_page.text
        assert '<script type="module" nonce="' in login_page.text
        assert 'src="/static/auth-studio.js"' in login_page.text
        assert '<a href="/auth/register">注册账户</a>' in login_page.text
        assert 'href="/auth/reset"' in login_page.text
        assert '演示' not in login_page.text

        assert 'id="email-login-form"' in login_page.text
        assert 'method="post" action="/auth/login"' in login_page.text
        assert 'id="email" name="email" type="email" autocomplete="username email"' in login_page.text
        assert 'id="password" name="password" type="password" autocomplete="current-password"' in login_page.text
        assert 'id="email-login-submit"' in login_page.text

        for page, action in ((redeem_page, "redeem"), (reset_page, "reset")):
            assert 'class="studio-auth auth-shell"' in page.text
            assert 'data-auth="login"' in page.text
            assert f'data-auth-route="{action}"' in page.text
            assert 'data-studio-tools' in page.text
            assert f'id="email-{action}-form"' in page.text
            assert f'method="post" action="/auth/{action}"' in page.text
            assert 'id="auth-fragment-status"' in page.text
            assert 'autocomplete="new-password"' in page.text
            assert 'href="/auth/login"' in page.text

        assert client.get("/static/auth-studio.css").status_code == 200
        assert client.get("/static/auth-studio.js").status_code == 200


def test_research_assistant_module_is_served_only_from_the_static_allowlist(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        module = client.get("/static/research-assistant.mjs")
        unknown = client.get("/static/assistant-secret.js")

    assert module.status_code == 200
    assert module.headers["content-type"].startswith("text/javascript")
    assert "mountResearchAssistant" in module.text
    assert unknown.status_code == 404


def test_email_registration_page_defaults_to_unavailable_without_bootstrap_or_invite(app_case):
    from tradingagents.workbench.email_server import create_email_app

    app = create_email_app(
        auth=app_case.auth,
        settings=app_case.settings,
        budget=app_case.budget,
        runs=app_case.runs,
        gateway=app_case.gateway,
    )
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        page = client.get("/auth/register")
        code = client.post("/auth/register/code", json={"email": "new@example.com"}, headers={"Origin": ORIGIN})

    assert page.status_code == 200
    assert 'id="email-register-form"' in page.text
    assert 'id="email-register-code-form"' in page.text
    assert 'id="register-send-code"' in page.text
    assert 'data-registration-available="false"' in page.text
    assert code.status_code == 503
    assert code.json()["code"] == "registration_unavailable"


def test_email_registration_routes_enforce_exact_json_origin_and_issue_email_session(app_case):
    from tradingagents.workbench.email_server import create_email_app

    registration = FakeRegistration(app_case.owner)
    app = create_email_app(
        auth=app_case.auth,
        settings=app_case.settings,
        budget=app_case.budget,
        runs=app_case.runs,
        gateway=app_case.gateway,
        registration=registration,
    )
    with TestClient(app, base_url=ORIGIN, follow_redirects=False, client=("198.51.100.30", 57000)) as client:
        bad_origin = client.post(
            "/auth/register/code",
            json={"email": "new@example.com"},
            headers={"Origin": "https://attacker.example"},
        )
        bad_payload = client.post(
            "/auth/register/code",
            json={"email": "new@example.com", "extra": True},
            headers={"Origin": ORIGIN},
        )
        accepted = client.post(
            "/auth/register/code",
            json={"email": "new@example.com"},
            headers={"Origin": ORIGIN},
        )
        wrong_code = client.post(
            "/auth/register",
            json={"email": "new@example.com", "code": "000000", "password": GUEST_PASSWORD},
            headers={"Origin": ORIGIN},
        )
        registered = client.post(
            "/auth/register",
            json={"email": "new@example.com", "code": "123456", "password": GUEST_PASSWORD},
            headers={"Origin": ORIGIN},
        )
        me = client.get("/api/me")

    assert bad_origin.status_code == 403
    assert bad_payload.status_code == 400
    assert accepted.status_code == 200
    assert accepted.json() == {"accepted": True, "expires_in": 600, "retry_after": 60}
    assert wrong_code.status_code == 401
    assert "__Host-tawb-email-session=" not in wrong_code.headers.get("set-cookie", "")
    assert registered.status_code == 200
    assert registered.json()["user"]["email"] == app_case.owner.principal.email
    assert "__Host-tawb-email-session=" in registered.headers["set-cookie"]
    assert me.status_code == 200
    assert registration.code_requests == [("new@example.com", "browser-login:198.51.100.30")]
    assert registration.register_requests == [
        ("new@example.com", "000000", GUEST_PASSWORD, "browser-login:198.51.100.30"),
        ("new@example.com", "123456", GUEST_PASSWORD, "browser-login:198.51.100.30"),
    ]


def test_email_registration_routes_use_actual_service_for_wrong_expired_and_replayed_codes(app_case):
    from tradingagents.workbench.email_server import create_email_app
    from tradingagents.workbench.public_registration import EmailRegistration

    sent = []
    registration = EmailRegistration(app_case.auth, sender=lambda email, code: sent.append((email, code)))
    app = create_email_app(
        auth=app_case.auth,
        settings=app_case.settings,
        budget=app_case.budget,
        runs=app_case.runs,
        gateway=app_case.gateway,
        registration=registration,
    )
    with TestClient(app, base_url=ORIGIN, follow_redirects=False, client=("198.51.100.40", 58000)) as client:
        first_send = client.post("/auth/register/code", json={"email": "new@example.com"}, headers={"Origin": ORIGIN})
        wrong = client.post(
            "/auth/register",
            json={"email": "new@example.com", "code": "000000", "password": GUEST_PASSWORD},
            headers={"Origin": ORIGIN},
        )
        correct = client.post(
            "/auth/register",
            json={"email": "new@example.com", "code": sent[-1][1], "password": GUEST_PASSWORD},
            headers={"Origin": ORIGIN},
        )
        replayed = client.post(
            "/auth/register",
            json={"email": "new@example.com", "code": sent[-1][1], "password": GUEST_PASSWORD},
            headers={"Origin": ORIGIN},
        )

    assert first_send.status_code == 200
    assert first_send.json() == {"accepted": True, "expires_in": 600, "retry_after": 60}
    assert sent[0][0] == "new@example.com"
    assert wrong.status_code == 401
    assert correct.status_code == 200
    assert correct.json()["user"]["email"] == "new@example.com"
    assert "__Host-tawb-email-session=" in correct.headers["set-cookie"]
    assert replayed.status_code in {401, 409}
    assert "__Host-tawb-email-session=" not in replayed.headers.get("set-cookie", "")

    app_case.clock[0] += 601
    sent.clear()
    with TestClient(app, base_url=ORIGIN, follow_redirects=False, client=("198.51.100.41", 58001)) as client:
        expired_send = client.post("/auth/register/code", json={"email": "expired@example.com"}, headers={"Origin": ORIGIN})
        app_case.clock[0] += 601
        expired = client.post(
            "/auth/register",
            json={"email": "expired@example.com", "code": sent[-1][1], "password": GUEST_PASSWORD},
            headers={"Origin": ORIGIN},
        )

    assert expired_send.status_code == 200
    assert expired.status_code == 401
    assert "__Host-tawb-email-session=" not in expired.headers.get("set-cookie", "")


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_public_static_delivery_compresses_and_revalidates_without_caching_private_data(app_case, encoding):
    from tradingagents.workbench.public_server import STATIC

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        asset = client.get("/static/vendor.js", headers={"Accept-Encoding": encoding})
        assert asset.status_code == 200
        assert asset.content == (STATIC / "vendor.js").read_bytes()
        assert asset.headers["cache-control"] == "public, no-cache"
        assert asset.headers["vary"] == "Accept-Encoding"
        assert asset.headers["etag"].startswith('W/"')
        if encoding == "gzip":
            assert asset.headers["content-encoding"] == "gzip"
            assert int(asset.headers["content-length"]) < len(asset.content) / 2
        else:
            assert "content-encoding" not in asset.headers
        cached = client.get("/static/vendor.js", headers={
            "Accept-Encoding": encoding, "If-None-Match": asset.headers["etag"],
        })
        assert cached.status_code == 304
        assert cached.content == b""
        assert cached.headers["cache-control"] == "public, no-cache"
        assert cached.headers["etag"] == asset.headers["etag"]
        assert cached.headers["vary"] == "Accept-Encoding"
        changed = client.get("/static/vendor.js", headers={"If-None-Match": 'W/"old-version"'})
        assert changed.status_code == 200
        for path in ("/auth/login", "/api/session", "/static/not-allowed.js", "/static/index.html"):
            response = client.get(path, headers={"Accept-Encoding": "gzip"})
            assert response.headers["cache-control"] == "no-store"
            assert "content-encoding" not in response.headers
        login_page = client.get("/auth/login")
        assert '<link rel="modulepreload" href="/static/vendor.js">' in login_page.text


def test_email_index_requires_auth_then_serves_static_workbench_and_exact_allowlist(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        anonymous = client.get("/")
        assert anonymous.status_code == 303
        assert anonymous.headers["location"] == "/auth/login"

        login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        index = client.get("/")
        assert index.status_code == 200
        assert "/static/boot.js" in index.text
        assert '<link rel="modulepreload" href="/static/vendor.js">' in index.text
        assert index.headers["cache-control"] == "no-store"
        assert "nonce=" in index.text
        assert index.headers["content-security-policy"].count("script-src") == 1
        assert "nonce-" in index.headers["content-security-policy"]

        styles = client.get("/static/styles.css")
        assert styles.status_code == 200
        assert "text/css" in styles.headers["content-type"]

        boot = client.get("/static/boot.js")
        assert boot.status_code == 200
        assert "javascript" in boot.headers["content-type"]

        assert client.get("/static/account-ui.js").status_code == 200
        assert client.get("/static/model-settings.js").status_code == 200
        assert client.get("/static/not-allowed.js").status_code == 404


def test_email_routes_enforce_origin_csrf_roles_and_foundry_availability(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        guest = login(client, app_case.guest.principal.email, GUEST_PASSWORD)
        token = guest["csrf_token"]
        assert client.post("/auth/logout", headers={"X-Workbench-Token": token}).status_code == 403
        assert client.post(
            "/auth/logout",
            headers={"Origin": "https://attacker.example", "X-Workbench-Token": token},
        ).status_code == 403
        assert client.get("/api/admin/users").status_code == 403
        update = client.put(
            "/api/settings",
            json={"mode": "managed_identity"},
            headers={"Origin": ORIGIN, "X-Workbench-Token": token},
        )
        assert update.status_code == 409
        assert client.get("/auth/callback").status_code == 404


def test_persisted_unavailable_managed_identity_run_create_rejects_before_mutation_and_byok_still_runs(app_case):
    owner = app_case.owner.principal
    current = app_case.settings.get(owner)
    app_case.settings.update(owner, {"mode": "managed_identity"}, expected_version=current["version"])
    runs_key = owner.owner_key + "/hosted-runs/index"

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        session = login(client, owner.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": session["csrf_token"]}
        before_budget = app_case.budget.snapshot()
        rejected = client.post(
            "/api/runs",
            headers=headers,
            json={"submission_id": str(uuid4()), "request": default_request()},
        )
        after_rejected_runs = app_case.store.read(runs_key)
        after_rejected_wire_calls = list(app_case.wire.calls)
        after_rejected_budget = app_case.budget.snapshot()

        current = client.get("/api/settings")
        restored = client.put(
            "/api/settings",
            json={
                "mode": "api_key",
                "provider": "openai",
                "quick_model": "gpt-5",
                "deep_model": "gpt-5-mini",
                "api_key": "fixture-openai-key",
                "data_keys": {"fred": "fixture-fred-key"},
            },
            headers={**headers, "If-Match": current.json()["version"]},
        )
        accepted = client.post(
            "/api/runs",
            headers=headers,
            json={"submission_id": str(uuid4()), "request": default_request()},
        )

    assert rejected.status_code == 409
    assert after_rejected_runs is None
    assert after_rejected_wire_calls == []
    assert after_rejected_budget == before_budget
    assert restored.status_code == 200
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "starting"
    assert app_case.wire.calls == []
    app_case.runs.advance_pending()
    assert app_case.runs.get(app_case.owner.session_id, accepted.json()["id"])["status"] == "running"
    assert len(app_case.wire.calls) >= 2


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/auth/login", {"email": "owner@example.com", "password": OWNER_PASSWORD}),
        ("/auth/redeem", {"token": "fixture-token", "email": "user@example.com", "password": GUEST_PASSWORD}),
        ("/auth/reset", {"token": "fixture-token", "email": "user@example.com", "password": GUEST_PASSWORD}),
        ("/auth/register/code", {"email": "user@example.com"}),
        ("/auth/register", {"email": "user@example.com", "code": "123456", "password": GUEST_PASSWORD}),
    ],
)
@pytest.mark.parametrize(
    ("headers", "status_code"),
    [
        ({"content-type": "application/json"}, 403),
        ({"origin": "https://attacker.example", "content-type": "application/json"}, 403),
        ([
            ("origin", ORIGIN),
            ("origin", "https://attacker.example"),
            ("content-type", "application/json"),
        ], 403),
        ({"origin": "null", "content-type": "application/json"}, 403),
        ({"origin": ORIGIN, "content-type": "text/plain"}, 415),
    ],
)
def test_public_auth_writes_require_exact_origin_and_json_mime_without_mutating_auth_state(
    app_case, path, payload, headers, status_code,
):
    before_admission = app_case.store.read(ADMISSION_KEY)
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        response = client.post(path, headers=headers, content=json.dumps(payload).encode("utf-8"))

    assert response.status_code == status_code
    assert "__Host-tawb-email-session=" not in response.headers.get("set-cookie", "")
    assert app_case.store.read(ADMISSION_KEY) == before_admission
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_invalid_json_bodies_fail_closed_without_mutating_auth_settings_or_dispatch(app_case):
    owner_settings = app_case.settings.get(app_case.owner.principal)
    upstream_requests = []
    original_http = app_case.gateway._http
    run_id = str(uuid4())
    capability = app_case.gateway.authorize(
        run_id,
        app_case.owner.principal,
        app_case.settings.pin(app_case.owner.principal, run_id),
        expires_at=NOW + 3600,
    ).serialize()

    def respond(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream_client:
        app_case.gateway._http = upstream_client
        with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
            bad_login = client.post(
                "/auth/login",
                headers={"Origin": ORIGIN, "content-type": "application/json"},
                content=b'{"email":"a@example.com","email":"b@example.com","password":"fixture-password-2026"}',
            )
            bad_settings = client.put(
                "/api/settings",
                headers={"Origin": ORIGIN, "X-Workbench-Token": login(client, app_case.owner.principal.email, OWNER_PASSWORD)["csrf_token"], "content-type": "application/json"},
                content=(b'{"mode":"api_key","pad":' + (b'{"pad":' * 65) + b'{}' + (b'}' * 65) + b'}'),
            )
            bad_worker = client.post(
                "/api/internal/worker/provider-dispatch",
                headers={"x-tawb-run-capability": capability, "content-type": "application/json"},
                content=b'{"method":"POST","path":"/responses","headers":{},"body":{"input":NaN}}',
            )
        app_case.gateway._http = original_http

    assert bad_login.status_code == 400
    assert bad_settings.status_code == 400
    assert bad_worker.status_code == 400
    assert "__Host-tawb-email-session=" not in bad_login.headers.get("set-cookie", "")
    assert app_case.settings.get(app_case.owner.principal) == owner_settings
    assert upstream_requests == []


def test_admin_invite_and_recovery_fragments_urlencode_valid_email_characters(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        tricky_email = "plus&alias+tag@example.com"
        invite = client.post(
            "/api/admin/invitations",
            json={"email": tricky_email},
            headers=headers,
        )
        users = client.get("/api/admin/users").json()["users"]
        guest_id = next(row["object_id"] for row in users if row["email"] == app_case.guest.principal.email)
        recovery = client.post(
            "/api/admin/recoveries",
            json={"user_id": guest_id},
            headers=headers,
        )

    invite_fragment = dict(parse_qsl(urlsplit(invite.json()["fragment"]).fragment, keep_blank_values=True, strict_parsing=True))
    recovery_fragment = dict(parse_qsl(urlsplit(recovery.json()["fragment"]).fragment, keep_blank_values=True, strict_parsing=True))
    assert invite_fragment["email"] == tricky_email
    assert recovery_fragment["email"] == app_case.guest.principal.email


def test_login_uses_peer_scoped_admission_buckets_and_ignores_forwarded_headers(app_case):
    for attempt in range(30):
        with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False, client=("198.51.100.10", 50000 + attempt)) as client:
            response = client.post(
                "/auth/login",
                json={"email": f"unknown-{attempt}@example.com", "password": "fixture-wrong-password"},
                headers={"Origin": ORIGIN, "X-Forwarded-For": f"203.0.113.{attempt}"},
            )
            assert response.status_code == 401

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False, client=("198.51.100.10", 54000)) as blocked_client:
        blocked = blocked_client.post(
            "/auth/login",
            json={"email": app_case.owner.principal.email, "password": OWNER_PASSWORD},
            headers={"Origin": ORIGIN, "X-Forwarded-For": "203.0.113.250"},
        )
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False, client=("198.51.100.11", 54001)) as fresh_client:
        fresh = fresh_client.post(
            "/auth/login",
            json={"email": app_case.owner.principal.email, "password": OWNER_PASSWORD},
            headers={"Origin": ORIGIN},
        )

    admission = app_case.store.read(ADMISSION_KEY)[0]
    assert blocked.status_code == 429
    assert fresh.status_code == 200
    assert "198.51.100.10" not in repr(admission)
    assert "203.0.113.250" not in repr(admission)


def test_missing_peer_uses_fail_closed_bucket(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False, client=(None, 0)) as client:
        for attempt in range(30):
            response = client.post(
                "/auth/login",
                json={"email": f"missing-peer-{attempt}@example.com", "password": "fixture-wrong-password"},
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 401
        blocked = client.post(
            "/auth/login",
            json={"email": app_case.owner.principal.email, "password": OWNER_PASSWORD},
            headers={"Origin": ORIGIN},
        )
    assert blocked.status_code == 429


def test_blocked_worker_dispatch_does_not_block_health_or_login(app_case):
    blocking_gateway = BlockingGateway()
    app_case.app.dependency_overrides = {}
    from tradingagents.workbench import email_server as email_server_module

    app = email_server_module.create_email_app(
        auth=app_case.auth,
        settings=app_case.settings,
        budget=app_case.budget,
        runs=app_case.runs,
        gateway=blocking_gateway,
    )
    capability = "11111111-1111-4111-8111-111111111111." + ("a" * 43)

    def worker_call():
        with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
            return client.post(
                "/api/internal/worker/provider-dispatch",
                headers={"x-tawb-run-capability": capability, "content-type": "application/json"},
                json={"method": "POST", "path": "/responses", "headers": {}, "body": {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4}},
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker_call)
        assert blocking_gateway.entered.wait(5)
        with TestClient(app, base_url=ORIGIN, follow_redirects=False, client=("198.51.100.20", 55000)) as client:
            health = client.get("/healthz")
            login_response = client.post(
                "/auth/login",
                json={"email": app_case.owner.principal.email, "password": OWNER_PASSWORD},
                headers={"Origin": ORIGIN},
            )
        blocking_gateway.release.set()
        worker_response = future.result(timeout=5)

    assert health.status_code == 200
    assert login_response.status_code == 200
    assert worker_response.status_code == 200


@pytest.mark.parametrize("is_owner", [True, False])
@pytest.mark.parametrize("available", [True, False])
def test_catalog_exposes_managed_identity_configuration_only_to_owner(app_case, is_owner, available):
    from tradingagents.workbench.email_server import create_email_app

    configuration = {
        "deployment": "gpt-5.6-sol",
        "endpoint": "https://fixture-foundry.services.ai.azure.com/",
        "model_family": "gpt-5.6",
        "model_version": "2026-07-09",
    } if available else None
    configurations = [configuration] if configuration is not None else []

    class CatalogGateway:
        def managed_identity_available(self):
            return available

        def assistant_available(self):
            return available

        def managed_identity_configuration(self):
            assert is_owner, "Guest catalog must not read installation configuration."
            return configuration

        def managed_identity_configurations(self):
            assert is_owner, "Guest catalog must not list installation configuration."
            return configurations

    app = create_email_app(
        auth=app_case.auth,
        settings=app_case.settings,
        budget=app_case.budget,
        runs=app_case.runs,
        gateway=CatalogGateway(),
    )
    session = app_case.owner if is_owner else app_case.guest
    password = OWNER_PASSWORD if is_owner else GUEST_PASSWORD
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        login(client, session.principal.email, password)
        response = client.get("/api/settings/catalog")

    assert response.status_code == 200
    assert response.json()["assistant_available"] is available
    assert response.json()["modes"]["managed_identity"] == {
        "admin_only": True,
        "available": is_owner and available,
        "configuration": configuration if is_owner else None,
        "configurations": configurations if is_owner else [],
    }


def test_invalid_managed_model_selection_is_rejected_without_changing_version(app_case):
    from tradingagents.workbench.email_server import create_email_app
    from tradingagents.workbench.public_gateway import GatewayError

    class SelectionGateway:
        def managed_identity_available(self):
            return True

        def managed_identity_configuration(self):
            return {"deployment": "gpt-5.6-sol", "endpoint": "https://fixture-foundry.services.ai.azure.com/", "model_family": "gpt-5.6", "model_version": "2026-07-09"}

        def managed_identity_configurations(self):
            return [self.managed_identity_configuration()]

        def validate_managed_models(self, models):
            if models != {"quick_model": "missing", "deep_model": "missing"}:
                return
            raise GatewayError("Selected managed model is unavailable.", status_code=409)

    app = create_email_app(
        auth=app_case.auth,
        settings=app_case.settings,
        budget=app_case.budget,
        runs=app_case.runs,
        gateway=SelectionGateway(),
    )
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        before = client.get("/api/settings")
        invalid = client.put(
            "/api/settings",
            json={"managed_models": {"quick_model": "missing", "deep_model": "missing"}},
            headers={"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"], "If-Match": before.json()["version"]},
        )
        after = client.get("/api/settings")

    assert invalid.status_code == 409
    assert before.json()["version"] == after.json()["version"]

    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as guest_client:
        guest = login(guest_client, app_case.guest.principal.email, GUEST_PASSWORD)
        denied = guest_client.put(
            "/api/settings",
            json={"managed_models": {"quick_model": "gpt-5.6-sol", "deep_model": "gpt-5.6-sol"}},
            headers={"Origin": ORIGIN, "X-Workbench-Token": guest["csrf_token"], "If-Match": guest_client.get("/api/settings").json()["version"]},
        )
    assert denied.status_code == 403


def test_foundry_enabled_catalog_exposes_managed_identity_and_allows_owner_selection(foundry_app_case):
    with TestClient(foundry_app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, foundry_app_case.owner.principal.email, OWNER_PASSWORD)
        catalog = client.get("/api/settings/catalog")
        current = client.get("/api/settings")
        update = client.put(
            "/api/settings",
            json={"mode": "managed_identity"},
            headers={
                "Origin": ORIGIN,
                "X-Workbench-Token": owner["csrf_token"],
                "If-Match": current.json()["version"],
            },
        )

    assert catalog.status_code == 200
    assert catalog.json()["modes"]["managed_identity"]["available"] is True
    assert update.status_code == 200
    assert update.json()["mode"] == "managed_identity"


def test_old_entra_cookie_is_not_accepted_by_email_http_routes(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        client.cookies.set("__Host-tawb-session", "legacy-entra-cookie", domain="testserver.local", path="/")
        response = client.get("/api/me")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_http_password_reset_revokes_prior_session_and_disable_revokes_guest_session(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as admin_client:
        owner = login(admin_client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        guest_id = app_case.guest.principal.object_id
        recovery = admin_client.post(
            "/api/admin/recoveries",
            json={"user_id": guest_id},
            headers=headers,
        ).json()["fragment"]

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as stale_guest_client:
        stale_guest = login(stale_guest_client, app_case.guest.principal.email, GUEST_PASSWORD)
        reset_token = dict(parse_qsl(urlsplit(recovery).fragment, keep_blank_values=True, strict_parsing=True))["token"]
        with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as reset_client:
            reset = reset_client.post(
                "/auth/reset",
                json={"token": reset_token, "email": app_case.guest.principal.email, "password": "fixture-reset-password-2026"},
                headers={"Origin": ORIGIN},
            )
        stale_after_reset = stale_guest_client.get("/api/me")

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as fresh_guest_client:
        fresh_guest = login(fresh_guest_client, app_case.guest.principal.email, "fixture-reset-password-2026")
        with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as admin_client:
            owner = login(admin_client, app_case.owner.principal.email, OWNER_PASSWORD)
            disabled = admin_client.post(
                f"/api/admin/users/{guest_id}/disable",
                headers={"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"],
                         "If-Match": admin_client.get('/api/admin/accounts').json()['version']},
            )
        stale_after_disable = fresh_guest_client.get("/api/me")

    assert stale_guest["user"]["email"] == app_case.guest.principal.email
    assert reset.status_code == 200
    assert stale_after_reset.status_code == 401
    assert fresh_guest["user"]["email"] == app_case.guest.principal.email
    assert disabled.status_code == 200
    assert stale_after_disable.status_code == 401


def test_invalid_host_and_auth_errors_keep_security_headers(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        invalid_host = client.get("/api/me", headers={"host": "attacker.example"})
        unauthenticated = client.get("/api/me")

    assert invalid_host.status_code == 400
    assert invalid_host.headers["x-frame-options"] == "DENY"
    assert invalid_host.headers["x-content-type-options"] == "nosniff"
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["referrer-policy"] == "no-referrer"


def test_email_submission_returns_same_durable_task_before_cloud_work(app_case):
    with TestClient(app_case.app, base_url=ORIGIN) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        payload = {"submission_id": str(uuid4()), "request": {"ticker": "MSFT", "analysis_date": "2026-09-01"}}
        first = client.post("/api/runs", headers=headers, json=payload)
        second = client.post("/api/runs", headers=headers, json=payload)
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["status"] == "starting"
        assert client.get(f"/api/runs/{first.json()['id']}").json()["status"] == "starting"
        assert app_case.wire.calls == []
        app_case.runs.advance_pending()
        assert client.get(f"/api/runs/{first.json()['id']}").json()["status"] == "running"


def test_email_runs_are_owned_per_account_and_use_real_hosted_services(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        created = client.post(
            "/api/runs",
            json={"submission_id": str(uuid4()), "request": {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]}},
            headers=headers,
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        assert client.get(f"/api/runs/{run_id}").status_code == 200
        app_case.runs.advance_pending()
        client.post("/auth/logout", headers=headers)

        login(client, app_case.guest.principal.email, GUEST_PASSWORD)
        assert client.get(f"/api/runs/{run_id}").status_code == 404
        assert client.get("/api/budget").status_code == 403
        assert any(path.endswith("/start") for method, path, body in app_case.wire.calls)


def test_email_stop_cancels_index_only_run_with_empty_json_without_cloud_calls(app_case, monkeypatch):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        with monkeypatch.context() as interrupted:
            interrupted.setattr(app_case.runs, "_begin", lambda *args: None)
            created = client.post("/api/runs", headers=headers, json={
                "submission_id": str(uuid4()),
                "request": {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]},
            })
        assert created.status_code == 202
        run_id = app_case.runs.list(app_case.owner.session_id)[0]["id"]
        root = app_case.owner.principal.owner_key + "/hosted-runs/"
        assert app_case.store.read(root + run_id) is None
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "starting"
        path = f"/api/runs/{run_id}/stop"
        assert client.post(path, headers=headers).status_code == 415
        assert client.post(path, json={}).status_code == 403
        assert client.post(path, headers={"Origin": ORIGIN}, json={}).status_code == 403
        assert client.post(path, headers=headers, json={"request": {}}).status_code == 400
        with TestClient(app_case.app, base_url=ORIGIN) as guest_client:
            guest = login(guest_client, app_case.guest.principal.email, GUEST_PASSWORD)
            assert guest_client.post(path, json={}, headers={
                "Origin": ORIGIN, "X-Workbench-Token": guest["csrf_token"],
            }).status_code == 404
        stopped = client.post(path, headers=headers, json={})
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"
        assert client.post(path, headers=headers, json={}).json()["status"] == "stopped"
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "stopped"
        record = app_case.store.read(root + run_id)[0]
        assert record["stop_requested"] and not record["prepare_attempted"]
        assert not record["start_pending"] and not record["stop_pending"]
        assert app_case.store.read("email-v1/executions/" + run_id) is None
        assert app_case.wire.calls == []


def test_email_resume_requires_explicit_owned_csrf_request_after_preparation(app_case):
    app_case.wire.fault = "accepted_empty"
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        created = client.post("/api/runs", headers=headers, json={
            "submission_id": str(uuid4()),
            "request": {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]},
        })
        assert created.status_code == 202
        run_id = created.json()["id"]
        path = f"/api/runs/{run_id}/resume"
        app_case.runs.advance_pending()
        assert client.post(f"/api/runs/{run_id}/reconcile", headers=headers, json={}).json()["status"] == "prepared"
        assert client.post(path, json={}).status_code == 403
        assert client.post(path, headers=headers, json={"request": {}}).status_code == 400
        assert not any(route.endswith("/start") for method, route, body in app_case.wire.calls)
        client.post("/auth/logout", headers=headers)
        assert client.post(path, headers=headers, json={}).status_code == 401
        guest = login(client, app_case.guest.principal.email, GUEST_PASSWORD)
        guest_headers = {"Origin": ORIGIN, "X-Workbench-Token": guest["csrf_token"]}
        assert client.post(path, headers=guest_headers, json={}).status_code == 404
        client.post("/auth/logout", headers=guest_headers)
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers["X-Workbench-Token"] = owner["csrf_token"]
        assert client.post(path, headers=headers, json={}).json()["status"] == "running"
        assert client.post(path, headers=headers, json={}).json()["status"] == "running"
        assert sum(route.endswith("/start") for method, route, body in app_case.wire.calls) == 1


def test_worker_dispatch_is_capability_only_and_maps_budget_402_to_budget_exhausted(foundry_app_case):
    run_id = str(uuid4())
    capability = foundry_app_case.gateway.authorize(
        run_id,
        foundry_app_case.owner.principal,
        foundry_app_case.gateway._settings.pin(foundry_app_case.owner.principal, run_id),
        expires_at=NOW + 3600,
    ).serialize()
    with TestClient(foundry_app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        response = client.post(
            "/api/internal/worker/provider-dispatch",
            json={
                "method": "POST",
                "path": "/responses",
                "headers": {},
                "body": {
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                    "max_output_tokens": 4,
                },
            },
            headers={"x-tawb-run-capability": capability},
        )
        assert response.status_code == 429
        assert response.json()["code"] == "budget_exhausted"


def test_worker_dispatch_accepts_authorized_body_over_browser_limit_via_hosted_transport(app_case):
    from openai import OpenAI

    from tradingagents.workbench.public_worker import HostedWorkerConfig, HostedWorkerContext

    upstream_requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_fixture",
                "object": "response",
                "model": "gpt-5",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                    }
                ],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "total_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    run_id = str(uuid4())
    capability = app_case.gateway.authorize(
        run_id,
        app_case.owner.principal,
        app_case.settings.pin(app_case.owner.principal, run_id),
        expires_at=NOW + 3600,
    ).serialize()
    config = HostedWorkerConfig.from_environment(
        {
            "TAWB_WORKER_MODE": "hosted",
            "TAWB_BACKEND_BASE_URL": "https://workbench.example",
            "TAWB_RUN_CAPABILITY": capability,
            "TRADINGAGENTS_RUN_ID": run_id,
            "TAWB_PUBLIC_CONFIG": json.dumps(
                {
                    "mode": "api_key",
                    "provider": "openai",
                    "quick_model": "gpt-5",
                    "deep_model": "gpt-5-mini",
                    "asset_type": "stock",
                    "data_vendors": default_request()["data_vendors"],
                }
            ),
        }
    )
    original_http = app_case.gateway._http
    large_text = "x" * (20 * 1024)
    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream_client:
        app_case.gateway._http = upstream_client
        with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
            backend_client = _worker_backend_bridge(client)
            context = HostedWorkerContext(config, http_client=backend_client)
            try:
                llm = context.create_llm_client("openai", "gpt-5", max_tokens=7).get_llm()
                sdk = OpenAI(api_key="dummy-openai", max_retries=0, http_client=llm.http_client)
                response = sdk.responses.create(
                    model="gpt-5",
                    input=[{"role": "user", "content": [{"type": "input_text", "text": large_text}]}],
                    max_output_tokens=7,
                )
            finally:
                context.close()
                backend_client.close()
        app_case.gateway._http = original_http

    assert response.output[0].content[0].text == "ok"
    assert len(upstream_requests) == 1
    forwarded = json.loads(upstream_requests[0].content.decode("utf-8"))
    assert forwarded["input"][0]["content"][0]["text"] == large_text


def test_worker_events_intake_merges_owned_direct_event_snapshot(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        created = client.post(
            "/api/runs",
            json={
                "submission_id": str(uuid4()),
                "request": {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]},
            },
            headers=headers,
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        app_case.runs.advance_pending()
        capability = app_case.store.read("email-v1/executions/" + run_id)[0]["capability"]
        event_line = encode_event(run_id, "phase", {"stage": "analyst:market"})

        ingested = client.post(
            "/api/internal/worker/events",
            headers={"x-tawb-run-capability": capability, "content-type": "application/json"},
            json={"lines": event_line},
        )

        assert ingested.status_code == 200
        refreshed = client.get(f"/api/runs/{run_id}")
        assert refreshed.status_code == 200
        assert refreshed.json()["events"] == [{
            "kind": "phase",
            "payload": {"stage": "analyst:market"},
            "timestamp": refreshed.json()["events"][0]["timestamp"],
        }]


def test_run_events_stream_returns_direct_snapshot_without_cloud_polling(app_case):
    from tradingagents.workbench.email_server import _run_events_stream

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        created = client.post(
            "/api/runs",
            json={
                "submission_id": str(uuid4()),
                "request": {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]},
            },
            headers=headers,
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        app_case.runs.advance_pending()
        capability = app_case.store.read("email-v1/executions/" + run_id)[0]["capability"]
        client.post(
            "/api/internal/worker/events",
            headers={"x-tawb-run-capability": capability, "content-type": "application/json"},
            json={"lines": encode_event(run_id, "phase", {"stage": "analyst:market"})},
        )
        session_id = client.cookies.get("__Host-tawb-email-session")
        before_calls = list(app_case.wire.calls)

    import asyncio

    async def first_frame():
        stream = _run_events_stream(app_case.auth, app_case.runs, session_id, run_id)
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    frame = asyncio.run(first_frame())
    assert frame.startswith("event: progress\ndata: ") and frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload["run_id"] == run_id
    assert payload["source"] == "direct"
    assert app_case.wire.calls == before_calls


def test_run_events_asgi_preflight_and_disconnect(app_case):
    import asyncio

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        created = client.post(
            "/api/runs",
            json={"submission_id": str(uuid4()), "request": {
                "ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"],
            }},
            headers={"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]},
        )
        run_id = created.json()["id"]
        cookie = client.cookies.get("__Host-tawb-email-session")

    async def request_events(session_cookie):
        messages = []
        disconnected = asyncio.Event()
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
            "path": f"/api/runs/{run_id}/events", "raw_path": f"/api/runs/{run_id}/events".encode(),
            "query_string": b"", "root_path": "", "server": ("workbench.example.com", 443),
            "client": ("127.0.0.1", 5000),
            "headers": [(b"host", b"workbench.example.com"),
                        (b"cookie", f"__Host-tawb-email-session={session_cookie}".encode())],
        }

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                disconnected.set()

        await asyncio.wait_for(app_case.app(scope, receive, send), timeout=2)
        return messages

    messages = asyncio.run(request_events(cookie))
    assert messages[0]["status"] == 200
    assert messages[1]["body"].startswith(b"event: progress\ndata: ")
    assert messages[1]["body"].endswith(b"\n\n")
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        login(client, app_case.guest.principal.email, GUEST_PASSWORD)
        guest_cookie = client.cookies.get("__Host-tawb-email-session")
    forbidden = asyncio.run(request_events(guest_cookie))
    assert forbidden[0]["status"] == 404
    assert not any(message.get("body", b"").startswith(b"event: progress") for message in forbidden)


def test_worker_events_intake_accepts_single_multipart_event_snapshot(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        session_id = client.cookies.get("__Host-tawb-email-session")
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        created = client.post(
            "/api/runs",
            json={"submission_id": str(uuid4()), "request": {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]}},
            headers=headers,
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        app_case.runs.advance_pending()
        capability = app_case.wire.secrets[0]["value"]
        before_calls = list(app_case.wire.calls)
        large_report = "".join(hashlib.sha256(str(index).encode()).hexdigest() for index in range(12000))
        lines = encode_event(run_id, "report", {"section": "market_report", "content": large_report})
        assert len(lines) > 1
        accepted = client.post(
            "/api/internal/worker/events",
            headers={"x-tawb-run-capability": capability, "content-type": "application/json"},
            json={"lines": lines},
        )
        assert accepted.status_code == 200
        payload = app_case.runs.event_snapshot(session_id, run_id)

    assert payload["run_id"] == run_id
    assert payload["source"] == "direct"
    assert len(payload["events"]) == 1
    report = next(event for event in payload["events"] if event["kind"] == "report")
    assert report["payload"] == {"section": "market_report", "content": large_report}
    assert app_case.wire.calls == before_calls


def test_run_events_stream_emits_keepalive_after_snapshot(app_case, monkeypatch):
    import asyncio

    from tradingagents.workbench import email_server as email_server_module

    monkeypatch.setattr(email_server_module, "_STREAM_POLL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(email_server_module, "_STREAM_HEARTBEAT_SECONDS", 0.01, raising=False)

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        session_id = client.cookies.get("__Host-tawb-email-session")
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        created = client.post(
            "/api/runs",
            json={
                "submission_id": str(uuid4()),
                "request": {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]},
            },
            headers=headers,
        )
        run_id = created.json()["id"]

    now = [0.0]

    async def check():
        async def advance(_seconds):
            now[0] += 0.02

        stream = email_server_module._run_events_stream(
            app_case.auth, app_case.runs, session_id, run_id,
            now_fn=lambda: now[0], sleep_fn=advance,
        )
        try:
            assert (await anext(stream)).startswith("event: progress\ndata: ")
            assert await anext(stream) == ": keepalive\n\n"
        finally:
            await stream.aclose()

    asyncio.run(check())


def test_run_events_stream_revalidates_session_before_next_cycle(app_case, monkeypatch):
    import asyncio

    from tradingagents.workbench import email_server as email_server_module
    from tradingagents.workbench.public_accounts import AccountError

    monkeypatch.setattr(email_server_module, "_STREAM_POLL_SECONDS", 0.01, raising=False)

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        session_id = client.cookies.get("__Host-tawb-email-session")
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        created = client.post(
            "/api/runs",
            json={
                "submission_id": str(uuid4()),
                "request": {"ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"]},
            },
            headers=headers,
        )
        run_id = created.json()["id"]

    async def check():
        stream = email_server_module._run_events_stream(
            app_case.auth, app_case.runs, session_id, run_id,
            sleep_fn=lambda _seconds: asyncio.sleep(0),
        )
        try:
            assert (await anext(stream)).startswith("event: progress\ndata: ")
            app_case.auth.logout(session_id)
            with pytest.raises(AccountError):
                await anext(stream)
        finally:
            await stream.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("revocation", ["expired", "disabled"])
def test_run_events_stream_closes_on_session_revocation(app_case, revocation):
    import asyncio

    from tradingagents.workbench.email_server import _run_events_stream

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        admin_session_id = client.cookies.get("__Host-tawb-email-session")
        if revocation == "disabled":
            login(client, app_case.guest.principal.email, GUEST_PASSWORD)
        session_id = client.cookies.get("__Host-tawb-email-session")
        created = client.post("/api/runs", json={"submission_id": str(uuid4()), "request": {
            "ticker": "MSFT", "analysis_date": "2026-09-01", "analysts": ["market"],
        }}, headers={"Origin": ORIGIN, "X-Workbench-Token": (
            owner["csrf_token"] if revocation == "expired" else app_case.auth.session(session_id).csrf_token)})
        run_id = created.json()["id"]

    async def check():
        stream = _run_events_stream(app_case.auth, app_case.runs, session_id, run_id,
                                    sleep_fn=lambda _seconds: asyncio.sleep(0))
        try:
            assert (await anext(stream)).startswith("event: progress\ndata: ")
            if revocation == "expired":
                app_case.auth.clock = lambda: app_case.owner.expires_at + 1
            else:
                app_case.auth.disable(admin_session_id, app_case.guest.principal.object_id,
                                      expected_version=app_case.auth.accounts(admin_session_id)['version'])
            with pytest.raises(AccountError):
                await anext(stream)
        finally:
            await stream.aclose()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("headers", "content", "status_code"),
    [
        (
            {"content-type": "application/json"},
            json.dumps(
                {
                    "method": "POST",
                    "path": "/responses",
                    "headers": {},
                    "body": {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4},
                }
            ).encode("utf-8"),
            403,
        ),
        (
            [
                ("x-tawb-run-capability", "duplicate"),
                ("x-tawb-run-capability", "duplicate"),
                ("content-type", "application/json"),
            ],
            json.dumps(
                {
                    "method": "POST",
                    "path": "/responses",
                    "headers": {},
                    "body": {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}], "max_output_tokens": 4},
                }
            ).encode("utf-8"),
            403,
        ),
        (
            None,
            json.dumps(
                {
                    "method": "POST",
                    "path": "/responses",
                    "headers": {},
                    "body": {"input": [{"role": "user", "content": [{"type": "input_text", "text": "x" * (2 * 1024 * 1024)}]}]},
                }
            ).encode("utf-8"),
            413,
        ),
    ],
    ids=["missing-capability", "duplicate-capability", "oversized-envelope"],
)
def test_worker_dispatch_rejects_invalid_auth_or_oversized_envelope_before_paid_dispatch(
    app_case, headers, content, status_code,
):
    upstream_requests = []
    original_http = app_case.gateway._http
    run_id = str(uuid4())
    capability = app_case.gateway.authorize(
        run_id,
        app_case.owner.principal,
        app_case.settings.pin(app_case.owner.principal, run_id),
        expires_at=NOW + 3600,
    ).serialize()

    def respond(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as upstream_client:
        app_case.gateway._http = upstream_client
        with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
            request_headers = headers
            if request_headers is None:
                request_headers = {
                    "x-tawb-run-capability": capability,
                    "content-type": "application/json",
                }
            response = client.post(
                "/api/internal/worker/provider-dispatch",
                headers=request_headers,
                content=content,
            )
        app_case.gateway._http = original_http

    assert response.status_code == status_code
    assert upstream_requests == []


def test_admin_origin_policy_and_invite_recovery_routes_return_fragment_only_data(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        owner = login(client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        assert client.get("/api/budget").status_code == 200
        policy = client.get("/api/admin/approved-origins")
        assert policy.status_code == 200
        assert policy.json()["approved_origins"] == ["https://relay.example.com"]
        updated = client.put(
            "/api/admin/approved-origins",
            json={"approved_origins": ["https://next.example.com"]},
            headers={**headers, "If-Match": policy.json()["version"] or ""},
        )
        assert updated.status_code == 200
        assert updated.json()["approved_origins"] == ["https://next.example.com"]

        invite = client.post(
            "/api/admin/invitations",
            json={"email": "new-user@example.com"},
            headers=headers,
        )
        assert invite.status_code == 200
        assert invite.json()["fragment"].startswith("/auth/redeem#token=")
        assert "?token=" not in invite.json()["fragment"]

        users = client.get("/api/admin/users").json()["users"]
        guest_id = next(row["object_id"] for row in users if row["email"] == app_case.guest.principal.email)
        recovery = client.post(
            "/api/admin/recoveries",
            json={"user_id": guest_id},
            headers=headers,
        )
        assert recovery.status_code == 200
        assert recovery.json()["fragment"].startswith("/auth/reset#token=")
        assert "?token=" not in recovery.json()["fragment"]


def test_email_redeem_reset_and_password_change_use_public_json_routes(app_case):
    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as admin_client:
        owner = login(admin_client, app_case.owner.principal.email, OWNER_PASSWORD)
        headers = {"Origin": ORIGIN, "X-Workbench-Token": owner["csrf_token"]}
        invite = admin_client.post(
            "/api/admin/invitations",
            json={"email": "second-user@example.com"},
            headers=headers,
        ).json()["fragment"]
        recovery = admin_client.post(
            "/api/admin/recoveries",
            json={"user_id": app_case.guest.principal.object_id},
            headers=headers,
        ).json()["fragment"]

    with TestClient(app_case.app, base_url=ORIGIN, follow_redirects=False) as client:
        token = invite.split("#token=", 1)[1].split("&email=", 1)[0]
        redeemed = client.post(
            "/auth/redeem",
            json={"token": token, "email": "second-user@example.com", "password": "fixture-second-password-2026"},
            headers={"Origin": ORIGIN},
        )
        assert redeemed.status_code == 200
        assert redeemed.json()["user"]["email"] == "second-user@example.com"

        reset_token = recovery.split("#token=", 1)[1].split("&email=", 1)[0]
        reset = client.post(
            "/auth/reset",
            json={"token": reset_token, "email": app_case.guest.principal.email, "password": "fixture-reset-password-2026"},
            headers={"Origin": ORIGIN},
        )
        assert reset.status_code == 200

        client.post(
            "/auth/logout",
            headers={"Origin": ORIGIN, "X-Workbench-Token": reset.json()["csrf_token"]},
        )
        assert client.post(
            "/auth/login",
            json={"email": app_case.guest.principal.email, "password": GUEST_PASSWORD},
            headers={"Origin": ORIGIN},
        ).status_code == 401
        guest = login(client, app_case.guest.principal.email, "fixture-reset-password-2026")
        updated = client.post(
            "/auth/password",
            json={"current_password": "fixture-reset-password-2026", "new_password": "fixture-change-password-2026"},
            headers={"Origin": ORIGIN, "X-Workbench-Token": guest["csrf_token"]},
        )
        assert updated.status_code == 200
        client.post(
            "/auth/logout",
            headers={"Origin": ORIGIN, "X-Workbench-Token": updated.json()["csrf_token"]},
        )
        assert client.post(
            "/auth/login",
            json={"email": app_case.guest.principal.email, "password": "fixture-reset-password-2026"},
            headers={"Origin": ORIGIN},
        ).status_code == 401
        assert client.post(
            "/auth/login",
            json={"email": app_case.guest.principal.email, "password": "fixture-change-password-2026"},
            headers={"Origin": ORIGIN},
        ).status_code == 200
