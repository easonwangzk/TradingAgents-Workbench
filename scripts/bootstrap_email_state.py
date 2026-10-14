"""Bootstrap only the email state encryption key through ARM."""

import argparse
import json
import re
from pathlib import Path

import httpx
from azure.identity import AzureCliCredential

from scripts.public_bootstrap import _ensure_private_secrets
from tradingagents.workbench.public_auth import canonical_uuid

_IDENTITY_ID_PATTERN = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-f-]{36})/resourceGroups/(?P<resource_group>[\w.-]+)/"
    r"providers/Microsoft\.ManagedIdentity/userAssignedIdentities/(?P<name>[\w.-]+)$",
    re.IGNORECASE,
)
_VAULT_ID_PATTERN = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-f-]{36})/resourceGroups/(?P<resource_group>[\w.-]+)/"
    r"providers/Microsoft\.KeyVault/vaults/(?P<name>[A-Za-z0-9-]+)$",
    re.IGNORECASE,
)


def _parse_resource_id(resource_id, pattern, label):
    match = pattern.fullmatch(resource_id)
    if not match:
        raise ValueError(f"Invalid {label} resource ID")
    return match.groupdict()


def _build_cli_credential(subscription_id, tenant_id):
    canonical_uuid(tenant_id)
    return AzureCliCredential(subscription=canonical_uuid(subscription_id))


def bootstrap_email_state_key(credential, client, *, tenant_id, subscription_id, identity_id, vault_id, receipt_path):
    tenant_id = canonical_uuid(tenant_id)
    subscription_id = canonical_uuid(subscription_id)
    identity = _parse_resource_id(identity_id, _IDENTITY_ID_PATTERN, "managed identity")
    vault = _parse_resource_id(vault_id, _VAULT_ID_PATTERN, "Key Vault")

    identity_subscription = canonical_uuid(identity["subscription"])
    vault_subscription = canonical_uuid(vault["subscription"])
    if identity_subscription != subscription_id or vault_subscription != subscription_id:
        raise ValueError("Managed identity and Key Vault must both use the expected subscription")
    if identity["resource_group"] != vault["resource_group"]:
        raise ValueError("Managed identity and Key Vault must both use the same resource group")

    token = credential.get_token("https://management.azure.com/.default")
    response = client.get(
        "https://management.azure.com" + identity_id,
        params={"api-version": "2023-01-31"},
        headers={"Authorization": "Bearer " + token.token},
    )
    response.raise_for_status()
    payload = response.json()
    properties = payload.get("properties") or {}
    identity_client_id = canonical_uuid(properties.get("clientId"))
    if payload.get("id", "").casefold() != identity_id.casefold():
        raise ValueError("Managed identity resource did not match the requested identity")
    if properties.get("tenantId") != tenant_id:
        raise ValueError("Managed identity tenant did not match the expected tenant")

    receipt = _ensure_private_secrets(
        credential,
        client,
        identity_client_id,
        vault_id,
        Path(receipt_path),
        ("state-encryption-key",),
    )
    return {
        "tenant_id": tenant_id,
        "subscription_id": subscription_id,
        "resource_group": identity["resource_group"],
        "identity_id": identity_id,
        "identity_client_id": identity_client_id,
        "vault_id": vault_id,
        "receipt_path": str(Path(receipt_path)),
        "credential": "managed-identity-state-key",
        "records": receipt["records"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--identity-id", required=True)
    parser.add_argument("--vault-id", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    try:
        with (
            _build_cli_credential(args.subscription_id, args.tenant_id) as credential,
            httpx.Client(timeout=30, follow_redirects=False) as client,
        ):
            result = bootstrap_email_state_key(
                credential,
                client,
                tenant_id=args.tenant_id,
                subscription_id=args.subscription_id,
                identity_id=args.identity_id,
                vault_id=args.vault_id,
                receipt_path=args.receipt,
            )
        print(json.dumps(result))
    except Exception:
        raise SystemExit(
            "Email state bootstrap failed. Check the target IDs, ARM access, and bootstrap receipt state."
        ) from None


if __name__ == "__main__":
    main()
