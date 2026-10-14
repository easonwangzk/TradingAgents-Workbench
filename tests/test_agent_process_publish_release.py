import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".local-run" / "public-build" / "agent-process-20260929" / "publish-release.ps1"
RUN_ID = "agent-process-20260929"
pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file()
    or shutil.which("pwsh") is None
    or "<# Retired draft; never executed." in SCRIPT.read_text(encoding="utf-8"),
    reason="Requires the original sandboxable release draft and PowerShell; the installed publisher is not a test fixture",
)


def _run_script(tmp_path, *, phase=None, offline=False, run_files=None):
    state = tmp_path / "state"
    run = state / RUN_ID
    run.mkdir(parents=True, exist_ok=True)
    for relative, value in (run_files or {}).items():
        target = run / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding="utf-8")
    command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)]
    if phase:
        command.extend(["-Phase", phase])
    if offline:
        command.append("-OfflineContractCheck")
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "TAWB_PUBLIC_BUILD_STATE": str(state),
            "TAWB_AZURE_PYTHON": str(tmp_path / "missing-azure-python.exe"),
        },
        check=False,
    )


def test_offline_contract_check_reports_exact_three_changes_and_no_env_mutation(tmp_path):
    result = _run_script(tmp_path, offline=True)

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "OfflineContractReady"
    assert payload["changed_fields"] == [
        "properties.template.containers[0].image",
        "properties.template.containers[0].env.TAWB_HOSTED_IMAGE",
        "properties.template.containers[0].env.TAWB_HOSTED_PREVIOUS_IMAGES_JSON",
    ]
    assert payload["unchanged_env_hash_before"] == payload["unchanged_env_hash_after"]
    assert payload["old_worker_image"].endswith("@sha256:1a8d5945df9d8a8600120b63c017ed1995c615f040c46944c014c20743f8186d")
    assert payload["new_worker_image"].endswith("@sha256:" + "b" * 64)
    assert payload["previous_images_json"] == json.dumps([payload["old_worker_image"]], separators=(",", ":"))
    assert "TAWB_HOSTED_PREVIOUS_IMAGES_JSON" in payload["allowed_environment_names"]
    assert payload["public_file_count"] >= 120
    assert payload["worker_file_count"] == 2


def test_offline_contract_check_rejects_empty_existing_build_digest_without_azure(tmp_path):
    run_files = {
        "build-worker.intent.json": {"phase": "build-worker"},
        "build-worker.receipt.json": {
            "status": "Succeeded",
            "phase": "build-worker",
            "digest": "",
            "run_id": "fixture-run",
            "intent_sha256": "x",
        },
    }

    result = _run_script(tmp_path, offline=True, run_files=run_files)

    assert result.returncode != 0
    assert "Build digest differs: build-worker" in (result.stderr or result.stdout)
    assert "missing-azure-python.exe" not in (result.stderr or result.stdout)


def test_publish_rejects_repeat_attempt_before_any_azure_call(tmp_path):
    run_files = {"publish.intent.json": {"phase": "Publish"}}

    result = _run_script(tmp_path, phase="Publish", run_files=run_files)

    assert result.returncode != 0
    assert "Phase already attempted locally: publish" in (result.stderr or result.stdout)
    assert "missing-azure-python.exe" not in (result.stderr or result.stdout)
