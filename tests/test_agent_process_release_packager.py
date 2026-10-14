import ast
import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / ".local-run" / "public-build" / "agent-process-20260929"
SCRIPT = RUN / "prepare-release.py"


def _load_module(script_path: Path):
    spec = importlib.util.spec_from_file_location("agent_process_prepare_release", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release_module():
    if not SCRIPT.exists():
        pytest.skip("prepare-release.py is not present yet")
    return _load_module(SCRIPT)


def _module_node_map(source: bytes):
    text = source.decode("utf-8")
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    nodes = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    nodes[target.id] = "".join(lines[node.lineno - 1:node.end_lineno])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nodes[node.name] = "".join(lines[node.lineno - 1:node.end_lineno])
    return nodes


def _normalized_snippet(value: str):
    return value.replace("\r\n", "\n")


def _class_method_map(source: bytes, class_name: str):
    text = source.decode("utf-8")
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            result = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result[child.name] = "".join(lines[child.lineno - 1:child.end_lineno])
            return result
    raise KeyError(class_name)


def _archive_members(archive_path: Path):
    with tarfile.open(archive_path, mode="r:gz") as archive:
        return {
            member.name: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile()
        }


def test_check_mode_reports_clean_transplant_and_required_source_maps(release_module, tmp_path):
    result = release_module.prepare(check=True, output_dir=tmp_path)

    assert result["status"] == "ok"
    assert result["prior_receipt_sha256"] == "cbacda85f7a65c058ca86e2c8f21f4dcf6ebed22e7eba28d03b1b17832859998"
    assert result["installed_worker_archive_sha256"] == "115deeb38db27b2bc72f05960019db2149387b7af552f02d47d060a0583c6531"
    assert result["mode"] == "check"
    assert result["blockers"] == []
    assert result["freeze_ready"] is True
    assert result["worker_diff"]["worker_py"]["changed_definitions"] == ["_HostedTelemetryRelay", "main"]
    assert result["worker_diff"]["worker_py"]["unexpected_changed"] == []
    assert result["worker_diff"]["public_worker_py"]["changed_definitions"] == [
        "BACKEND_EVENTS_PATH",
        "HostedWorkerContext",
        "_TelemetryContext",
        "_post_telemetry_events",
        "_send_backend_request",
    ]
    assert result["worker_diff"]["public_worker_py"]["unexpected_changed"] == []
    assert result["worker_diff"]["public_worker_py"]["missing_allowed"] == []
    assert result["worker_diff"]["public_worker_py"]["added_definitions"] == [
        "BACKEND_EVENTS_PATH",
        "_TelemetryContext",
        "_post_telemetry_events",
        "_send_backend_request",
    ]
    assert result["worker_diff"]["public_worker_py"]["removed_definitions"] == []
    assert "source_maps" in result
    assert set(result["source_maps"]) == {"public_context", "worker_context"}
    assert "worker-source.tar.gz" in result["actual_changed_sets"]["outer_overlay"]
    assert result["actual_changed_sets"]["added_public_files"] == ["tradingagents/workbench/static/agent-process.mjs"]
    assert result["actual_changed_sets"]["worker_files"] == sorted(release_module.WORKER_FILES)
    worker_map = result["source_maps"]["worker_context"]
    assert set(worker_map) == set(_archive_members(release_module.INSTALLED_WORKER_ARCHIVE))
    for sha in worker_map.values():
        assert isinstance(sha, str) and len(sha) == 64
    assert not (tmp_path / "public-context").exists()
    assert not (tmp_path / "worker-context").exists()
    assert not (tmp_path / "release-source.json").exists()


def test_ast_guard_reports_unapproved_definition_changes_synthetically(release_module):
    baseline = b"def kept():\n    return 1\n\ndef main():\n    return 2\n"
    current = b"def kept():\n    return 9\n\ndef main():\n    return 2\n\nclass _HostedTelemetryRelay:\n    pass\n"

    result = release_module._assert_only_allowed_segments_changed(
        "tradingagents/workbench/worker.py",
        baseline,
        current,
    )

    assert result["unexpected_changed"] == ["kept"]
    assert result["changed_definitions"] == ["_HostedTelemetryRelay", "kept"]
    assert result["blockers"] == ["worker.py changes exceed approved telemetry transplant"]


def test_check_builds_exact_ast_transplant_against_installed_worker_baseline(release_module, tmp_path):
    result = release_module.prepare(check=True, output_dir=tmp_path)

    worker_archive = tmp_path / "worker-source.tar.gz"
    public_archive = tmp_path / "public-source.tar.gz"
    worker_context = tmp_path / "worker-context"
    public_context = tmp_path / "public-context"

    assert not worker_archive.exists()
    assert not public_archive.exists()
    assert not worker_context.exists()
    assert not public_context.exists()

    transplanted_worker = release_module._build_worker_transplant()
    worker_nodes = {
        relative_path: _module_node_map(content)
        for relative_path, content in transplanted_worker.items()
    }
    baseline_worker = {
        relative_path: (release_module.INSTALLED_WORKER_CONTEXT / relative_path).read_bytes()
        for relative_path in release_module.WORKER_FILES
    }
    baseline_nodes = {
        relative_path: _module_node_map(content)
        for relative_path, content in baseline_worker.items()
    }
    current_nodes = {
        relative_path: _module_node_map((release_module.ROOT / relative_path).read_bytes())
        for relative_path in release_module.WORKER_FILES
    }
    baseline_public_methods = _class_method_map(baseline_worker["tradingagents/workbench/public_worker.py"], "HostedWorkerContext")
    current_public_methods = _class_method_map((release_module.ROOT / "tradingagents/workbench/public_worker.py").read_bytes(), "HostedWorkerContext")
    worker_public_methods = _class_method_map(transplanted_worker["tradingagents/workbench/public_worker.py"], "HostedWorkerContext")

    assert _normalized_snippet(worker_nodes["tradingagents/workbench/worker.py"]["probe_foundry"]) == _normalized_snippet(baseline_nodes["tradingagents/workbench/worker.py"]["probe_foundry"])
    assert _normalized_snippet(worker_nodes["tradingagents/workbench/worker.py"]["_HostedTelemetryRelay"]) == _normalized_snippet(current_nodes["tradingagents/workbench/worker.py"]["_HostedTelemetryRelay"])
    assert _normalized_snippet(worker_nodes["tradingagents/workbench/worker.py"]["main"]) == _normalized_snippet(current_nodes["tradingagents/workbench/worker.py"]["main"])
    assert _normalized_snippet(worker_nodes["tradingagents/workbench/public_worker.py"]["_dispatch_backend_request"]) == _normalized_snippet(baseline_nodes["tradingagents/workbench/public_worker.py"]["_dispatch_backend_request"])
    assert _normalized_snippet(worker_nodes["tradingagents/workbench/public_worker.py"]["BACKEND_EVENTS_PATH"]) == _normalized_snippet(current_nodes["tradingagents/workbench/public_worker.py"]["BACKEND_EVENTS_PATH"])
    assert _normalized_snippet(worker_nodes["tradingagents/workbench/public_worker.py"]["_post_telemetry_events"]) == _normalized_snippet(current_nodes["tradingagents/workbench/public_worker.py"]["_post_telemetry_events"])
    assert _normalized_snippet(worker_nodes["tradingagents/workbench/public_worker.py"]["_send_backend_request"]) == _normalized_snippet(current_nodes["tradingagents/workbench/public_worker.py"]["_send_backend_request"])
    assert _normalized_snippet(worker_nodes["tradingagents/workbench/public_worker.py"]["_TelemetryContext"]) == _normalized_snippet(current_nodes["tradingagents/workbench/public_worker.py"]["_TelemetryContext"])
    assert _normalized_snippet(worker_public_methods["_require_open"]) == _normalized_snippet(current_public_methods["_require_open"])
    assert _normalized_snippet(worker_public_methods["telemetry_context"]) == _normalized_snippet(current_public_methods["telemetry_context"])
    assert _normalized_snippet(worker_public_methods["post_events"]) == _normalized_snippet(current_public_methods["post_events"])
    assert _normalized_snippet(worker_public_methods["__init__"]) == _normalized_snippet(current_public_methods["__init__"])
    assert _normalized_snippet(worker_public_methods["close"]) == _normalized_snippet(current_public_methods["close"])
    assert _normalized_snippet(worker_public_methods["dispatch"]) == _normalized_snippet(baseline_public_methods["dispatch"])
    assert compile(transplanted_worker["tradingagents/workbench/worker.py"], "worker.py", "exec")
    assert compile(transplanted_worker["tradingagents/workbench/public_worker.py"], "public_worker.py", "exec")
    assert result["worker_archive_sha256"] == hashlib.sha256(
        release_module._build_deterministic_tar(release_module._read_archive_members(release_module.INSTALLED_WORKER_ARCHIVE.read_bytes()) | transplanted_worker)
    ).hexdigest()


def test_freeze_refuses_to_write_artifacts_before_parent_release_is_finalized(release_module, tmp_path, monkeypatch):
    monkeypatch.setattr(release_module, "FREEZE_ENABLED", False)
    with pytest.raises(AssertionError, match="freeze is not enabled"):
        release_module.prepare(check=False, output_dir=tmp_path)

    assert not (tmp_path / "public-context").exists()
    assert not (tmp_path / "worker-context").exists()
    assert not (tmp_path / "public-source.tar.gz").exists()
    assert not (tmp_path / "worker-source.tar.gz").exists()
    assert not (tmp_path / "release-source.json").exists()


def test_freeze_rejects_existing_artifacts_when_force_enabled(release_module, tmp_path, monkeypatch):
    monkeypatch.setattr(release_module, "FREEZE_ENABLED", True)
    (tmp_path / "public-context").mkdir(parents=True)

    with pytest.raises(FileExistsError, match="Release artifacts already exist"):
        release_module.prepare(check=False, output_dir=tmp_path)


def test_frozen_contexts_match_archives_and_manifest(release_module, tmp_path, monkeypatch):
    monkeypatch.setattr(release_module, "FREEZE_ENABLED", True)
    release_module.prepare(check=False, output_dir=tmp_path)
    manifest = json.loads((tmp_path / "release-source.json").read_text())
    for kind in ("public", "worker"):
        archive = _archive_members(tmp_path / f"{kind}-source.tar.gz")
        context = {path.relative_to(tmp_path / f"{kind}-context").as_posix(): path.read_bytes()
                   for path in (tmp_path / f"{kind}-context").rglob("*") if path.is_file()}
        assert context == archive
        assert manifest["source_maps"][f"{kind}_context"] == {
            path: hashlib.sha256(content).hexdigest() for path, content in archive.items()
        }


def test_worker_archive_is_deterministic_and_only_worker_files_change(release_module):
    transplanted_worker = release_module._build_worker_transplant()
    baseline_archive = release_module._read_archive_members(release_module.INSTALLED_WORKER_ARCHIVE.read_bytes())
    merged_once = dict(baseline_archive)
    merged_once.update(transplanted_worker)
    merged_twice = dict(baseline_archive)
    merged_twice.update(transplanted_worker)

    archive_once = release_module._build_deterministic_tar(merged_once)
    archive_twice = release_module._build_deterministic_tar(merged_twice)

    assert archive_once == archive_twice
    archive_members = _archive_members(release_module.INSTALLED_WORKER_ARCHIVE)
    assert set(transplanted_worker) == set(release_module.WORKER_FILES)
    changed_members = sorted(
        name for name, content in merged_once.items()
        if baseline_archive.get(name) != content
    )
    assert changed_members == sorted(release_module.WORKER_FILES)
    assert set(archive_members) == set(baseline_archive)
