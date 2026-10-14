# Modified for public distribution: installation identifiers replaced with examples.
param(
    [string]$ResourceGroup = 'rg-research-example',
    [string]$Registry = 'researchregistryexample',
    [string]$EnvironmentName = 'research-environment-example',
    [string]$JobName = 'job-research-example',
    [string]$ImageTag = (Get-Date -Format 'yyyyMMddHHmmss'),
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
$parameterPath = $null
try {
    if (-not $env:ALPHA_VANTAGE_API_KEY) {
        throw 'Set ALPHA_VANTAGE_API_KEY in this process before deployment. Do not put it on the command line.'
    }
    $subscription = az account show --query id -o tsv --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Azure login is required.' }
    if (-not $SkipBuild) {
        az acr build --registry $Registry --image "tradingagents-workbench:$ImageTag" --file infra/Dockerfile.workbench --no-logs . --only-show-errors
        if ($LASTEXITCODE -ne 0) { throw 'ACR build failed.' }
    }
    $stateDir = Join-Path $root '.local-run/workbench'
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    $parameterPath = Join-Path $stateDir ('parameters-' + [Guid]::NewGuid() + '.json')
    $parameters = @{
        '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
        contentVersion = '1.0.0.0'
        parameters = @{
            imageTag = @{ value = $ImageTag }
            jobName = @{ value = $JobName }
            environmentName = @{ value = $EnvironmentName }
            registryName = @{ value = $Registry }
            alphaVantageKey = @{ value = $env:ALPHA_VANTAGE_API_KEY }
            fredKey = @{ value = if ($env:FRED_API_KEY) { $env:FRED_API_KEY } else { '' } }
        }
    }
    [IO.File]::WriteAllText($parameterPath, ($parameters | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    if ($IsWindows -or $env:OS -eq 'Windows_NT') {
        $acl = Get-Acl $parameterPath
        $acl.SetAccessRuleProtection($true, $false)
        $rule = [Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.WindowsIdentity]::GetCurrent().Name, 'FullControl', 'Allow')
        $acl.SetAccessRule($rule)
        Set-Acl -Path $parameterPath -AclObject $acl
    }
    $output = az deployment group create --resource-group $ResourceGroup --name 'tradingagents-workbench' --template-file infra/workbench.bicep --parameters "@$parameterPath" --query properties.outputs -o json --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Job deployment failed.' }
    $deployed = $output | ConvertFrom-Json
    $workspaceId = az containerapp env show --name $EnvironmentName --resource-group $ResourceGroup --query properties.appLogsConfiguration.logAnalyticsConfiguration.customerId -o tsv --only-show-errors
    if ($LASTEXITCODE -ne 0 -or -not $workspaceId) { throw 'Log Analytics configuration is required.' }
    $config = @{
        subscription_id = $subscription.Trim()
        resource_group = $ResourceGroup
        job_name = $JobName
        workspace_id = $workspaceId.Trim()
        model = $deployed.model.value
        endpoint = $deployed.endpoint.value
    }
    [IO.File]::WriteAllText((Join-Path $stateDir 'deployment.json'), ($config | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Write-Output "Deployed manual Job: $JobName (image $ImageTag). Local configuration written without secrets."
} finally {
    if ($parameterPath -and (Test-Path $parameterPath)) { Remove-Item -LiteralPath $parameterPath -Force }
    Pop-Location
}