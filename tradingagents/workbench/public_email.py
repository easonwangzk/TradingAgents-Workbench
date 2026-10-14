"""Bounded Azure Communication Email delivery for registration codes."""

import re
from urllib.parse import urlsplit
from uuid import UUID

from tradingagents.workbench.public_accounts import AccountError, _email


def email_configuration(environment):
    fields = ("TAWB_EMAIL_ENDPOINT", "TAWB_EMAIL_SENDER")
    if not any(name in environment for name in fields):
        return None
    endpoint, sender = (environment.get(name) for name in fields)
    if not isinstance(endpoint, str) or not isinstance(sender, str):
        raise ValueError("Incomplete email configuration")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)?\.communication\.azure\.com", parsed.hostname)
            or parsed.netloc != parsed.hostname or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("Invalid email endpoint")
    try:
        sender = _email(sender)
    except AccountError:
        raise ValueError("Invalid email sender") from None
    return endpoint.rstrip("/"), sender


class VerificationEmailSender:
    def __init__(self, client, sender):
        self.client = client
        self.sender = _email(sender)

    def __call__(self, email, code):
        email = _email(email)
        if type(code) is not str or not re.fullmatch(r"[0-9]{6}", code):
            raise AccountError(status_code=400)
        message = {
            "senderAddress": self.sender,
            "recipients": {"to": [{"address": email}]},
            "content": {
                "subject": "TradingAgents email verification",
                "plainText": (
                    f"Your TradingAgents registration code is: {code}\n\n"
                    "This code expires in 10 minutes and can only be used once.\n"
                    "Do not share it. If you did not request registration, ignore this email."
                ),
            },
            "userEngagementTrackingDisabled": True,
        }
        try:
            result = self.client.begin_send(
                message, polling=False, retry_total=0, connection_timeout=5,
                read_timeout=15, logging_enable=False,
            ).result()
            if type(result) is not dict or result.get("status") not in {"Running", "NotStarted", "Succeeded"}:
                raise ValueError
            UUID(result["id"])
        except Exception:
            raise AccountError(status_code=503) from None
