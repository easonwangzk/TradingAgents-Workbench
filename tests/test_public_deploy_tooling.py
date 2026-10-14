# Modified for public distribution: installation identifiers replaced with examples.
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy-public-workbench.ps1"


@pytest.mark.parametrize(
    ("relative", "pattern"),
    [
        ("scripts/deploy-public-workbench.ps1", r"\$workerJobName = '([^']+)'"),
        ("infra/public-worker.bicep", r"param jobName string = '([^']+)'"),
    ],
)
def test_public_worker_job_name_meets_azure_limits(relative, pattern):
    match = re.search(pattern, (ROOT / relative).read_text(encoding="utf-8"))
    assert match is not None
    job_name = match.group(1)
    assert 2 <= len(job_name) <= 32
    assert re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]", job_name)
    assert "--" not in job_name
    assert job_name != "job-research-example"


def _compile_bicep(relative, output_path):
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"az bicep build --file '{_ps_quote(ROOT / relative)}' --outfile '{_ps_quote(output_path)}' --only-show-errors",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(output_path.read_text(encoding="utf-8"))


def _ps_quote(value):
    return str(value).replace("'", "''")


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")


def _output_text(result):
    plain = re.sub(r"\x1b\[[0-9;]*m", "", (result.stderr + result.stdout))
    return " ".join(plain.split())


def _foundation_state(public_origin="https://workbench.example.com"):
    return {
        "registryName": {"value": "researchregistryexample"},
        "registryServer": {"value": "researchregistryexample.azurecr.io"},
        "identityName": {"value": "tradingagents-workbench-host"},
        "identityClientId": {"value": "55555555-5555-4555-8555-555555555555"},
        "environmentName": {"value": "tradingagents-workbench-env-vnet"},
        "vaultUrl": {"value": "https://previous-vault-example.vault.azure.net/"},
        "storageUrl": {"value": "https://previousregistryexample.blob.core.windows.net/"},
        "appName": {"value": "tradingagents-workbench"},
        "publicOrigin": {"value": public_origin},
    }


def _foundation_owner_state():
    return {
        "resource_group_id": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench",
        "deployment_id": "dep-20260911-fixture",
    }


def _email_images_state(public_digest=None, worker_digest=None, generation="build-20260911-1"):
    return {
        "generation": generation,
        "repository": "tradingagents-workbench-worker",
        "public": {
            "digest": public_digest or ("sha256:" + "a" * 64),
            "source_sha256": "b" * 64,
            "archive_sha256": "c" * 64,
            "context_id": "public-context-fixture",
        },
        "worker": {
            "digest": worker_digest or ("sha256:" + "d" * 64),
            "source_sha256": "e" * 64,
            "archive_sha256": "f" * 64,
            "context_id": "worker-context-fixture",
        },
    }


def _worker_receipt_state(images=None, hosted_image=None, job_id=None):
    images = images or _email_images_state()
    return {
        "jobName": {"value": "job-tradingagents-public"},
        "jobId": {
            "value": job_id
            or "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"
        },
        "environmentId": {
            "value": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/managedEnvironments/tradingagents-workbench-env-vnet"
        },
        "workerIdentityId": {
            "value": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker"
        },
        "workspaceId": {"value": "22222222-2222-4222-8222-222222222222"},
        "workspaceResourceId": {
            "value": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.OperationalInsights/workspaces/tradingagents-workbench-logs"
        },
        "hostedImage": {
            "value": hosted_image or f"researchregistryexample.azurecr.io/tradingagents-workbench-worker@{images['worker']['digest']}"
        },
        "customRoleDefinitionId": {
            "value": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.Authorization/roleDefinitions/custom-fixture"
        },
        "location": {"value": "eastus2"},
        "buildReceipt": {"value": images},
    }


def _worker_owner_state(job_id=None):
    return {
        "job_id": job_id
        or "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public",
        "job_name": "job-tradingagents-public",
    }


def _existing_worker_job(image=None, origin="https://workbench.example.com"):
    return {
        "id": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public",
        "location": "East US 2",
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {
                "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker": {}
            },
        },
        "properties": {
            "environmentId": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/managedEnvironments/tradingagents-workbench-env-vnet",
            "workloadProfileName": "Consumption",
            "configuration": {
                "triggerType": "Manual",
                "replicaTimeout": 3600,
                "replicaRetryLimit": 0,
                "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                "registries": [
                    {
                        "server": "researchregistryexample.azurecr.io",
                        "identity": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker",
                        "username": "",
                        "passwordSecretRef": "",
                    }
                ],
                "secrets": None,
                "eventTriggerConfig": None,
                "scheduleTriggerConfig": None,
            },
            "template": {
                "containers": [
                    {
                        "image": image or ("researchregistryexample.azurecr.io/tradingagents-workbench-worker@sha256:" + "d" * 64),
                        "env": [
                            {"name": "TAWB_WORKER_MODE", "value": "hosted"},
                            {"name": "TAWB_BACKEND_BASE_URL", "value": origin},
                        ],
                        "resources": {"cpu": 1, "memory": "2Gi"},
                    }
                ],
                "initContainers": None,
                "volumes": None,
            },
        },
    }


def _published_receipt_state(
    foundation=None,
    images=None,
    worker=None,
    *,
    app_id=None,
    public_image=None,
    revision="tradingagents-workbench--fixture",
    runtime_mode="email",
):
    foundation = foundation or _foundation_state()
    images = images or _email_images_state()
    worker = worker or _worker_receipt_state(images=images)
    host_identity_id = (
        "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/"
        "providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-host"
    )
    environment_id = (
        "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/"
        "providers/Microsoft.App/managedEnvironments/tradingagents-workbench-env-vnet"
    )
    return {
        "runtimeMode": {"value": runtime_mode},
        "publicOrigin": {"value": foundation["publicOrigin"]["value"]},
        "appId": {
            "value": app_id
            or "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/containerApps/tradingagents-workbench"
        },
        "latestRevisionName": {"value": revision},
        "publicImage": {
            "value": public_image
            or f"researchregistryexample.azurecr.io/public-workbench@{images['public']['digest']}"
        },
        "hostedImage": {"value": worker["hostedImage"]["value"]},
        "hostIdentityId": {"value": host_identity_id},
        "hostedWorkerIdentityId": {"value": worker["workerIdentityId"]["value"]},
        "managedEnvironmentId": {"value": environment_id},
        "hostedJobResourceId": {"value": worker["jobId"]["value"]},
        "hostedWorkspaceId": {"value": worker["workspaceId"]["value"]},
        "buildGeneration": {"value": images["generation"]},
        "publicContextId": {"value": images["public"]["context_id"]},
        "workerContextId": {"value": images["worker"]["context_id"]},
    }


