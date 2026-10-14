"""Encrypted, versioned records for the public workbench."""

import json
import re
import secrets
from typing import Protocol

from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class StoreConflict(RuntimeError):
    """The record changed since the caller read it."""


class StoreUnavailable(RuntimeError):
    """A record cannot be read or safely persisted."""


class RecordBackend(Protocol):
    def read(self, key: str) -> tuple[bytes, str] | None: ...

    def write(self, key: str, content: bytes, expected_version: str | None = None) -> str: ...

    def delete(self, key: str, expected_version: str) -> None: ...


class BlobBackend:
    def __init__(self, container, *, max_bytes=32 * 1024 * 1024 + 29):
        self.container = container
        self.max_bytes = max_bytes

    def read(self, key):
        try:
            download = self.container.get_blob_client(key).download_blob()
            if download.size > self.max_bytes:
                raise StoreUnavailable("Stored state exceeds the size limit.")
            return download.readall(), download.properties.etag
        except ResourceNotFoundError:
            return None
        except AzureError:
            raise StoreUnavailable("Application storage read failed.") from None

    def write(self, key, content, expected_version=None):
        if len(content) > self.max_bytes:
            raise ValueError("Stored state exceeds the size limit")
        conditions = {}
        if expected_version is not None:
            conditions = {"etag": expected_version, "match_condition": MatchConditions.IfNotModified}
        try:
            response = self.container.get_blob_client(key).upload_blob(
                content, overwrite=expected_version is not None, **conditions
            )
            return response["etag"]
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            raise StoreConflict("Stored state changed.") from None
        except AzureError:
            raise StoreUnavailable("Application storage write failed.") from None

    def delete(self, key, expected_version):
        try:
            self.container.get_blob_client(key).delete_blob(
                etag=expected_version, match_condition=MatchConditions.IfNotModified
            )
        except (ResourceNotFoundError, ResourceModifiedError):
            raise StoreConflict("Stored state changed.") from None
        except AzureError:
            raise StoreUnavailable("Application storage delete failed.") from None


def _record_key(key: str) -> bytes:
    if not isinstance(key, str) or len(key) > 512 or not re.fullmatch(
        r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", key
    ):
        raise ValueError("Invalid state record key")
    return key.encode("ascii")


class EncryptedStore:
    def __init__(self, backend: RecordBackend, encryption_key: bytes, *, max_bytes=8 * 1024 * 1024):
        if not isinstance(encryption_key, bytes) or len(encryption_key) != 32:
            raise ValueError("State encryption requires a 256-bit key")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024:
            raise ValueError("Invalid record size limit")
        self.backend = backend
        self.cipher = AESGCM(encryption_key)
        self.max_bytes = max_bytes

    def read(self, key: str) -> tuple[dict, str] | None:
        associated_data = _record_key(key)
        stored = self.backend.read(key)
        if stored is None:
            return None
        content, version = stored
        try:
            if not 29 <= len(content) <= self.max_bytes + 29 or content[:1] != b"\x01":
                raise ValueError
            plaintext = self.cipher.decrypt(content[1:13], content[13:], associated_data)
            value = json.loads(plaintext)
            if type(value) is not dict:
                raise ValueError
            return value, version
        except (InvalidTag, ValueError, UnicodeError, TypeError, RecursionError):
            raise StoreUnavailable("Encrypted state is unavailable.") from None

    def write(self, key: str, value: dict, expected_version: str | None = None) -> str:
        associated_data = _record_key(key)
        if type(value) is not dict:
            raise ValueError("State records must be objects")
        plaintext = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        if len(plaintext) > self.max_bytes:
            raise ValueError("State record exceeds its size limit")
        nonce = secrets.token_bytes(12)
        content = b"\x01" + nonce + self.cipher.encrypt(nonce, plaintext, associated_data)
        return self.backend.write(key, content, expected_version)

    def delete(self, key: str, expected_version: str) -> None:
        _record_key(key)
        self.backend.delete(key, expected_version)

    def take(self, key: str) -> dict | None:
        stored = self.read(key)
        if stored is None:
            return None
        value, version = stored
        self.delete(key, version)
        return value
