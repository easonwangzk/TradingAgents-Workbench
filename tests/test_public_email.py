# Modified for public distribution: installation identifiers replaced with examples.
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from azure.communication.email import EmailClient
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import HttpResponse, HttpTransport

from tradingagents.workbench.public_accounts import AccountError


def test_email_sender_uses_fixed_sender_and_single_recipient_without_retry():
    from tradingagents.workbench.public_email import VerificationEmailSender

    calls = []

    def begin_send(message, **kwargs):
        calls.append((message, kwargs))
        return SimpleNamespace(result=lambda: {"id": str(uuid4()), "status": "Running"})

    sender = VerificationEmailSender(SimpleNamespace(begin_send=begin_send), "donotreply@fixture.azurecomm.net")
    sender("guest@example.com", "123456")
    message, options = calls[0]
    assert message["senderAddress"] == "donotreply@fixture.azurecomm.net"
    assert message["recipients"] == {"to": [{"address": "guest@example.com"}]}
    assert "123456" in message["content"]["plainText"]
    assert "10 minutes" in message["content"]["plainText"]
    assert message["userEngagementTrackingDisabled"] is True
    assert options["polling"] is False
    assert options["retry_total"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("result", [None, {}, {"id": str(uuid4()), "status": "Failed"}, {"id": "bad", "status": "Running"}])
def test_email_sender_rejects_unconfirmed_provider_outcome(result):
    from tradingagents.workbench.public_email import VerificationEmailSender

    client = SimpleNamespace(begin_send=lambda *args, **kwargs: SimpleNamespace(result=lambda: result))
    with pytest.raises(AccountError) as error:
        VerificationEmailSender(client, "donotreply@fixture.azurecomm.net")("guest@example.com", "123456")
    assert error.value.status_code == 503
    assert "123456" not in str(error.value)


def test_email_sender_sanitizes_uncertain_delivery_without_resending():
    from tradingagents.workbench.public_email import VerificationEmailSender

    calls = []

    def begin_send(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("private provider info 123456")

    with pytest.raises(AccountError) as error:
        VerificationEmailSender(SimpleNamespace(begin_send=begin_send), "donotreply@fixture.azurecomm.net")("guest@example.com", "123456")
    assert error.value.status_code == 503
    assert "private" not in str(error.value)
    assert calls == [True]


@pytest.mark.parametrize("environment", [
    {"TAWB_EMAIL_ENDPOINT": "https://fixture.communication.azure.com"},
    {"TAWB_EMAIL_ENDPOINT": "http://fixture.communication.azure.com", "TAWB_EMAIL_SENDER": "a@fixture.azurecomm.net"},
    {"TAWB_EMAIL_ENDPOINT": "https://attacker.example", "TAWB_EMAIL_SENDER": "a@fixture.azurecomm.net"},
    {"TAWB_EMAIL_ENDPOINT": "https://fixture.communication.azure.com?secret=bad", "TAWB_EMAIL_SENDER": "a@fixture.azurecomm.net"},
])
def test_email_configuration_rejects_incomplete_or_nonazure_endpoint(environment):
    from tradingagents.workbench.public_email import email_configuration

    with pytest.raises(ValueError):
        email_configuration(environment)


def test_email_configuration_is_explicit_and_optional():
    from tradingagents.workbench.public_email import email_configuration

    assert email_configuration({}) is None
    assert email_configuration({
        "TAWB_EMAIL_ENDPOINT": "https://example-email.unitedstates.communication.azure.com",
        "TAWB_EMAIL_SENDER": "donotreply@fixture.azurecomm.net",
    })[0] == "https://example-email.unitedstates.communication.azure.com"
    assert email_configuration({"TAWB_EMAIL_ENDPOINT": "https://fixture.communication.azure.com/", "TAWB_EMAIL_SENDER": "DoNotReply@fixture.azurecomm.net"}) == (
        "https://fixture.communication.azure.com", "donotreply@fixture.azurecomm.net",
    )


@pytest.mark.parametrize("status_code", [202, 429])
def test_real_sdk_initial_acceptance_does_not_poll_or_retry(status_code):
    from tradingagents.workbench.public_email import VerificationEmailSender

    class Response(HttpResponse):
        def __init__(self, request):
            super().__init__(request, None)
            self.status_code = status_code
            self.headers = {
                "content-type": "application/json",
                "operation-location": "https://fixture.communication.azure.com/emails/operations/fixture",
                "retry-after": "60",
            }
            self.content = json.dumps({"id": str(uuid4()), "status": "Running"}).encode()

        def body(self):
            return self.content

        def read(self):
            return self.content

        def iter_bytes(self):
            yield self.content

        def json(self):
            return json.loads(self.content)

    class Transport(HttpTransport):
        def __init__(self):
            self.requests = []

        def open(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def send(self, request, **kwargs):
            self.requests.append(request)
            return Response(request)

    transport = Transport()
    credential = SimpleNamespace(get_token=lambda *args, **kwargs: AccessToken("fixture-only", 9999999999))
    with EmailClient("https://fixture.communication.azure.com", credential, transport=transport) as client:
        sender = VerificationEmailSender(client, "donotreply@fixture.azurecomm.net")
        if status_code == 202:
            sender("guest@example.com", "123456")
        else:
            with pytest.raises(AccountError):
                sender("guest@example.com", "123456")
    assert len(transport.requests) == 1
    assert transport.requests[0].method == "POST"
    assert "/emails:send?" in transport.requests[0].url