def _container_app_projection(
    foundation=None,
    images=None,
    worker=None,
    *,
    app_id=None,
    public_image=None,
    revision="tradingagents-workbench--fixture",
    runtime_mode="email",
    foundry_env=None,
):
    foundation = foundation or _foundation_state()
    images = images or _email_images_state()
    worker = worker or _worker_receipt_state(images=images)
    host_identity_id = (
        "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/"
        "providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-host"
    )
    public_image = public_image or f"researchregistryexample.azurecr.io/public-workbench@{images['public']['digest']}"
    fqdn = foundation["publicOrigin"]["value"].removeprefix("https://")
    env = [
        {"name": "TAWB_PUBLIC_RUNTIME_MODE", "value": runtime_mode},
        {"name": "TAWB_MANAGED_IDENTITY_CLIENT_ID", "value": foundation["identityClientId"]["value"]},
        {"name": "TAWB_PUBLIC_ORIGIN", "value": foundation["publicOrigin"]["value"]},
        {"name": "TAWB_VAULT_URL", "value": foundation["vaultUrl"]["value"].rstrip("/")},
        {"name": "TAWB_STORAGE_ACCOUNT_URL", "value": foundation["storageUrl"]["value"].rstrip("/")},
        {"name": "TAWB_HOSTED_SUBSCRIPTION_ID", "value": "00000000-0000-4000-8000-000000000116"},
        {"name": "TAWB_HOSTED_RESOURCE_GROUP", "value": "rg-research-workbench"},
        {"name": "TAWB_HOSTED_JOB_NAME", "value": "job-tradingagents-public"},
        {"name": "TAWB_HOSTED_JOB_RESOURCE_ID", "value": worker["jobId"]["value"]},
        {"name": "TAWB_HOSTED_ENVIRONMENT_ID", "value": worker["environmentId"]["value"]},
        {"name": "TAWB_HOSTED_WORKER_IDENTITY_ID", "value": worker["workerIdentityId"]["value"]},
        {"name": "TAWB_HOSTED_IMAGE", "value": worker["hostedImage"]["value"]},
        {"name": "TAWB_HOSTED_WORKSPACE_ID", "value": worker["workspaceId"]["value"]},
        {"name": "TAWB_HOSTED_LOCATION", "value": "eastus2"},
    ]
    if foundry_env:
        env.extend(foundry_env)
    return {
        "id": app_id
        or "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/containerApps/tradingagents-workbench",
        "location": "East US 2",
        "tags": {"application": "tradingagents-public-workbench"},
        "identity": {"type": "UserAssigned", "userAssignedIdentities": {host_identity_id: {}}},
        "properties": {
            "managedEnvironmentId": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/managedEnvironments/tradingagents-workbench-env-vnet",
            "workloadProfileName": "Consumption",
            "latestRevisionName": revision,
            "latestReadyRevisionName": revision,
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {
                    "fqdn": fqdn,
                    "external": True,
                    "allowInsecure": False,
                    "targetPort": 8080,
                    "transport": "http",
                },
                "registries": [{"server": foundation["registryServer"]["value"], "identity": host_identity_id}],
            },
            "template": {
                "containers": [{"image": public_image, "resources": {"cpu": 0.5, "memory": "1Gi"}, "env": env}],
                "scale": {"minReplicas": 1, "maxReplicas": 2, "rules": [{"name": "http", "http": {"metadata": {"concurrentRequests": "20"}}}]},
            },
        },
    }


def _run_dispatcher_from_ast(tmp_path, *, phase, runtime_mode):
    calls_path = tmp_path / "phase-calls.json"
    command = """
$ErrorActionPreference = 'Stop'
$scriptPath = '__SCRIPT_PATH__'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) {
    throw ($errors | ForEach-Object Message | Out-String)
}
$functionNames = @(
    'Invoke-Foundation',
    'Invoke-Credentials',
    'Invoke-Build',
    'Invoke-Worker',
    'Invoke-Publish',
    'Invoke-Bootstrap'
)
$functions = foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $functionNames -notcontains $statement.Name) {
        $statement.Extent.Text
    }
}
$tail = foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) {
        $statement.Extent.Text
    }
}
$paramBlock = if ($ast.ParamBlock) { $ast.ParamBlock.Extent.Text } else { '' }
$harness = @(
    $paramBlock,
    '$ErrorActionPreference = ''Stop''',
    'Set-StrictMode -Version Latest',
    '$script:calls = [System.Collections.Generic.List[string]]::new()',
    '$root = Split-Path $PSScriptRoot -Parent',
    '$python = Join-Path $root ''.venv/Scripts/python.exe''',
    '$state = Join-Path $root ''.local-run/public-build''',
    '$subscription = ''00000000-0000-4000-8000-000000000116''',
    '$tenant = ''00000000-0000-4000-8000-000000000105''',
    ($functions -join "`n`n"),
    'function Invoke-Foundation { $script:calls.Add(''Foundation'') }',
    'function Invoke-Credentials { $script:calls.Add(''Credentials'') }',
    'function Invoke-Build { $script:calls.Add(''Build'') }',
    'function Invoke-Worker { $script:calls.Add(''Worker'') }',
    'function Invoke-Publish { $script:calls.Add(''Publish'') }',
    'function Invoke-Bootstrap { $script:calls.Add(''Bootstrap'') }',
    'function Invoke-AzureJson([string[]]$Arguments) { if ($Arguments[0] -eq ''account'' -and $Arguments[1] -eq ''show'') { return @{ id = $subscription; tenantId = $tenant } } throw (''unexpected azure json call: '' + ($Arguments -join '' '')) }',
    'function global:az { param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments) if ($Arguments[0] -eq ''bicep'' -and $Arguments[1] -eq ''build'') { $global:LASTEXITCODE = 0; return } $global:LASTEXITCODE = 1; throw (''unexpected az call: '' + ($Arguments -join '' '')) }',
    ($tail -join "`n`n"),
    '[System.IO.File]::WriteAllText(''__CALLS_PATH__'', ([string[]]$script:calls.ToArray() | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))'
) -join "`n`n"
$harnessPath = Join-Path ([System.IO.Path]::GetDirectoryName($scriptPath)) '__dispatcher-harness.ps1'
[System.IO.File]::WriteAllText($harnessPath, $harness, [System.Text.UTF8Encoding]::new($false))
try {
    & $harnessPath -Phase '__PHASE__' -RuntimeMode '__RUNTIME_MODE__'
} finally {
    Remove-Item -LiteralPath $harnessPath -Force -ErrorAction SilentlyContinue
}
""".replace("__SCRIPT_PATH__", _ps_quote(SCRIPT)).replace("__PHASE__", phase).replace("__RUNTIME_MODE__", runtime_mode).replace("__CALLS_PATH__", _ps_quote(calls_path))
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    calls = json.loads(calls_path.read_text(encoding="utf-8")) if calls_path.exists() else []
    return result, calls


