"""Initialize only the approved public application's certificate and state key."""

import argparse
import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential
from azure.keyvault.secrets import SecretClient
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tradingagents.workbench.public_auth import canonical_uuid, public_origin


def _secret(vault, name):
    try:
        return vault.get_secret(name).value
    except ResourceNotFoundError:
        return None


def _application(credential, client, object_id, client_id, origin):
    canonical_uuid(object_id)
    canonical_uuid(client_id)
    origin = public_origin(origin)
    token = credential.get_token("https://graph.microsoft.com/.default")
    headers = {"Authorization": "Bearer " + token.token}
    url = "https://graph.microsoft.com/v1.0/applications/" + object_id
    response = client.get(url, headers=headers, params={"$select": "id,appId,signInAudience,keyCredentials,web"})
    response.raise_for_status()
    application = response.json()
    if application.get("id") != object_id or application.get("appId") != client_id or application.get("signInAudience") != "AzureADMyOrg":
        raise ValueError("The dedicated single-tenant application was not confirmed")
    return application, headers, url, origin


def _new_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tradingagents-public-workbench")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
                   .sign(key, hashes.SHA256()))
    return {"private_key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()).decode("ascii"),
            "public_certificate": certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            "thumbprint": certificate.fingerprint(hashes.SHA1()).hex()}


def configure_application(vault, credential, client, object_id, client_id, origin):
    application, headers, url, origin = _application(credential, client, object_id, client_id, origin)
    state_key = _secret(vault, "state-encryption-key")
    if state_key is None:
        state_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
        vault.set_secret("state-encryption-key", state_key, content_type="application/octet-stream")
    if len(base64.b64decode(state_key, validate=True)) != 32:
        raise ValueError("Existing encryption key is invalid; do not replace it")
    stored = _secret(vault, "entra-certificate")
    if stored is None:
        stored = json.dumps(_new_material())
        vault.set_secret("entra-certificate", stored, content_type="application/json")
    material = json.loads(stored)
    certificate = x509.load_pem_x509_certificate(material["public_certificate"].encode("ascii"))
    private = serialization.load_pem_private_key(material["private_key"].encode("ascii"), password=None)
    if (certificate.fingerprint(hashes.SHA1()).hex() != material["thumbprint"]
            or certificate.public_key().public_numbers() != private.public_key().public_numbers()
            or certificate.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=7)):
        raise ValueError("Existing certificate needs explicit rotation")
    return _register(client, application, headers, url, object_id, origin, certificate)


def _register(client, application, headers, url, object_id, origin, certificate):
    if certificate.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=7):
        raise ValueError("Existing certificate needs explicit rotation")
    fingerprint = base64.b64encode(certificate.fingerprint(hashes.SHA1())).decode("ascii")
    keys = application.get("keyCredentials", [])
    changed = False
    if not any(item.get("customKeyIdentifier") == fingerprint for item in keys):
        if any(not item.get("key") for item in keys):
            raise ValueError("Cannot safely preserve unreadable existing public certificates")
        keys.append({"keyId": str(uuid5(NAMESPACE_URL, object_id + certificate.fingerprint(hashes.SHA1()).hex())),
                     "type": "AsymmetricX509Cert", "usage": "Verify", "customKeyIdentifier": fingerprint,
                     "key": base64.b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode("ascii"),
                     "displayName": "public-workbench", "startDateTime": certificate.not_valid_before_utc.isoformat(),
                     "endDateTime": certificate.not_valid_after_utc.isoformat()})
        changed = True
    web = application.get("web") or {}
    redirects = web.get("redirectUris", [])
    if origin + "/auth/callback" not in redirects:
        redirects.append(origin + "/auth/callback")
        changed = True
    implicit = {"enableAccessTokenIssuance": False, "enableIdTokenIssuance": False}
    if web.get("implicitGrantSettings") != implicit:
        changed = True
    if changed:
        response = client.patch(url, headers=headers, json={"keyCredentials": keys,
            "web": {"redirectUris": redirects, "implicitGrantSettings": implicit}})
        response.raise_for_status()
    return {"origin": origin, "certificate_expires": certificate.not_valid_after_utc.isoformat()}


def configure_private_application(credential, client, object_id, client_id, origin, vault_id, receipt_path):
    application, headers, url, origin = _application(credential, client, object_id, client_id, origin)
    receipt = _ensure_private_secrets(credential, client, client_id, vault_id, receipt_path,
                                      ("state-encryption-key", "entra-certificate"))
    certificate = x509.load_pem_x509_certificate(receipt["records"]["entra-certificate"]["public_certificate"].encode("ascii"))
    return _register(client, application, headers, url, object_id, origin, certificate)


