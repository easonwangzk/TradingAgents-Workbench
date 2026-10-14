from types import SimpleNamespace

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceModifiedError, ResourceNotFoundError

from tests.test_public_store import MemoryBackend


class BlobContainer:
    def __init__(self):
        self.memory = MemoryBackend()
        self.failure = None
        self.downloads = 0

    def get_blob_client(self, key):
        owner = self

        class Blob:
            def download_blob(self):
                if owner.failure:
                    raise owner.failure
                record = owner.memory.read(key)
                if not record:
                    raise ResourceNotFoundError("missing")
                content, version = record

                def readall():
                    owner.downloads += 1
                    return content

                return SimpleNamespace(size=len(content), properties=SimpleNamespace(etag=version), readall=readall)

            def upload_blob(self, content, *, overwrite=False, etag=None, match_condition=None):
                from tradingagents.workbench.public_store import StoreConflict

                assert bool(overwrite) == bool(etag)
                if overwrite:
                    assert match_condition == MatchConditions.IfNotModified
                try:
                    return {"etag": owner.memory.write(key, content, etag)}
                except StoreConflict:
                    raise ResourceModifiedError("private conflict details") from None

            def delete_blob(self, *, etag, match_condition):
                from tradingagents.workbench.public_store import StoreConflict

                assert match_condition == MatchConditions.IfNotModified
                try:
                    owner.memory.delete(key, etag)
                except StoreConflict:
                    raise ResourceModifiedError("private conflict details") from None

        return Blob()


def test_cloud_state_round_trip_and_conditional_updates():
    from tradingagents.workbench.public_store import BlobBackend, EncryptedStore, StoreConflict

    container = BlobContainer()
    store = EncryptedStore(BlobBackend(container), b"a" * 32)
    version = store.write("users/alice", {"profile": "first"})
    assert store.read("users/alice") == ({"profile": "first"}, version)
    newer = store.write("users/alice", {"profile": "second"}, version)
    with pytest.raises(StoreConflict):
        store.write("users/alice", {"profile": "stale"}, version)
    with pytest.raises(StoreConflict):
        store.delete("users/alice", version)
    store.delete("users/alice", newer)
    assert store.read("users/alice") is None


def test_oversized_cloud_record_is_rejected_before_downloading_body():
    from tradingagents.workbench.public_store import BlobBackend, StoreUnavailable

    container = BlobContainer()
    container.memory.write("users/alice", b"x" * 129)
    with pytest.raises(StoreUnavailable):
        BlobBackend(container, max_bytes=128).read("users/alice")
    assert container.downloads == 0


def test_cloud_storage_errors_do_not_expose_provider_response():
    from tradingagents.workbench.public_store import BlobBackend, StoreUnavailable

    container = BlobContainer()
    container.failure = HttpResponseError("private provider response")
    with pytest.raises(StoreUnavailable) as failure:
        BlobBackend(container).read("users/alice")
    assert "private" not in str(failure.value)
