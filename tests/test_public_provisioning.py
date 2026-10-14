import copy

import pytest

from tests.test_public_auth import BOB, auth_harness as auth_harness, sign_in
from tests.test_public_profiles import FOUNDRY, SUB, resources as resources

INITIALIZE = {"subscription_id": SUB, "resource_group": "new-research", "location": "eastus2",
              "prefix": "my-research", "foundry_account_id": FOUNDRY, "deployment_name": "research-model"}


@pytest.fixture
def provisioning(resources, harness):
    from tradingagents.workbench.public_profiles import AzureResources
    from tradingagents.workbench.public_provisioning import Provisioning

    cloud, _, values, requests = resources
    auth, provider, clock, _ = harness
    provider.token_result["access_token"] = "delegated-only"
    _, session = sign_in(auth)
    values[f"/subscriptions/{SUB}/locations"] = {"value": [{"name": "eastus2"}]}

    def factory(principal, credential):
        return AzureResources(principal, credential, client=cloud.client)

    service = Provisioning(auth=auth, resource_factory=factory, clock=lambda: clock[0])
    return service, session, values, requests, auth, provider, clock


def test_preview_only_reads_azure_and_persists_exact_owner_plan(provisioning):
    service, session, _, requests, _, _, _ = provisioning
    preview = service.preview(session.session_id, INITIALIZE)
    assert preview["status"] == "preview"
    assert preview["plan"]["selection"] == INITIALIZE
    assert preview["plan"]["foundry"]["endpoint"] == "https://research-ai.services.ai.azure.com/"
    kinds = {resource["kind"] for resource in preview["plan"]["resources"]}
    assert kinds == {"registry", "workspace", "environment", "identity", "job"}
    assert all(resource["id"].startswith(f"/subscriptions/{SUB}/resourceGroups/new-research/")
               for resource in preview["plan"]["resources"])
    assert {role["role"] for role in preview["plan"]["roles"]} == {"AcrPull", "Cognitive Services OpenAI User"}
    assert all(role["scope"] != f"/subscriptions/{SUB}" for role in preview["plan"]["roles"])
    assert preview["plan"]["charges"]
    assert service.get(session.session_id, preview["id"]) == preview
    assert {request.method for request in requests} == {"GET"}


def test_user_cannot_get_or_confirm_another_users_preview(provisioning):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, _, requests, auth, provider, _ = provisioning
    preview = service.preview(session.session_id, INITIALIZE)
    provider.claims["oid"] = BOB
    _, bob = sign_in(auth)
    count = len(requests)
    assert service.list(bob.session_id) == []
    with pytest.raises(ResourceError) as failure:
        service.get(bob.session_id, preview["id"])
    assert failure.value.status_code == 404
    with pytest.raises(ResourceError):
        service.confirm(bob.session_id, preview["id"], preview["confirmation"])
    assert len(requests) == count


def test_confirmation_is_exact_and_cannot_change_preview_parameters(provisioning):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, _, requests, _, _, _ = provisioning
    preview = service.preview(session.session_id, INITIALIZE)
    count = len(requests)
    with pytest.raises(ResourceError):
        service.confirm(session.session_id, preview["id"], "yes")
    assert service.get(session.session_id, preview["id"])["status"] == "preview"
    confirmed = service.confirm(session.session_id, preview["id"], preview["confirmation"])
    assert confirmed["status"] == "confirmed"
    assert confirmed["plan"] == preview["plan"]
    assert service.confirm(session.session_id, preview["id"], preview["confirmation"])["plan"] == preview["plan"]
    assert len(requests) == count


def test_expired_preview_requires_new_review(provisioning):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, _, _, _, _, clock = provisioning
    preview = service.preview(session.session_id, INITIALIZE)
    clock[0] += 3601
    with pytest.raises(ResourceError):
        service.confirm(session.session_id, preview["id"], preview["confirmation"])


@pytest.mark.parametrize("change", [{"owner": BOB}, {"endpoint": "http://169.254.169.254/"},
                                   {"prefix": "../other"}, {"resource_group": "bad/../group"}])
def test_preview_rejects_untrusted_fields_before_network(provisioning, change):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, _, requests, _, _, _ = provisioning
    with pytest.raises(ResourceError):
        service.preview(session.session_id, {**INITIALIZE, **change})
    assert requests == []


def test_existing_resource_collision_is_never_adopted_or_overwritten(provisioning):
    from tradingagents.workbench.public_profiles import ResourceError

    service, session, values, requests, _, _, _ = provisioning
    existing = f"/subscriptions/{SUB}/resourceGroups/new-research/providers/Microsoft.App/jobs/my-research-job"
    values[existing] = {"id": existing, "tags": {"owner": "another-project"}}
    before = copy.deepcopy(values[existing])
    with pytest.raises(ResourceError):
        service.preview(session.session_id, INITIALIZE)
    assert values[existing] == before
    assert {request.method for request in requests} == {"GET"}


def test_azure_writes_are_delegated_bounded_and_keep_authorization_denials():
    import httpx

    from tests.test_public_profiles import ALICE_PRINCIPAL, Credential
    from tradingagents.workbench.public_profiles import AzureResources, ResourceError

    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer delegated-only"
        assert request.headers["if-none-match"] == "*"
        return httpx.Response(403, text="private-cloud-diagnostic")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        cloud = AzureResources(ALICE_PRINCIPAL, Credential(), client=client)
        with pytest.raises(ResourceError) as failure:
            cloud.write(f"/subscriptions/{SUB}/resourceGroups/new-research", "2021-04-01",
                        {"location": "eastus2"}, create_only=True)
        assert failure.value.status_code == 403
        assert "private-cloud" not in str(failure.value)
        with pytest.raises(ResourceError):
            cloud.write("https://attacker.example/", "2021-04-01", {})
    assert len(calls) == 1


@pytest.mark.parametrize("persistent", [False, True])
def test_preview_index_conflict_does_not_orphan_operations(provisioning, monkeypatch, persistent):
    from tradingagents.workbench.public_store import StoreConflict

    service, session, _, _, _, _, _ = provisioning
    original_write = service.store.write
    captured = []
    failures = []

    def conflict(key, value, expected_version=None):
        if "/operations/" in key:
            captured.append(key)
        if key.endswith("/operation-index") and (persistent or not failures):
            failures.append(key)
            raise StoreConflict("Simulated index race")
        return original_write(key, value, expected_version)

    monkeypatch.setattr(service.store, "write", conflict)
    if persistent:
        with pytest.raises(StoreConflict):
            service.preview(session.session_id, INITIALIZE)
        assert service.store.read(captured[0]) is None
    else:
        preview = service.preview(session.session_id, INITIALIZE)
        assert service.list(session.session_id) == [preview]