def _ensure_private_secrets(credential, client, client_id, vault_id, receipt_path, names):
    if not re.fullmatch(r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[\w.-]+/providers/Microsoft.KeyVault/vaults/[a-zA-Z0-9-]+", vault_id):
        raise ValueError("Invalid Key Vault resource ID")
    receipt_path = Path(receipt_path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    lock = receipt_path.with_suffix(".lock")
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        expected = {"version": 1, "vault_id": vault_id, "app_id": client_id}
        receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {**expected, "records": {}}
        if any(receipt.get(name) != value for name, value in expected.items()) or not isinstance(receipt.get("records"), dict):
            raise ValueError("Bootstrap receipt does not match the deployment")
        arm_headers = {"Authorization": "Bearer " + credential.get_token("https://management.azure.com/.default").token}
        for name in names:
            secret_url = "https://management.azure.com" + vault_id + "/secrets/" + name
            response = client.get(secret_url, headers=arm_headers, params={"api-version": "2023-07-01"})
            record = receipt["records"].get(name)
            if response.status_code != 404:
                response.raise_for_status()
                metadata = response.json()
                tags = metadata.get("tags") or {}
                if (not record or tags.get("tawbApp") != client_id or tags.get("tawbSha256") != record["sha256"]
                        or metadata.get("id", "").casefold() != (vault_id + "/secrets/" + name).casefold()
                        or metadata.get("properties", {}).get("attributes", {}).get("enabled") is not True):
                    raise ValueError("Existing secret does not match its bootstrap receipt; do not replace it")
                continue
            if record:
                raise ValueError("Previous secret write is uncertain; inspect it before any further writes")
            if name == "state-encryption-key":
                value = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
                record = {}
                content_type = "application/octet-stream"
            else:
                material = _new_material()
                value = json.dumps(material)
                record = {"public_certificate": material["public_certificate"]}
                content_type = "application/json"
            record["sha256"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
            receipt["records"][name] = record
            temporary = receipt_path.with_suffix("." + secrets.token_hex(8) + ".tmp")
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream)
            temporary.replace(receipt_path)
            response = client.put(secret_url, headers=arm_headers, params={"api-version": "2023-07-01"}, json={
                "tags": {"tawbApp": client_id, "tawbSha256": record["sha256"]},
                "properties": {"value": value, "contentType": content_type, "attributes": {"enabled": True}}})
            response.raise_for_status()
        return receipt
    finally:
        lock.unlink()


def configure_federated_application(credential, client, object_id, client_id, origin, tenant_id, identity_id, vault_id, receipt_path):
    canonical_uuid(tenant_id)
    if not re.fullmatch(r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[\w.-]+/providers/Microsoft.ManagedIdentity/userAssignedIdentities/[\w.-]+", identity_id):
        raise ValueError("Invalid managed identity resource ID")
    application, headers, url, origin = _application(credential, client, object_id, client_id, origin)
    response = client.get("https://management.azure.com" + identity_id, params={"api-version": "2023-01-31"},
                          headers={"Authorization": "Bearer " + credential.get_token("https://management.azure.com/.default").token})
    response.raise_for_status()
    identity = response.json()
    properties = identity.get("properties", {})
    if identity.get("id", "").casefold() != identity_id.casefold() or properties.get("tenantId") != tenant_id:
        raise ValueError("Managed identity does not match the resource and tenant")
    principal_id = canonical_uuid(properties.get("principalId"))
    identity_client_id = canonical_uuid(properties.get("clientId"))
    trust = {"name": "public-workbench-host", "issuer": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
             "subject": principal_id, "audiences": ["api://AzureADTokenExchange"]}
    response = client.get(url + "/federatedIdentityCredentials", headers=headers)
    response.raise_for_status()
    collection = response.json()
    entries = collection.get("value")
    if not isinstance(entries, list) or len(entries) > 20 or collection.get("@odata.nextLink"):
        raise ValueError("Cannot verify the complete federated credential list")
    matching = [entry for entry in entries if entry.get("name") == trust["name"]]
    if matching:
        if len(matching) != 1 or any(matching[0].get(name) != value for name, value in trust.items()):
            raise ValueError("Existing federated trust does not match the hosting identity")
    else:
        response = client.post(url + "/federatedIdentityCredentials", headers=headers, json=trust)
        response.raise_for_status()
        created = response.json()
        if any(created.get(name) != value for name, value in trust.items()):
            raise ValueError("Created federated trust did not match the requested identity")
    _ensure_private_secrets(credential, client, client_id, vault_id, receipt_path, ("state-encryption-key",))
    web = dict(application.get("web") or {})
    redirects = list(web.get("redirectUris") or [])
    callback = origin + "/auth/callback"
    if callback not in redirects:
        redirects.append(callback)
    web.update({"redirectUris": redirects, "implicitGrantSettings": {"enableAccessTokenIssuance": False, "enableIdTokenIssuance": False}})
    if web != application.get("web"):
        response = client.patch(url, headers=headers, json={"web": web})
        response.raise_for_status()
    return {"origin": origin, "identity_client_id": identity_client_id, "credential": "managed-identity-federation"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault-url")
    parser.add_argument("--vault-id")
    parser.add_argument("--receipt")
    parser.add_argument("--identity-id")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--object-id", required=True)
    args = parser.parse_args()
    try:
        with (AzureCliCredential(tenant_id=args.tenant_id) as credential,
              httpx.Client(timeout=60, follow_redirects=False) as client):
            if args.identity_id and args.vault_id and args.receipt and not args.vault_url:
                result = configure_federated_application(credential, client, args.object_id, args.client_id,
                    args.origin, args.tenant_id, args.identity_id, args.vault_id, args.receipt)
            elif args.vault_id and args.receipt and not args.vault_url and not args.identity_id:
                result = configure_private_application(credential, client, args.object_id, args.client_id,
                                                       args.origin, args.vault_id, args.receipt)
            elif args.vault_url and not args.vault_id and not args.receipt and not args.identity_id:
                with SecretClient(vault_url=args.vault_url, credential=credential) as vault:
                    result = configure_application(vault, credential, client, args.object_id, args.client_id, args.origin)
            else:
                raise ValueError("Select either private management bootstrap or direct vault access")
        print(json.dumps(result))
    except Exception:
        raise SystemExit("Public bootstrap failed. Check the dedicated app and Key Vault permissions; existing keys were not rotated.") from None


if __name__ == "__main__":
    main()
