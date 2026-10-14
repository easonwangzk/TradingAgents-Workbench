import copy

import pytest


class MemoryBackend:
    def __init__(self):
        self.records = {}
        self.revision = 0

    def read(self, key):
        return copy.deepcopy(self.records.get(key))

    def write(self, key, content, expected_version=None):
        from tradingagents.workbench.public_store import StoreConflict

        current = self.records.get(key)
        if (current[1] if current else None) != expected_version:
            raise StoreConflict
        self.revision += 1
        version = str(self.revision)
        self.records[key] = (content, version)
        return version

    def delete(self, key, expected_version):
        from tradingagents.workbench.public_store import StoreConflict

        current = self.records.get(key)
        if not current or current[1] != expected_version:
            raise StoreConflict
        del self.records[key]


def test_encrypted_records_round_trip_without_plaintext_in_backend():
    from tradingagents.workbench.public_store import EncryptedStore

    backend = MemoryBackend()
    store = EncryptedStore(backend, b"a" * 32)
    version = store.write("sessions/alice", {"cache": "private-token-cache"})
    assert b"private-token-cache" not in backend.records["sessions/alice"][0]
    assert store.read("sessions/alice") == ({"cache": "private-token-cache"}, version)
    assert store.read("sessions/bob") is None


def test_ciphertext_cannot_be_moved_to_another_owner_key():
    from tradingagents.workbench.public_store import EncryptedStore, StoreUnavailable

    backend = MemoryBackend()
    store = EncryptedStore(backend, b"a" * 32)
    store.write("users/alice/profile", {"subscription": "alice-only"})
    backend.records["users/bob/profile"] = backend.records["users/alice/profile"]
    with pytest.raises(StoreUnavailable):
        store.read("users/bob/profile")


def test_concurrent_update_and_one_time_consumption_fail_closed():
    from tradingagents.workbench.public_store import EncryptedStore, StoreConflict

    store = EncryptedStore(MemoryBackend(), b"a" * 32)
    initial = store.write("flows/login", {"state": "first"})
    store.write("flows/login", {"state": "second"}, initial)
    with pytest.raises(StoreConflict):
        store.write("flows/login", {"state": "stale"}, initial)
    assert store.take("flows/login") == {"state": "second"}
    assert store.take("flows/login") is None


@pytest.mark.parametrize("key", ["../alice", "/alice", "users//alice", "users/alice?x", ""])
def test_invalid_record_paths_are_rejected(key):
    from tradingagents.workbench.public_store import EncryptedStore

    store = EncryptedStore(MemoryBackend(), b"a" * 32)
    with pytest.raises(ValueError):
        store.write(key, {"value": "not-written"})


def test_nonfinite_and_oversized_data_cannot_be_persisted():
    from tradingagents.workbench.public_store import EncryptedStore

    store = EncryptedStore(MemoryBackend(), b"a" * 32, max_bytes=128)
    with pytest.raises(ValueError):
        store.write("profiles/alice", {"value": float("nan")})
    with pytest.raises(ValueError):
        store.write("profiles/alice", {"value": "x" * 256})
    assert store.read("profiles/alice") is None