def _run_script(tmp_path, *, phase, runtime_mode, state_files=None, az_body=None, web_body=None, state_dir=None):
    state = state_dir or (tmp_path / "state")
    state.mkdir(exist_ok=True)
    for name, value in (state_files or {}).items():
        _write_json(state / name, value)
    calls_path = tmp_path / "az-calls.txt"
    az_body = az_body or "throw ('unexpected az call: ' + ($Arguments -join ' '))"
    web_body = web_body or "throw 'unexpected web request'"
    command = f"""
$ErrorActionPreference = 'Stop'
function global:az {{
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    Add-Content -LiteralPath '{_ps_quote(calls_path)}' -Value (($Arguments -join ' ') + "`n")
    {az_body}
}}
    function global:Invoke-WebRequest {{
    param(
        [string]$Uri,
        [switch]$SkipHttpErrorCheck,
        [int]$TimeoutSec
    )
    {web_body}
}}
& '{_ps_quote(SCRIPT)}' -Phase '{phase}' -RuntimeMode '{runtime_mode}'
"""
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "TAWB_PUBLIC_BUILD_STATE": str(state)},
        check=False,
    )
    calls = calls_path.read_text(encoding="utf-8") if calls_path.exists() else ""
    return result, calls


def test_public_app_health_probes_send_the_approved_host(tmp_path):
    template = _compile_bicep("infra/public-app.bicep", tmp_path / "public-app.json")
    app = next(resource for resource in template["resources"] if resource["type"] == "Microsoft.App/containerApps")
    probes = app["properties"]["template"]["containers"][0]["probes"]
    assert {probe["type"] for probe in probes} == {"Startup", "Readiness", "Liveness"}
    assert template["parameters"]["environmentName"]["defaultValue"] == "[format('{0}-env-vnet', parameters('appName'))]"
    assert template["parameters"]["publicCompatibleOrigins"]["defaultValue"] == "https://research.example.com"
    expected_host = "[format('{0}.{1}', parameters('appName'), reference(resourceId('Microsoft.App/managedEnvironments', parameters('environmentName')), '2024-03-01').defaultDomain)]"
    for probe in probes:
        assert probe["httpGet"]["path"] == "/healthz"
        assert probe["httpGet"]["port"] == 8080
        headers = probe["httpGet"].get("httpHeaders", [])
        assert len(headers) == 1
        assert headers[0]["name"] == "Host"
        assert headers[0]["value"] == expected_host


def test_public_worker_template_compiles_to_runtime_compatible_outputs_and_verified_actions(tmp_path):
    import tradingagents.workbench.public_runtime as runtime
    from tradingagents.workbench.public_execution import HostedExecution

    template = _compile_bicep("infra/public-worker.bicep", tmp_path / "public-worker.json")

    assert template["variables"]["jobOperatorActions"] == [
        "Microsoft.App/jobs/read",
        "Microsoft.App/jobs/write",
        "Microsoft.App/jobs/start/action",
        "Microsoft.App/jobs/execution/read",
        "Microsoft.App/jobs/executions/read",
        "Microsoft.App/jobs/stop/execution/action",
        "Microsoft.App/jobs/stop/execution/backport/action",
        "Microsoft.App/jobs/listSecrets/action",
    ]
    assert "Microsoft.App/jobs/stop/action" not in template["variables"]["jobOperatorActions"]
    job_resource = next(item for item in template["resources"] if item["type"] == "Microsoft.App/jobs")
    assert job_resource["properties"]["workloadProfileName"] == "Consumption"
    assert template["outputs"]["workspaceId"]["value"] == (
        "[reference(resourceId('Microsoft.OperationalInsights/workspaces', parameters('workspaceName')), '2023-09-01').customerId]"
    )
    assert template["outputs"]["workspaceResourceId"]["value"] == (
        "[resourceId('Microsoft.OperationalInsights/workspaces', parameters('workspaceName'))]"
    )

    environment = {
        "TAWB_HOSTED_SUBSCRIPTION_ID": "11111111-1111-4111-8111-111111111111",
        "TAWB_HOSTED_RESOURCE_GROUP": "hosted-fixture",
        "TAWB_HOSTED_JOB_NAME": "job-hosted-research",
        "TAWB_HOSTED_JOB_RESOURCE_ID": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/hosted-fixture/providers/Microsoft.App/jobs/job-hosted-research",
        "TAWB_HOSTED_ENVIRONMENT_ID": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/hosted-fixture/providers/Microsoft.App/managedEnvironments/hosted-env",
        "TAWB_HOSTED_WORKER_IDENTITY_ID": "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/hosted-fixture/providers/Microsoft.ManagedIdentity/userAssignedIdentities/worker-pull",
        "TAWB_HOSTED_IMAGE": "fixture.azurecr.io/research@sha256:" + "a" * 64,
        "TAWB_HOSTED_WORKSPACE_ID": "22222222-2222-4222-8222-222222222222",
        "TAWB_HOSTED_LOCATION": "eastus2",
    }
    installation = runtime._email_execution_installation(environment, "https://workbench.example.com")
    controller = HostedExecution(
        object(),
        gateway=object(),
        installation=installation,
        credential=SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="fixture-token")),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    assert installation["workspace_id"] == "22222222-2222-4222-8222-222222222222"
    controller.close()


