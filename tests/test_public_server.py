import pytest
from fastapi.testclient import TestClient

from tests.test_public_auth import ALICE, TENANT, auth_harness as auth_harness

ORIGIN = "https://workbench.example.com"


@pytest.fixture
def web(harness):
    from tradingagents.workbench.public_server import create_public_app

    auth, provider, clock, backend = harness
    with TestClient(create_public_app(auth=auth), base_url=ORIGIN, follow_redirects=False) as client:
        yield client, provider, clock, backend


def login(client):
    started = client.get("/auth/login")
    assert started.status_code == 303
    callback = client.get("/auth/callback", params={"state": "provider-state", "code": "one-time-code"})
    assert callback.status_code == 303
    return started, callback


def test_anonymous_requests_and_forged_platform_headers_cannot_access_data(web):
    client, _, _, _ = web
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL-ID": ALICE}).status_code == 401
    assert client.get("/").headers["location"] == "/auth/login"


def test_login_cookie_and_identity_response_do_not_expose_credentials(web):
    client, _, _, _ = web
    started, callback = login(client)
    for response in (started, callback):
        cookies = response.headers.get_list("set-cookie")
        assert all("Secure" in cookie and "HttpOnly" in cookie for cookie in cookies)
        assert all("Path=/" in cookie and "SameSite=lax" in cookie for cookie in cookies)
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"] == {"tenant_id": TENANT, "object_id": ALICE, "name": "Alice"}
    assert "private-" not in me.text
    assert "cache" not in me.json()
    assert me.headers["cache-control"] == "no-store"


def test_cross_origin_and_cross_session_writes_are_rejected(web):
    client, _, _, _ = web
    login(client)
    token = client.get("/api/me").json()["csrf_token"]
    assert client.post("/auth/logout", headers={"X-Workbench-Token": token}).status_code == 403
    assert client.post("/auth/logout", headers={"Origin": "https://attacker.example", "X-Workbench-Token": token}).status_code == 403
    assert client.post("/auth/logout", headers={"Origin": ORIGIN, "X-Workbench-Token": "wrong-session"}).status_code == 403
    assert client.get("/api/me").status_code == 200


def test_logout_revokes_server_session_and_callback_cannot_be_replayed(web):
    client, _, _, _ = web
    login(client)
    captured = dict(client.cookies)
    token = client.get("/api/me").json()["csrf_token"]
    assert client.post("/auth/logout", headers={"Origin": ORIGIN, "X-Workbench-Token": token}).status_code == 200
    client.cookies.update(captured)
    assert client.get("/api/me").status_code == 401
    assert client.get("/auth/callback", params={"state": "provider-state", "code": "one-time-code"}).status_code == 401


def test_wrong_host_duplicate_callback_and_oversized_body_fail_closed(web):
    client, _, _, _ = web
    assert client.get("/auth/login", headers={"Host": "attacker.example"}).status_code == 400
    client.get("/auth/login")
    assert client.get("/auth/callback?state=provider-state&state=other&code=one-time-code").status_code == 400
    login(client)
    token = client.get("/api/me").json()["csrf_token"]
    assert client.post("/auth/logout", content="x" * 20000, headers={"Origin": ORIGIN, "X-Workbench-Token": token}).status_code == 413


def test_logout_does_not_acknowledge_a_failed_conditional_delete(web, monkeypatch):
    client, _, _, backend = web
    login(client)
    token = client.get("/api/me").json()["csrf_token"]
    original_delete = backend.delete

    def concurrent_refresh(key, expected_version):
        content, version = backend.read(key)
        backend.write(key, content, version)
        original_delete(key, expected_version)

    monkeypatch.setattr(backend, "delete", concurrent_refresh)
    response = client.post("/auth/logout", headers={"Origin": ORIGIN, "X-Workbench-Token": token})
    assert response.status_code == 409
    assert "signed_out" not in response.json()
    monkeypatch.setattr(backend, "delete", original_delete)
    assert client.post("/auth/logout", headers={"Origin": ORIGIN, "X-Workbench-Token": token}).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_cross_site_bad_state_cannot_destroy_the_pending_valid_login(web):
    client, _, _, _ = web
    assert client.get("/auth/login").status_code == 303
    bad = client.get("/auth/callback", params={"state": "forged-state", "code": "forged-code"})
    assert bad.status_code == 401
    valid = client.get("/auth/callback", params={"state": "provider-state", "code": "one-time-code"})
    assert valid.status_code == 303
    assert client.get("/api/me").json()["user"]["object_id"] == ALICE


def test_anonymous_login_burst_is_bounded_before_storage_allocation(web):
    client, _, clock, backend = web
    for _attempt in range(60):
        assert client.get("/auth/login").status_code == 303
    record_count = len(backend.records)
    blocked = client.get("/auth/login")
    assert blocked.status_code == 429
    assert len(backend.records) == record_count
    assert blocked.headers["retry-after"] == "60"
    clock[0] += 61
    assert client.get("/auth/login").status_code == 303


def test_public_ui_requires_session_and_uses_per_response_script_nonce(web):
    client, _, _, _ = web
    assert client.get("/static/app.js").status_code == 401
    login(client)
    page = client.get("/")
    assert page.status_code == 200
    policy = page.headers["content-security-policy"]
    nonce = policy.split("'nonce-", 1)[1].split("'", 1)[0]
    assert f'<script nonce="{nonce}">' in page.text
    assert "script-src 'self' 'nonce-" in policy
    assert "frame-ancestors 'none'" in policy
    assert "private-" not in page.text
    assert client.get("/").headers["content-security-policy"] != policy
    for asset in ["app.js", "boot.js", "public-ui.js", "public-init.js", "ui-core.mjs", "styles.css", "vendor.js"]:
        response = client.get("/static/" + asset)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    for path in ["package.json", "vendor-entry.js", "node_modules/lucide/package.json", "%2e%2e/public_auth.py"]:
        assert client.get("/static/" + path).status_code == 404


def test_signed_out_page_is_public_without_private_ui_or_automatic_login(web):
    client, _, _, _ = web
    response = client.get("/signed-out")
    assert response.status_code == 200
    assert 'href="/auth/login"' in response.text
    assert "<script" not in response.text
    assert "workbench" not in response.headers.get("set-cookie", "")
