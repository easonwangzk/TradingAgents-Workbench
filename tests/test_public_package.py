import io
import tarfile

import pytest

from tests.test_public_source import source_tree as source_tree


def test_public_context_contains_required_assets_and_pinned_worker_only(source_tree):
    from tradingagents.workbench.public_server import ASSETS
    from tradingagents.workbench.public_source import package_public_source, package_worker_source

    static = source_tree / "tradingagents" / "workbench" / "static"
    static.mkdir()
    (source_tree / "tradingagents" / "workbench" / "email_bootstrap.py").write_text("def main():\n    return None\n", encoding="utf-8")
    for asset in ASSETS | {"index.html"}:
        (static / asset).write_text("public asset", encoding="utf-8")
    (static / "package.json").write_text("private package setting")
    (static / "node_modules").mkdir()
    (static / "node_modules" / "private.js").write_text("private module")
    (source_tree / "infra" / "Dockerfile.public-workbench").write_text("FROM python:3.12-slim\n")
    (source_tree / ".env").write_text("PRIVATE=private-key")
    worker = package_worker_source(source_tree)
    package = package_public_source(source_tree)
    assert package.content == package_public_source(source_tree).content
    with tarfile.open(fileobj=io.BytesIO(package.content), mode="r:gz") as archive:
        names = set(archive.getnames())
        assert "infra/Dockerfile.public-workbench" in names
        assert archive.extractfile("worker-source.tar.gz").read() == worker.content
        assert "tradingagents/workbench/email_bootstrap.py" in names
        assert {"tradingagents/workbench/static/" + asset for asset in ASSETS | {"index.html"}} <= names
        assert not any("node_modules" in name or ".env" in name or "package.json" in name for name in names)
        assert all(b"private-" not in archive.extractfile(member).read() for member in archive.getmembers())


def test_public_context_rejects_missing_assets_without_partial_output(source_tree):
    from tradingagents.workbench.public_source import package_public_source

    with pytest.raises(ValueError):
        package_public_source(source_tree)