def test_deploy_script_rejects_email_all_before_any_az_call(tmp_path):
    result, calls = _run_script(tmp_path, phase="All", runtime_mode="email")

    assert result.returncode != 0
    assert "email runtime mode" in _output_text(result)
    assert calls == ""


def test_deploy_script_dispatches_legacy_all_without_worker_or_bootstrap(tmp_path):
    result, calls = _run_dispatcher_from_ast(tmp_path, phase="All", runtime_mode="entra")

    assert result.returncode == 0, result.stderr or result.stdout
    assert calls == ["Foundation", "Credentials", "Build", "Publish"]


def test_deploy_script_rejects_publish_when_worker_receipt_does_not_match_build_receipt(tmp_path):
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": {
            "generation": "build-20260911-1",
            "repository": "tradingagents-workbench-worker",
            "public": {"digest": "sha256:" + "a" * 64, "source_sha256": "b" * 64, "archive_sha256": "c" * 64},
            "worker": {"digest": "sha256:" + "d" * 64, "source_sha256": "e" * 64, "archive_sha256": "f" * 64},
        },
        "email-worker.json": {
            "jobName": {"value": "job-tradingagents-public"},
            "jobId": {"value": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"},
            "environmentId": {"value": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/managedEnvironments/tradingagents-workbench-env-vnet"},
            "workerIdentityId": {"value": "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker"},
            "workspaceId": {"value": "22222222-2222-4222-8222-222222222222"},
            "hostedImage": {"value": "researchregistryexample.azurecr.io/tradingagents-workbench-worker@sha256:" + "0" * 64},
            "location": {"value": "eastus2"},
            "buildReceipt": {
                "value": {
                    "generation": "build-20260911-0",
                    "repository": "tradingagents-workbench-worker",
                    "public": {"digest": "sha256:" + "a" * 64, "source_sha256": "b" * 64, "archive_sha256": "c" * 64},
                    "worker": {"digest": "sha256:" + "d" * 64, "source_sha256": "e" * 64, "archive_sha256": "f" * 64},
                }
            },
        },
    }

    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($joined -eq 'account show --only-show-errors --output json') { $global:LASTEXITCODE = 0; '{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}'; return }
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Publish",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode != 0
    assert "build receipt" in _output_text(result).lower()
    assert "deployment group create" not in calls


def test_deploy_script_requires_email_publish_receipt_before_bootstrap(tmp_path):
    images = _email_images_state()
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": _worker_receipt_state(images=images),
    }
    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """
    result, calls = _run_script(tmp_path, phase="Bootstrap", runtime_mode="email", state_files=state_files, az_body=az_body)

    assert result.returncode != 0
    assert "publish" in _output_text(result).lower()
    assert "group show --name rg-research-workbench" in calls
    assert "containerapp exec" not in calls


@pytest.mark.parametrize(
    "foundry_env",
    [
        {"name": "TAWB_FOUNDRY_ENDPOINT", "value": "https://foundry.example.test"},
        {"name": "TAWB_FOUNDRY_FUTURE_SETTING", "value": ""},
        {"name": "TAWB_FOUNDRY_FUTURE_SETTING", "secretRef": "foundry-fixture"},
    ],
    ids=["endpoint", "empty-unknown-setting", "secret-reference"],
)
@pytest.mark.parametrize("container_index", [0, 1], ids=["main-container", "second-container"])
def test_deploy_script_publish_rejects_existing_foundry_env_before_intent_or_cloud_writes(tmp_path, foundry_env, container_index):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    receipt = _published_receipt_state(foundation=foundation, images=images, worker=worker)
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-worker-owner.json": _worker_owner_state(),
        "email-public.json": receipt,
        "email-public.receipt.json": {"intent": {"old": True}, "result": receipt},
    }
    app = _container_app_projection(foundation=foundation, images=images, worker=worker)
    containers = app["properties"]["template"]["containers"]
    if container_index:
        containers.append({"image": containers[0]["image"], "env": []})
    containers[container_index]["env"].append(foundry_env)
    job = _existing_worker_job(image=worker["hostedImage"]["value"], origin=foundation["publicOrigin"]["value"])
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') {{ $global:LASTEXITCODE = 0; '[{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"}}]'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/jobs/job-tradingagents-public*') {{ $global:LASTEXITCODE = 0; '{json.dumps(job, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/containerApps/tradingagents-workbench*') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'rest' -and $Arguments[1] -eq '--method') {{ $global:LASTEXITCODE = 0; '{{"value":[{{"properties":{{"status":"Succeeded"}}}}]}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'validate') {{ $global:LASTEXITCODE = 0; '{{}}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(tmp_path, phase="Publish", runtime_mode="email", state_files=state_files, az_body=az_body)

    assert result.returncode != 0
    state = tmp_path / "state"
    assert not (state / "email-public.pending.json").exists()
    assert "deployment group" not in calls
    assert "containerapp exec" not in calls
    assert "secrets" not in calls.lower()
    assert "bicep build" not in calls
    assert f"resource show --ids {app['id']}" in calls
    output = _output_text(result).lower()
    assert "foundry" in output
    assert "approved" in output and "activation" in output and "receipt" in output
    assert "https://foundry.example.test" not in output
    assert "foundry-fixture" not in output
    assert {path.name for path in state.iterdir()} == set(state_files)
    for name, value in state_files.items():
        assert json.loads((state / name).read_text(encoding="utf-8")) == value


def test_deploy_script_publish_health_failure_keeps_pending_and_preserves_existing_success_receipt(tmp_path):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    receipt = _published_receipt_state(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--stale")
    state = tmp_path / "state"
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-worker-owner.json": _worker_owner_state(),
        "email-public.json": receipt,
        "email-public.receipt.json": {"intent": {"old": True}, "completed_at": "2026-09-11T00:00:00Z", "result": {"publicOrigin": {"value": foundation["publicOrigin"]["value"]}}},
    }
    job = _existing_worker_job(image=worker["hostedImage"]["value"], origin=foundation["publicOrigin"]["value"])
    app = _container_app_projection(foundation=foundation, images=images, worker=worker)
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') {{ $global:LASTEXITCODE = 0; '[{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"}}]'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/jobs/job-tradingagents-public*') {{ $global:LASTEXITCODE = 0; '{json.dumps(job, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/containerApps/tradingagents-workbench*') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'rest' -and $Arguments[1] -eq '--method') {{ $global:LASTEXITCODE = 0; '{{"value":[{{"properties":{{"status":"Succeeded"}}}}]}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'validate') {{ $global:LASTEXITCODE = 0; '{{}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'create') {{ $global:LASTEXITCODE = 0; '{{"publicOrigin":{{"value":"https://workbench.example.com"}},"appId":{{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/containerApps/tradingagents-workbench"}}}}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """
    web_body = """
    if ($Uri -like 'https://workbench.example.com/healthz') { return [pscustomobject]@{ StatusCode = 503; Content = '{"status":"down"}' } }
    throw 'unexpected web request'
    """

    result, calls = _run_script(
        tmp_path,
        phase="Publish",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
        web_body=web_body,
        state_dir=state,
    )

    assert result.returncode != 0
    assert "not healthy" in _output_text(result).lower()
    assert (state / "email-public.pending.json").exists()
    assert json.loads((state / "email-public.json").read_text(encoding="utf-8")) == receipt
    stale_success = json.loads((state / "email-public.receipt.json").read_text(encoding="utf-8"))
    assert stale_success["intent"]["old"] is True
    assert "deployment group create" in calls

    bootstrap_result, bootstrap_calls = _run_script(
        tmp_path,
        phase="Bootstrap",
        runtime_mode="email",
        az_body=az_body,
        state_dir=state,
    )

    assert bootstrap_result.returncode != 0
    assert "pending" in _output_text(bootstrap_result).lower()
    assert "containerapp exec" not in bootstrap_calls


