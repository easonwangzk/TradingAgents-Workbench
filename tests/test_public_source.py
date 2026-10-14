import io
import tarfile

import pytest


@pytest.fixture
def source_tree(tmp_path):
    (tmp_path / "infra").mkdir()
    (tmp_path / "tradingagents" / "workbench").mkdir(parents=True)
    (tmp_path / "cli").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname="worker"\nversion="0.1"\n')
    (tmp_path / "README.md").write_text("Research worker")
    (tmp_path / "LICENSE").write_text("Test license")
    (tmp_path / "infra" / "Dockerfile.workbench").write_text("FROM python:3.12-slim\n")
    (tmp_path / "tradingagents" / "workbench" / "worker.py").write_text("print('worker')\n")
    (tmp_path / "cli" / "stats_handler.py").write_text("class Stats: pass\n")
    return tmp_path


def test_worker_source_is_deterministic_bounded_and_excludes_local_private_data(source_tree):
    from tradingagents.workbench.public_source import package_worker_source

    first = package_worker_source(source_tree)
    (source_tree / ".env").write_text("TOKEN=private-secret")
    (source_tree / "tradingagents" / ".env").write_text("TOKEN=private-secret")
    (source_tree / "tradingagents" / "results").mkdir()
    (source_tree / "tradingagents" / "results" / "private.py").write_text("private-secret")
    (source_tree / "tradingagents" / "private").mkdir()
    (source_tree / "tradingagents" / "private" / "settings.py").write_text("TOKEN='private-secret'")
    (source_tree / ".local-run").mkdir()
    (source_tree / ".local-run" / "report.json").write_text("private-report")
    second = package_worker_source(source_tree)
    assert first.content == second.content
    assert first.sha256 == second.sha256
    with tarfile.open(fileobj=io.BytesIO(first.content), mode="r:gz") as archive:
        assert set(archive.getnames()) == {
            "pyproject.toml", "README.md", "LICENSE", "infra/Dockerfile.workbench",
            "tradingagents/workbench/worker.py", "cli/stats_handler.py",
        }
        assert all(member.isfile() and member.mtime == 0 for member in archive.getmembers())


def test_source_version_changes_when_worker_changes(source_tree):
    from tradingagents.workbench.public_source import package_worker_source

    first = package_worker_source(source_tree)
    (source_tree / "tradingagents" / "workbench" / "worker.py").write_text("print('changed')\n")
    assert package_worker_source(source_tree).sha256 != first.sha256


def test_worker_source_retains_public_license_notices(source_tree):
    from tradingagents.workbench.public_source import package_worker_source

    (source_tree / "NOTICE").write_text("Upstream attribution")
    (source_tree / "THIRD_PARTY_NOTICES.md").write_text("Bundled dependency notices")
    (source_tree / "licenses").mkdir()
    (source_tree / "licenses" / "example-LICENSE").write_text("Dependency license grant")
    (source_tree / "licenses" / ".env").write_text("DO_NOT_PACKAGE=private")

    result = package_worker_source(source_tree)
    with tarfile.open(fileobj=io.BytesIO(result.content), mode="r:gz") as archive:
        assert archive.extractfile("NOTICE").read() == b"Upstream attribution"
        assert archive.extractfile("THIRD_PARTY_NOTICES.md").read() == b"Bundled dependency notices"
        assert archive.extractfile("licenses/example-LICENSE").read() == b"Dependency license grant"
        assert "licenses/.env" not in archive.getnames()


def test_missing_or_oversized_source_is_rejected(source_tree):
    from tradingagents.workbench.public_source import package_worker_source

    with pytest.raises(ValueError):
        package_worker_source(source_tree, max_bytes=10)
    (source_tree / "infra" / "Dockerfile.workbench").unlink()
    with pytest.raises(ValueError):
        package_worker_source(source_tree)