def test_deploy_script_publish_anonymous_guard_keeps_pending_without_new_success_receipt(tmp_path):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-worker-owner.json": _worker_owner_state(),
    }
    job = _existing_worker_job(image=worker["hostedImage"]["value"], origin=foundation["publicOrigin"]["value"])
    app = _container_app_projection(foundation=foundation, images=images, worker=worker)
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') {{ $global:LASTEXITCODE = 0; '[{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"}}]'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/jobs/job-tradingagents-public*') {{ $global:LASTEXITCODE = 0; '{json.dumps(job, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/containerApps/tradingagents-workbench*') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'rest' -and $Arguments[1] -eq '--method') {{ $global:LASTEXITCODE = 0; '{{"value":[{{"properties":{{"status":"Succeeded"}}}}]}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'validate') {{ $global:LASTEXITCODE = 0; '{{}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'create') {{ $global:LASTEXITCODE = 0; '{{"publicOrigin":{{"value":"https://workbench.example.com"}},"appId":{{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/containerApps/tradingagents-workbench"}}}}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """
    web_body = """
    if ($Uri -like 'https://workbench.example.com/healthz') { return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' } }
    if ($Uri -like 'https://workbench.example.com/api/me') { return [pscustomobject]@{ StatusCode = 200; Content = '{}' } }
    throw 'unexpected web request'
    """

    result, calls = _run_script(tmp_path, phase="Publish", runtime_mode="email", state_files=state_files, az_body=az_body, web_body=web_body)

    assert result.returncode != 0
    assert "anonymous api isolation failed" in _output_text(result).lower()
    assert (tmp_path / "state" / "email-public.pending.json").exists()
    assert not (tmp_path / "state" / "email-public.receipt.json").exists()
    assert not (tmp_path / "state" / "email-public.json").exists()
    assert "deployment group create" in calls


def test_deploy_script_publish_timeout_keeps_pending_without_new_success_receipt(tmp_path):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-worker-owner.json": _worker_owner_state(),
    }
    job = _existing_worker_job(image=worker["hostedImage"]["value"], origin=foundation["publicOrigin"]["value"])
    app = _container_app_projection(foundation=foundation, images=images, worker=worker)
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') {{ $global:LASTEXITCODE = 0; '[{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"}}]'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/jobs/job-tradingagents-public*') {{ $global:LASTEXITCODE = 0; '{json.dumps(job, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/containerApps/tradingagents-workbench*') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'rest' -and $Arguments[1] -eq '--method') {{ $global:LASTEXITCODE = 0; '{{"value":[{{"properties":{{"status":"Succeeded"}}}}]}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'validate') {{ $global:LASTEXITCODE = 0; '{{}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'create') {{ $global:LASTEXITCODE = 0; '{{"publicOrigin":{{"value":"https://workbench.example.com"}},"appId":{{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/containerApps/tradingagents-workbench"}}}}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """
    web_body = """
    if ($Uri -like 'https://workbench.example.com/healthz') { throw 'simulated timeout' }
    throw 'unexpected web request'
    """

    result, calls = _run_script(tmp_path, phase="Publish", runtime_mode="email", state_files=state_files, az_body=az_body, web_body=web_body)

    assert result.returncode != 0
    assert "simulated timeout" in _output_text(result).lower()
    assert (tmp_path / "state" / "email-public.pending.json").exists()
    assert not (tmp_path / "state" / "email-public.receipt.json").exists()
    assert not (tmp_path / "state" / "email-public.json").exists()
    assert "deployment group create" in calls


@pytest.mark.parametrize("wrong_output_origin", [False, True])
@pytest.mark.parametrize("omitted_registry_credentials", [False, True])
def test_deploy_script_publish_success_writes_verified_receipt_and_clears_pending(tmp_path, wrong_output_origin, omitted_registry_credentials):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-worker-owner.json": _worker_owner_state(),
    }
    job = _existing_worker_job(image=worker["hostedImage"]["value"], origin=foundation["publicOrigin"]["value"])
    if omitted_registry_credentials:
        registry = job["properties"]["configuration"]["registries"][0]
        del registry["username"]
        del registry["passwordSecretRef"]
    app = _container_app_projection(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--verified")
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') {{ $global:LASTEXITCODE = 0; '[{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"}}]'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/jobs/job-tradingagents-public*') {{ $global:LASTEXITCODE = 0; '{json.dumps(job, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show' -and $joined -like '*providers/Microsoft.App/containerApps/tradingagents-workbench*') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'rest' -and $Arguments[1] -eq '--method') {{ $global:LASTEXITCODE = 0; '{{"value":[{{"properties":{{"status":"Succeeded"}}}}]}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'validate') {{ $global:LASTEXITCODE = 0; '{{}}'; return }}
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'create') {{ $global:LASTEXITCODE = 0; '{{"publicOrigin":{{"value":"https://workbench.example.com"}},"appId":{{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/containerApps/tradingagents-workbench"}}}}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """
    web_body = """
    if ($Uri -like 'https://workbench.example.com/healthz') { return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' } }
    if ($Uri -like 'https://workbench.example.com/api/me') { return [pscustomobject]@{ StatusCode = 401; Content = '{}' } }
    throw 'unexpected web request'
    """
    if wrong_output_origin:
        az_body = az_body.replace('"publicOrigin":{"value":"https://workbench.example.com"}',
                                  '"publicOrigin":{"value":"https://other.example.com"}')
        web_body = web_body.replace("workbench.example.com", "other.example.com")

    result, calls = _run_script(tmp_path, phase="Publish", runtime_mode="email", state_files=state_files, az_body=az_body, web_body=web_body)

    if wrong_output_origin:
        assert result.returncode != 0
        assert "origin" in _output_text(result).lower()
        assert (tmp_path / "state" / "email-public.pending.json").exists()
        assert not (tmp_path / "state" / "email-public.json").exists()
        return
    assert result.returncode == 0, result.stderr or result.stdout
    assert not (tmp_path / "state" / "email-public.pending.json").exists()
    receipt = json.loads((tmp_path / "state" / "email-public.json").read_text(encoding="utf-8"))
    assert receipt["latestRevisionName"]["value"] == "tradingagents-workbench--verified"
    assert receipt["publicImage"]["value"] == f"researchregistryexample.azurecr.io/public-workbench@{images['public']['digest']}"
    assert receipt["hostedImage"]["value"] == worker["hostedImage"]["value"]
    assert receipt["buildGeneration"]["value"] == images["generation"]
    success_receipt = json.loads((tmp_path / "state" / "email-public.receipt.json").read_text(encoding="utf-8"))
    assert success_receipt["result"]["latestRevisionName"]["value"] == "tradingagents-workbench--verified"
    assert "Public endpoint: https://workbench.example.com" in result.stdout
    assert "resource show --ids /subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/containerApps/tradingagents-workbench --api-version 2024-03-01 --query" in calls


def test_deploy_script_bootstrap_executes_exact_safe_command_once_for_verified_email_publish(tmp_path):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    receipt = _published_receipt_state(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--verified")
    app = _container_app_projection(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--verified")
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-public.json": receipt,
    }
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    if ($Arguments[0] -eq 'containerapp' -and $Arguments[1] -eq 'exec') {{ $global:LASTEXITCODE = 0; 'bootstrap ok'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(tmp_path, phase="Bootstrap", runtime_mode="email", state_files=state_files, az_body=az_body)

    assert result.returncode == 0, result.stderr or result.stdout
    assert calls.count("containerapp exec --name tradingagents-workbench --resource-group rg-research-workbench --subscription 00000000-0000-4000-8000-000000000116 --command python -m tradingagents.workbench.email_bootstrap") == 1
    assert "secrets" not in calls.lower()
    assert "password" not in calls.lower()


def test_deploy_script_bootstrap_rejects_receipt_revision_mismatch_without_exec(tmp_path):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    receipt = _published_receipt_state(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--stale")
    app = _container_app_projection(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--verified")
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-public.json": receipt,
    }
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(tmp_path, phase="Bootstrap", runtime_mode="email", state_files=state_files, az_body=az_body)

    assert result.returncode != 0
    assert "revision" in _output_text(result).lower()
    assert "containerapp exec" not in calls


def test_deploy_script_bootstrap_rejects_runtime_image_and_paid_mode_drift_without_exec(tmp_path):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    receipt = _published_receipt_state(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--verified")
    app = _container_app_projection(
        foundation=foundation,
        images=images,
        worker=worker,
        revision="tradingagents-workbench--verified",
        runtime_mode="entra",
        public_image="researchregistryexample.azurecr.io/public-workbench@sha256:" + "0" * 64,
        foundry_env=[{"name": "TAWB_FOUNDRY_ENDPOINT", "value": "https://foundry.example.test"}],
    )
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-public.json": receipt,
    }
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(tmp_path, phase="Bootstrap", runtime_mode="email", state_files=state_files, az_body=az_body)

    assert result.returncode != 0
    output = _output_text(result).lower()
    assert "runtime" in output or "paid" in output or "image" in output
    assert "containerapp exec" not in calls


def test_deploy_script_bootstrap_rejects_owner_and_resource_drift_without_exec(tmp_path):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    receipt = _published_receipt_state(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--verified")
    app = _container_app_projection(foundation=foundation, images=images, worker=worker, revision="tradingagents-workbench--verified")
    app["identity"]["userAssignedIdentities"] = {
        "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/not-approved": {}
    }
    app["properties"]["template"]["containers"][0]["resources"] = {"cpu": 1, "memory": "1Gi"}
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-public.json": receipt,
    }
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{json.dumps(app, ensure_ascii=True)}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(tmp_path, phase="Bootstrap", runtime_mode="email", state_files=state_files, az_body=az_body)

    assert result.returncode != 0
    output = _output_text(result).lower()
    assert "approved fixed" in output and "installation" in output
    assert "containerapp exec" not in calls


def test_deploy_script_bootstrap_rejects_incomplete_receipt_with_controlled_message(tmp_path):
    images = _email_images_state()
    worker = _worker_receipt_state(images=images)
    foundation = _foundation_state()
    state_files = {
        "foundation.json": foundation,
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker,
        "email-public.json": {"runtimeMode": {"value": "email"}, "publicOrigin": {"value": foundation["publicOrigin"]["value"]}},
    }
    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """
    result, calls = _run_script(tmp_path, phase="Bootstrap", runtime_mode="email", state_files=state_files, az_body=az_body)

    assert result.returncode != 0
    assert "publish receipt" in _output_text(result).lower()
    assert "containerapp exec" not in calls


def test_deploy_script_worker_phase_allows_first_email_worker_create_with_empty_job_discovery(tmp_path):
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": _email_images_state(),
    }
    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($joined -eq 'account show --only-show-errors --output json') { $global:LASTEXITCODE = 0; '{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}'; return }
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list' -and $Arguments -contains 'Microsoft.App/jobs') { $global:LASTEXITCODE = 0; '[]'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list' -and $Arguments -contains 'Microsoft.ManagedIdentity/userAssignedIdentities') { $global:LASTEXITCODE = 0; '[]'; return }
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'validate') { $global:LASTEXITCODE = 0; '{}'; return }
    if ($Arguments[0] -eq 'deployment' -and $Arguments[1] -eq 'group' -and $Arguments[2] -eq 'create') { $global:LASTEXITCODE = 0; '{"jobId":{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"},"jobName":{"value":"job-tradingagents-public"},"environmentId":{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/managedEnvironments/tradingagents-workbench-env-vnet"},"workerIdentityId":{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker"},"workspaceId":{"value":"22222222-2222-4222-8222-222222222222"},"workspaceResourceId":{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.OperationalInsights/workspaces/tradingagents-workbench-logs"},"hostedImage":{"value":"researchregistryexample.azurecr.io/tradingagents-workbench-worker@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"},"customRoleDefinitionId":{"value":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.Authorization/roleDefinitions/custom-fixture"}}'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Worker",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "Email worker job ready" in result.stdout
    worker_receipt = json.loads((tmp_path / "state" / "email-worker.json").read_text(encoding="utf-8"))
    assert worker_receipt["jobName"]["value"] == "job-tradingagents-public"
    assert worker_receipt["hostedImage"]["value"] == "researchregistryexample.azurecr.io/tradingagents-workbench-worker@sha256:" + "d" * 64
    assert "resource list --resource-group rg-research-workbench --subscription 00000000-0000-4000-8000-000000000116 --resource-type Microsoft.App/jobs --name job-tradingagents-public" in calls
    assert "deployment group create" in calls


def test_deploy_script_rejects_unexpected_existing_worker_job_id_without_mutation(tmp_path):
    images = _email_images_state()
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker-owner.json": _worker_owner_state(
            "/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/not-the-approved-job"
        ),
    }
    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($joined -eq 'account show --only-show-errors --output json') { $global:LASTEXITCODE = 0; '{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}'; return }
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') { $global:LASTEXITCODE = 0; '[{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/not-the-approved-job"}]'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Worker",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode != 0
    assert "approved worker job id" in _output_text(result).lower()
    assert "deployment group create" not in calls


def test_deploy_script_rejects_paginated_worker_execution_listing(tmp_path):
    images = _email_images_state()
    worker_receipt = _worker_receipt_state(images=images)
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": worker_receipt,
        "email-worker-owner.json": _worker_owner_state(),
    }
    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($joined -eq 'account show --only-show-errors --output json') { $global:LASTEXITCODE = 0; '{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}'; return }
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') { $global:LASTEXITCODE = 0; '[{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"}]'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public","location":"East US 2","identity":{"type":"UserAssigned","userAssignedIdentities":{"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker":{}}},"properties":{"environmentId":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/managedEnvironments/tradingagents-workbench-env-vnet","workloadProfileName":"Consumption","configuration":{"triggerType":"Manual","replicaTimeout":3600,"replicaRetryLimit":0,"manualTriggerConfig":{"parallelism":1,"replicaCompletionCount":1},"registries":[{"server":"researchregistryexample.azurecr.io","identity":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker","username":"","passwordSecretRef":""}],"secrets":[],"eventTriggerConfig":null,"scheduleTriggerConfig":null},"template":{"containers":[{"image":"researchregistryexample.azurecr.io/tradingagents-workbench-worker@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","env":[{"name":"TAWB_WORKER_MODE","value":"hosted"},{"name":"TAWB_BACKEND_BASE_URL","value":"https://workbench.example.com"}],"resources":{"cpu":1,"memory":"2Gi"}}],"initContainers":null,"volumes":null}}}'; return }
    if ($Arguments[0] -eq 'rest' -and $Arguments[1] -eq '--method') { $global:LASTEXITCODE = 0; '{"value":[{"properties":{"status":"Succeeded"}}],"nextLink":"https://next.example.test"}'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Publish",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode != 0
    assert "paginated" in _output_text(result).lower()
    assert "deployment group create" not in calls


def test_deploy_script_rejects_wrong_foundation_before_email_build_mutation(tmp_path):
    state_files = {
        "foundation.json": {
            "registryName": {"value": "wrong-registry"},
            "identityName": {"value": "tradingagents-workbench-host"},
            "environmentName": {"value": "tradingagents-workbench-env-vnet"},
            "publicOrigin": {"value": "https://workbench.example.com"},
        },
        "foundation-owner.json": _foundation_owner_state(),
    }
    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($joined -eq 'account show --only-show-errors --output json') { $global:LASTEXITCODE = 0; '{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}'; return }
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Build",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode != 0
    output = _output_text(result).lower()
    assert "approved fixed" in output and "installation" in output
    assert "acr build" not in calls


def test_start_intent_uses_atomic_create_new(tmp_path):
    pending_path = tmp_path / "atomic.pending.json"
    command = """
$ErrorActionPreference = 'Stop'
$scriptPath = '__SCRIPT_PATH__'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) {
    throw ($errors | ForEach-Object Message | Out-String)
}
$wanted = @('ConvertTo-StableJson', 'Get-PendingIntentPath', 'Start-Intent')
$functions = foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $statement.Name) {
        $statement.Extent.Text
    }
}
$harness = @(
    '$ErrorActionPreference = ''Stop''',
    'Set-StrictMode -Version Latest',
    '$state = ''__STATE__''',
    'function Assert-NoPendingIntent([string]$Name, [string]$Message) { }',
    ($functions -join "`n`n"),
    'Start-Intent ''atomic'' @{ attempt = 1 }',
    'Start-Intent ''atomic'' @{ attempt = 2 }'
) -join "`n`n"
$harnessPath = Join-Path '__STATE__' '__start-intent-harness.ps1'
[System.IO.File]::WriteAllText($harnessPath, $harness, [System.Text.UTF8Encoding]::new($false))
try {
    & $harnessPath
} finally {
    Remove-Item -LiteralPath $harnessPath -Force -ErrorAction SilentlyContinue
}
""".replace("__SCRIPT_PATH__", _ps_quote(SCRIPT)).replace("__STATE__", _ps_quote(tmp_path))
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode != 0
    assert pending_path.exists()


def test_deploy_script_rejects_publish_when_worker_receipt_hosted_image_is_stale(tmp_path):
    images = _email_images_state()
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": _worker_receipt_state(
            images=images,
            hosted_image="researchregistryexample.azurecr.io/tradingagents-workbench-worker@sha256:" + "0" * 64,
        ),
    }

    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($joined -eq 'account show --only-show-errors --output json') { $global:LASTEXITCODE = 0; '{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}'; return }
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Publish",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode != 0
    assert "worker deployment receipt is incomplete" in _output_text(result).lower()
    assert "deployment group create" not in calls
    assert "acr build" not in calls


def test_deploy_script_rejects_existing_worker_identity_collision_before_first_create(tmp_path):
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": _email_images_state(),
    }
    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($joined -eq 'account show --only-show-errors --output json') { $global:LASTEXITCODE = 0; '{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}'; return }
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list' -and $Arguments -contains 'Microsoft.App/jobs') { $global:LASTEXITCODE = 0; '[]'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list' -and $Arguments -contains 'Microsoft.ManagedIdentity/userAssignedIdentities') { $global:LASTEXITCODE = 0; '[{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker"}]'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Worker",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode != 0
    output = _output_text(result).lower()
    assert "worker identity" in output and "approved fixed" in output and "installation" in output
    assert "deployment group create" not in calls


def test_deploy_script_rejects_malformed_worker_execution_listing(tmp_path):
    images = _email_images_state()
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": _worker_receipt_state(images=images),
        "email-worker-owner.json": _worker_owner_state(),
    }
    az_body = """
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') { $global:LASTEXITCODE = 0; return }
    if ($joined -eq 'account show --only-show-errors --output json') { $global:LASTEXITCODE = 0; '{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}'; return }
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') { $global:LASTEXITCODE = 0; '[{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"}]'; return }
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show') { $global:LASTEXITCODE = 0; '{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public","location":"East US 2","identity":{"type":"UserAssigned","userAssignedIdentities":{"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker":{}}},"properties":{"environmentId":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/managedEnvironments/tradingagents-workbench-env-vnet","workloadProfileName":"Consumption","configuration":{"triggerType":"Manual","replicaTimeout":3600,"replicaRetryLimit":0,"manualTriggerConfig":{"parallelism":1,"replicaCompletionCount":1},"registries":[{"server":"researchregistryexample.azurecr.io","identity":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.ManagedIdentity/userAssignedIdentities/tradingagents-workbench-worker","username":"","passwordSecretRef":""}],"secrets":[],"eventTriggerConfig":null,"scheduleTriggerConfig":null},"template":{"containers":[{"image":"researchregistryexample.azurecr.io/tradingagents-workbench-worker@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","env":[{"name":"TAWB_WORKER_MODE","value":"hosted"},{"name":"TAWB_BACKEND_BASE_URL","value":"https://workbench.example.com"}],"resources":{"cpu":1,"memory":"2Gi"}}],"initContainers":null,"volumes":null}}}'; return }
    if ($Arguments[0] -eq 'rest' -and $Arguments[1] -eq '--method') { $global:LASTEXITCODE = 0; '{"items":[{"properties":{"status":"Succeeded"}}]}'; return }
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Publish",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode != 0
    output = _output_text(result).lower()
    assert "could not be validated" in output and "safely" in output
    assert "deployment group create" not in calls


@pytest.mark.parametrize("drift", ["resources", "secrets"])
def test_deploy_script_rejects_worker_job_resource_and_volume_drift(tmp_path, drift):
    images = _email_images_state()
    state_files = {
        "foundation.json": _foundation_state(),
        "foundation-owner.json": _foundation_owner_state(),
        "email-images.json": images,
        "email-worker.json": _worker_receipt_state(images=images),
        "email-worker-owner.json": _worker_owner_state(),
    }
    job = _existing_worker_job(origin="https://workbench.example.com")
    if drift == "secrets":
        job["properties"]["configuration"]["secrets"] = [{"name": "tawb-run-capability"}]
    else:
        job["properties"]["template"]["containers"][0]["resources"] = {"cpu": 2, "memory": "2Gi"}
        job["properties"]["template"]["volumes"] = [{"name": "cache"}]
    az_body = f"""
    $joined = $Arguments -join ' '
    if ($Arguments[0] -eq 'bicep' -and $Arguments[1] -eq 'build') {{ $global:LASTEXITCODE = 0; return }}
    if ($joined -eq 'account show --only-show-errors --output json') {{ $global:LASTEXITCODE = 0; '{{"id":"00000000-0000-4000-8000-000000000116","tenantId":"00000000-0000-4000-8000-000000000105"}}'; return }}
    if ($Arguments[0] -eq 'group' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench","tags":{{"application":"tradingagents-public-workbench","tawbDeployment":"dep-20260911-fixture"}}}}'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'list') {{ $global:LASTEXITCODE = 0; '[{{"id":"/subscriptions/00000000-0000-4000-8000-000000000116/resourceGroups/rg-research-workbench/providers/Microsoft.App/jobs/job-tradingagents-public"}}]'; return }}
    if ($Arguments[0] -eq 'resource' -and $Arguments[1] -eq 'show') {{ $global:LASTEXITCODE = 0; '{json.dumps(job, ensure_ascii=True)}'; return }}
    $global:LASTEXITCODE = 1
    throw ('unexpected az call: ' + $joined)
    """

    result, calls = _run_script(
        tmp_path,
        phase="Publish",
        runtime_mode="email",
        state_files=state_files,
        az_body=az_body,
    )

    assert result.returncode != 0
    output = _output_text(result).lower()
    if drift == "secrets":
        assert "contains secrets" in output
    else:
        assert "approved fixed" in output and "installation" in output
    assert "deployment group create" not in calls
