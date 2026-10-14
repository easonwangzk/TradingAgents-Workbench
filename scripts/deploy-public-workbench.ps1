# Modified for public distribution: installation identifiers replaced with examples.
param(
    [ValidateSet('Validate', 'Foundation', 'Credentials', 'Build', 'Worker', 'Publish', 'Bootstrap', 'All')]
    [string]$Phase = 'Validate',
    [ValidateSet('email', 'entra')]
    [string]$RuntimeMode = 'email'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv/Scripts/python.exe'
$state = if ($env:TAWB_PUBLIC_BUILD_STATE) { $env:TAWB_PUBLIC_BUILD_STATE } else { Join-Path $root '.local-run/public-build' }
$subscription = '00000000-0000-4000-8000-000000000116'
$tenant = '00000000-0000-4000-8000-000000000105'
$clientId = '00000000-0000-4000-8000-000000000130'
$objectId = '00000000-0000-4000-8000-000000000111'
$group = 'rg-research-workbench'
$appName = 'tradingagents-workbench'
$logsName = 'tradingagents-workbench-logs'
$foundryName = 'example-foundry-source'
$foundryGroup = 'rg-research-example'
$workerJobName = 'job-tradingagents-public'
$workerIdentityName = 'tradingagents-workbench-worker'
$workerRepository = 'tradingagents-workbench-worker'
New-Item -ItemType Directory -Path $state -Force | Out-Null

function Get-StatePath([string]$Name) {
    return Join-Path $state $Name
}

function Get-CanonicalResourceGroupId {
    return "/subscriptions/$subscription/resourceGroups/$group"
}

function Get-CanonicalWorkerJobId {
    return "$(Get-CanonicalResourceGroupId)/providers/Microsoft.App/jobs/$workerJobName"
}

function Get-CanonicalWorkerIdentityId {
    return "$(Get-CanonicalResourceGroupId)/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$workerIdentityName"
}

function Get-CanonicalHostIdentityId {
    return "$(Get-CanonicalResourceGroupId)/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$appName-host"
}

function Get-CanonicalContainerAppId {
    return "$(Get-CanonicalResourceGroupId)/providers/Microsoft.App/containerApps/$appName"
}

function Invoke-AzureJson([string[]]$Arguments) {
    $result = & az @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) { throw 'Azure operation failed. No automatic retry was issued.' }
    if ($result) { return ($result | ConvertFrom-Json -Depth 64) }
}

function Test-HexDigest([string]$Value) {
    return $Value -is [string] -and $Value -match '^sha256:[0-9a-f]{64}$'
}

function Test-Hash([string]$Value) {
    return $Value -is [string] -and $Value -match '^[0-9a-f]{64}$'
}

function Test-Uuid([string]$Value) {
    return $Value -is [string] -and $Value -match '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

function ConvertTo-StableJson($Value) {
    return ($Value | ConvertTo-Json -Depth 64 -Compress)
}

function Test-NullOrEmptyCollection($Value) {
    return $null -eq $Value -or @($Value).Count -eq 0
}

function Test-ObjectProperty($Value, [string]$Name) {
    if ($Value -eq $null) { return $false }
    if ($Value -is [System.Collections.IDictionary]) { return $Value.Contains($Name) }
    return $Value.PSObject -ne $null -and ($Value.PSObject.Properties.Name -contains $Name)
}

function Get-PropertyValue($Value, [string]$Name) {
    if (-not (Test-ObjectProperty $Value $Name)) { return $null }
    $property = if ($Value -is [System.Collections.IDictionary]) { $Value[$Name] } else { $Value.$Name }
    if ($property -is [System.Collections.IDictionary] -and $property.Contains('value')) {
        return $property['value']
    }
    if ($property -ne $null -and $property.PSObject -ne $null -and ($property.PSObject.Properties.Name -contains 'value')) {
        return $property.value
    }
    return $property
}

function Get-PendingIntentPath([string]$Name) {
    return Join-Path $state ($Name + '.pending.json')
}

function Assert-NoPendingIntent([string]$Name, [string]$Message) {
    if (Test-Path (Get-PendingIntentPath $Name)) { throw $Message }
}

function Start-Intent([string]$Name, $Value) {
    Assert-NoPendingIntent $Name "A previous $Name operation is still pending. Inspect the local state before retrying."
    $pendingPath = Get-PendingIntentPath $Name
    $encoding = [Text.UTF8Encoding]::new($false)
    $payload = $encoding.GetBytes((ConvertTo-StableJson $Value))
    try {
        $stream = [IO.File]::Open($pendingPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    } catch [IO.IOException] {
        throw "A previous $Name operation is still pending. Inspect the local state before retrying."
    }
    try {
        $stream.Write($payload, 0, $payload.Length)
    } finally {
        $stream.Dispose()
    }
}

function Complete-Intent([string]$Name, $Result) {
    $pendingPath = Get-PendingIntentPath $Name
    if (-not (Test-Path $pendingPath)) { throw "The $Name intent record is missing." }
    $pending = Get-Content -Raw $pendingPath | ConvertFrom-Json -Depth 64
    Save-State ($Name + '.receipt.json') @{ intent = $pending; completed_at = [DateTimeOffset]::UtcNow.ToString('o'); result = $Result }
    Remove-Item $pendingPath -Force
}

function Assert-LegacyPhase([string]$Name) {
    if ($RuntimeMode -ne 'entra') { throw "$Name is restricted to explicit legacy Entra mode." }
}

function Assert-EmailPhase([string]$Name) {
    if ($RuntimeMode -ne 'email') { throw "$Name is only available for the email runtime mode." }
}

function Assert-PhaseSelection {
    if ($Phase -eq 'All' -and $RuntimeMode -eq 'email') {
        throw 'The All phase is unavailable for the email runtime mode. Run Build, Worker, Publish, and Bootstrap explicitly.'
    }
    if ($Phase -in @('Foundation', 'Credentials')) { Assert-LegacyPhase $Phase }
    if ($Phase -in @('Worker', 'Bootstrap')) { Assert-EmailPhase $Phase }
}

function Assert-ImageReceipt([object]$Images) {
    if ($Images -eq $null -or -not ($Images.PSObject.Properties.Name -contains 'generation') -or -not ($Images.PSObject.Properties.Name -contains 'repository')) {
        throw 'The email build receipt is incomplete.'
    }
    foreach ($name in @('public', 'worker')) {
        $entry = $Images.$name
        if ($entry -eq $null -or -not (Test-HexDigest $entry.digest) -or -not (Test-Hash $entry.source_sha256) -or -not (Test-Hash $entry.archive_sha256)) {
            throw 'The email build receipt is incomplete.'
        }
    }
}

function Assert-ApprovedFoundationRecord([object]$Foundation) {
    if ($Foundation -eq $null -or
        $Foundation.registryName.value -ne 'researchregistryexample' -or
        $Foundation.identityName.value -ne 'tradingagents-workbench-host' -or
        $Foundation.environmentName.value -ne 'tradingagents-workbench-env-vnet' -or
        -not ($Foundation.publicOrigin.value -is [string]) -or
        -not $Foundation.publicOrigin.value.StartsWith('https://')) {
        throw 'The saved hosting foundation does not match the approved fixed installation.'
    }
}

function Get-ExpectedPublicImage([object]$Foundation, [object]$Images) {
    return "$($Foundation.registryName.value).azurecr.io/public-workbench@$($Images.public.digest)"
}

function Get-CurrentPublicAppProjection {
    $query = '{id:id,location:location,tags:{application:tags.application},identity:{type:identity.type,userAssignedIdentities:identity.userAssignedIdentities},properties:{managedEnvironmentId:properties.managedEnvironmentId,workloadProfileName:properties.workloadProfileName,latestRevisionName:properties.latestRevisionName,latestReadyRevisionName:properties.latestReadyRevisionName,configuration:{activeRevisionsMode:properties.configuration.activeRevisionsMode,ingress:{fqdn:properties.configuration.ingress.fqdn,external:properties.configuration.ingress.external,allowInsecure:properties.configuration.ingress.allowInsecure,targetPort:properties.configuration.ingress.targetPort,transport:properties.configuration.ingress.transport},registries:properties.configuration.registries[].{server:server,identity:identity}},template:{containers:properties.template.containers[].{image:image,resources:resources,env:env[].{name:name,value:value}},scale:{minReplicas:properties.template.scale.minReplicas,maxReplicas:properties.template.scale.maxReplicas,rules:properties.template.scale.rules[].{name:name,http:http}}}}}'
    return Invoke-AzureJson @('resource', 'show', '--ids', (Get-CanonicalContainerAppId), '--api-version', '2024-03-01', '--query', $query)
}

function New-EmailPublishReceipt([object]$Foundation, [object]$Images, [object]$Worker, [object]$App) {
    return @{
        runtimeMode = @{ value = 'email' }
        publicOrigin = @{ value = "https://$($App.properties.configuration.ingress.fqdn)" }
        appId = @{ value = $App.id }
        latestRevisionName = @{ value = $App.properties.latestRevisionName }
        publicImage = @{ value = $App.properties.template.containers[0].image }
        hostedImage = @{ value = $Worker.hostedImage.value }
        hostIdentityId = @{ value = Get-CanonicalHostIdentityId }
        hostedWorkerIdentityId = @{ value = $Worker.workerIdentityId.value }
        managedEnvironmentId = @{ value = $App.properties.managedEnvironmentId }
        hostedJobResourceId = @{ value = $Worker.jobId.value }
        hostedWorkspaceId = @{ value = $Worker.workspaceId.value }
        buildGeneration = @{ value = $Images.generation }
        publicContextId = @{ value = $Images.public.context_id }
        workerContextId = @{ value = $Images.worker.context_id }
    }
}

function Assert-BootstrapReceipt([object]$Deployment, [object]$Foundation, [object]$Images, [object]$Worker) {
    if ($Deployment -eq $null) { throw 'The saved email publish receipt is incomplete.' }
    $required = @('runtimeMode', 'publicOrigin', 'appId', 'latestRevisionName', 'publicImage', 'hostedImage', 'hostIdentityId', 'hostedWorkerIdentityId', 'managedEnvironmentId', 'hostedJobResourceId', 'hostedWorkspaceId', 'buildGeneration', 'publicContextId', 'workerContextId')
    foreach ($name in $required) {
        if (-not (Test-ObjectProperty $Deployment $name)) { throw 'The saved email publish receipt is incomplete.' }
        $value = Get-PropertyValue $Deployment $name
        if (-not ($value -is [string]) -or [string]::IsNullOrWhiteSpace($value)) { throw 'The saved email publish receipt is incomplete.' }
    }
    $expectedOrigin = $Foundation.publicOrigin.value
    $expectedAppId = Get-CanonicalContainerAppId
    $expectedPublicImage = Get-ExpectedPublicImage $Foundation $Images
    $expectedHostIdentityId = Get-CanonicalHostIdentityId
    $expectedEnvironmentId = "$(Get-CanonicalResourceGroupId)/providers/Microsoft.App/managedEnvironments/$($Foundation.environmentName.value)"
    if ((Get-PropertyValue $Deployment 'runtimeMode') -ne 'email' -or
        (Get-PropertyValue $Deployment 'publicOrigin') -ne $expectedOrigin -or
        (Get-PropertyValue $Deployment 'appId') -ne $expectedAppId -or
        (Get-PropertyValue $Deployment 'publicImage') -ne $expectedPublicImage -or
        (Get-PropertyValue $Deployment 'hostedImage') -ne $Worker.hostedImage.value -or
        (Get-PropertyValue $Deployment 'hostIdentityId') -ne $expectedHostIdentityId -or
        (Get-PropertyValue $Deployment 'hostedWorkerIdentityId') -ne $Worker.workerIdentityId.value -or
        (Get-PropertyValue $Deployment 'managedEnvironmentId') -ne $expectedEnvironmentId -or
        (Get-PropertyValue $Deployment 'hostedJobResourceId') -ne $Worker.jobId.value -or
        (Get-PropertyValue $Deployment 'hostedWorkspaceId') -ne $Worker.workspaceId.value -or
        (Get-PropertyValue $Deployment 'buildGeneration') -ne $Images.generation -or
        (Get-PropertyValue $Deployment 'publicContextId') -ne $Images.public.context_id -or
        (Get-PropertyValue $Deployment 'workerContextId') -ne $Images.worker.context_id) {
        throw 'The saved email publish receipt does not match the current approved build and installation.'
    }
}

function Assert-PublicAppBaseline([object]$App, [object]$Foundation, [object]$Worker, [object]$Deployment) {
    if ($App -eq $null -or -not (Test-ObjectProperty $App 'id') -or -not (Test-ObjectProperty $App 'properties')) {
        throw 'The current public application could not be validated safely.'
    }
    if ($App.id -ne (Get-PropertyValue $Deployment 'appId')) {
        throw 'The current public application does not match the approved fixed installation.'
    }
    if ($App.location -ne 'eastus2' -and $App.location -ne 'East US 2') {
        throw 'The current public application does not match the approved fixed installation.'
    }
    if ($App.tags.application -ne 'tradingagents-public-workbench') {
        throw 'The current public application does not match the approved fixed installation.'
    }
    $hostIdentityId = Get-PropertyValue $Deployment 'hostIdentityId'
    if ($App.identity.type -ne 'UserAssigned' -or @($App.identity.userAssignedIdentities.PSObject.Properties.Name).Count -ne 1 -or
        @($App.identity.userAssignedIdentities.PSObject.Properties.Name)[0].ToLowerInvariant() -ne $hostIdentityId.ToLowerInvariant()) {
        throw 'The current public application does not match the approved fixed installation.'
    }
    if ($App.properties.managedEnvironmentId -ne (Get-PropertyValue $Deployment 'managedEnvironmentId') -or $App.properties.workloadProfileName -ne 'Consumption') {
        throw 'The current public application does not match the approved fixed installation.'
    }
    if ($App.properties.latestRevisionName -ne (Get-PropertyValue $Deployment 'latestRevisionName') -or $App.properties.latestReadyRevisionName -ne (Get-PropertyValue $Deployment 'latestRevisionName')) {
        throw 'The current public application revision does not match the approved email publish receipt.'
    }
    $configuration = $App.properties.configuration
    if ($configuration.activeRevisionsMode -ne 'Single' -or $configuration.ingress.external -ne $true -or
        $configuration.ingress.allowInsecure -ne $false -or $configuration.ingress.targetPort -ne 8080 -or
        $configuration.ingress.transport -ne 'http' -or ("https://$($configuration.ingress.fqdn)") -ne (Get-PropertyValue $Deployment 'publicOrigin')) {
        throw 'The current public application does not match the approved fixed installation.'
    }
    if (@($configuration.registries).Count -ne 1 -or
        $configuration.registries[0].server -ne "$($Foundation.registryName.value).azurecr.io" -or
        $configuration.registries[0].identity.ToLowerInvariant() -ne $hostIdentityId.ToLowerInvariant()) {
        throw 'The current public application does not match the approved fixed installation.'
    }
    $template = $App.properties.template
    if (@($template.containers).Count -ne 1 -or $template.containers[0].image -ne (Get-PropertyValue $Deployment 'publicImage')) {
        throw 'The current public application image does not match the approved email publish receipt.'
    }
    if ($template.containers[0].resources.cpu -ne 0.5 -or $template.containers[0].resources.memory -ne '1Gi') {
        throw 'The current public application does not match the approved fixed installation.'
    }
    if ($template.scale.minReplicas -ne 1 -or $template.scale.maxReplicas -ne 2 -or @($template.scale.rules).Count -ne 1 -or $template.scale.rules[0].name -ne 'http') {
        throw 'The current public application does not match the approved fixed installation.'
    }
    $envMap = @{}
    foreach ($entry in @($template.containers[0].env)) {
        if (-not (Test-ObjectProperty $entry 'name') -or -not (Test-ObjectProperty $entry 'value')) {
            throw 'The current public application could not be validated safely.'
        }
        $envMap[$entry.name] = $entry.value
    }
    if ($envMap.ContainsKey('TAWB_PUBLIC_COMPATIBLE_ORIGINS') -and
        $envMap['TAWB_PUBLIC_COMPATIBLE_ORIGINS'] -cne 'https://research.example.com') {
        throw 'The current public application does not match the approved fixed installation.'
    }
    $expectedEnv = @{
        TAWB_PUBLIC_RUNTIME_MODE = 'email'
        TAWB_MANAGED_IDENTITY_CLIENT_ID = $Foundation.identityClientId.value
        TAWB_PUBLIC_ORIGIN = $Foundation.publicOrigin.value
        TAWB_VAULT_URL = $Foundation.vaultUrl.value.TrimEnd('/')
        TAWB_STORAGE_ACCOUNT_URL = $Foundation.storageUrl.value.TrimEnd('/')
        TAWB_HOSTED_SUBSCRIPTION_ID = $subscription
        TAWB_HOSTED_RESOURCE_GROUP = $group
        TAWB_HOSTED_JOB_NAME = $Worker.jobName.value
        TAWB_HOSTED_JOB_RESOURCE_ID = $Worker.jobId.value
        TAWB_HOSTED_ENVIRONMENT_ID = $Worker.environmentId.value
        TAWB_HOSTED_WORKER_IDENTITY_ID = $Worker.workerIdentityId.value
        TAWB_HOSTED_IMAGE = $Worker.hostedImage.value
        TAWB_HOSTED_WORKSPACE_ID = $Worker.workspaceId.value
        TAWB_HOSTED_LOCATION = 'eastus2'
    }
    foreach ($name in $expectedEnv.Keys) {
        if (-not $envMap.ContainsKey($name) -or $envMap[$name] -ne $expectedEnv[$name]) {
            if ($name -eq 'TAWB_PUBLIC_RUNTIME_MODE') { throw 'The current public application runtime does not match the approved email publish receipt.' }
            if ($name -eq 'TAWB_HOSTED_IMAGE') { throw 'The current public application image does not match the approved email publish receipt.' }
            throw 'The current public application does not match the approved fixed installation.'
        }
    }
    foreach ($name in $envMap.Keys) {
        if ($name -like 'TAWB_FOUNDRY_*') {
            throw 'The current public application paid mode is not approved for bootstrap.'
        }
        if ($name -ne 'TAWB_APPROVED_COMPATIBLE_ORIGINS' -and
            $name -ne 'TAWB_PUBLIC_COMPATIBLE_ORIGINS' -and -not $expectedEnv.ContainsKey($name)) {
            throw 'The current public application does not match the approved fixed installation.'
        }
    }
}

function Get-ApprovedFoundation {
    $foundation = Get-Foundation
    Assert-ApprovedFoundationRecord $foundation
    $ownerPath = Get-StatePath 'foundation-owner.json'
    if (-not (Test-Path $ownerPath)) { throw 'The saved hosting foundation does not match the approved fixed installation.' }
    $owner = Get-Content -Raw $ownerPath | ConvertFrom-Json -Depth 32
    $expectedGroupId = Get-CanonicalResourceGroupId
    if ($owner.resource_group_id -ne $expectedGroupId -or -not ($owner.deployment_id -is [string])) {
        throw 'The saved hosting foundation does not match the approved fixed installation.'
    }
    $existing = Invoke-AzureJson @('group', 'show', '--name', $group, '--subscription', $subscription)
    if ($existing.id -ne $expectedGroupId -or
        $existing.tags.application -ne 'tradingagents-public-workbench' -or
        $existing.tags.tawbDeployment -ne $owner.deployment_id) {
        throw 'The hosting resource group does not match the saved deployment ownership.'
    }
    return $foundation
}

function Assert-WorkerDeploymentReceipt([object]$Foundation, [object]$Images, [object]$Worker) {
    Assert-ImageReceipt $Images
    if ($Worker -eq $null -or -not ($Worker.PSObject.Properties.Name -contains 'buildReceipt')) {
        throw 'The worker deployment receipt is incomplete.'
    }
    $buildReceipt = $Worker.buildReceipt.value
    if ($null -eq $buildReceipt) { $buildReceipt = $Worker.buildReceipt }
    if ($null -eq $buildReceipt) { throw 'The worker deployment receipt is incomplete.' }
    if ((ConvertTo-StableJson $buildReceipt) -ne (ConvertTo-StableJson $Images)) {
        throw 'The worker deployment build receipt does not match the current email build receipt.'
    }
    $expectedJobId = Get-CanonicalWorkerJobId
    $expectedIdentityId = Get-CanonicalWorkerIdentityId
    $expectedEnvironmentId = "$(Get-CanonicalResourceGroupId)/providers/Microsoft.App/managedEnvironments/$($Foundation.environmentName.value)"
    $expectedHostedImage = "$($Foundation.registryName.value).azurecr.io/$workerRepository@$($Images.worker.digest)"
    if ($Worker.jobName.value -ne $workerJobName -or
        $Worker.jobId.value -ne $expectedJobId -or
        $Worker.workerIdentityId.value -ne $expectedIdentityId -or
        $Worker.environmentId.value -ne $expectedEnvironmentId -or
        $Worker.hostedImage.value -ne $expectedHostedImage -or
        $Worker.location.value -notin @('eastus2', 'East US 2') -or
        -not (Test-Uuid $Worker.workspaceId.value) -or
        -not (Test-HexDigest ($buildReceipt.worker.digest))) {
        throw 'The worker deployment receipt is incomplete.'
    }
}

function Assert-LocalPhasePrerequisites {
    if ($Phase -eq 'Build' -and $RuntimeMode -eq 'email') {
        Assert-NoPendingIntent 'email-build' 'The previous email build outcome is uncertain. Inspect the local state before retrying.'
        return
    }
    if ($Phase -eq 'Worker') {
        Assert-NoPendingIntent 'email-worker' 'The previous email worker deployment outcome is uncertain. Inspect the local state before retrying.'
        Assert-NoPendingIntent 'email-build' 'The previous email build outcome is uncertain. Inspect the local state before retrying.'
        Assert-ImageReceipt (Get-EmailImages)
        return
    }
    if ($Phase -eq 'Publish' -and $RuntimeMode -eq 'email') {
        Assert-NoPendingIntent 'email-worker' 'The previous email worker deployment outcome is uncertain. Inspect the local state before retrying.'
        Assert-NoPendingIntent 'email-build' 'The previous email build outcome is uncertain. Inspect the local state before retrying.'
        Assert-NoPendingIntent 'email-public' 'The previous email publish outcome is uncertain. Inspect the local state before retrying.'
        $foundation = Get-ApprovedFoundation
        $images = Get-EmailImages
        $worker = Get-WorkerDeployment
        Assert-WorkerDeploymentReceipt $foundation $images $worker
        $app = Get-CurrentPublicAppProjection
        foreach ($container in @($app.properties.template.containers)) {
            foreach ($entry in @($container.env)) {
                if ($entry.name -like 'TAWB_FOUNDRY_*') {
                    throw 'The current public application has TAWB_FOUNDRY_* settings. Email Publish would disable the owner Foundry configuration. Use the approved owner Foundry activation receipt workflow instead.'
                }
            }
        }
        return
    }
    if ($Phase -eq 'Bootstrap') {
        Assert-NoPendingIntent 'email-build' 'The previous email build outcome is uncertain. Inspect the local state before retrying.'
        Assert-NoPendingIntent 'email-worker' 'The previous email worker deployment outcome is uncertain. Inspect the local state before retrying.'
        Assert-NoPendingIntent 'email-public' 'The previous email publish outcome is uncertain. Inspect the local state before retrying.'
        $foundation = Get-ApprovedFoundation
        $images = Get-EmailImages
        $worker = Get-WorkerDeployment
        Assert-WorkerDeploymentReceipt $foundation $images $worker
        Assert-BootstrapReceipt (Get-State 'email-public.json' 'Complete the email Publish phase first.') $foundation $images $worker
    }
}

function Get-ExistingWorkerJobId {
    $rawMatches = Invoke-AzureJson @('resource', 'list', '--resource-group', $group, '--subscription', $subscription,
        '--resource-type', 'Microsoft.App/jobs', '--name', $workerJobName)
    if ($null -eq $rawMatches) { return $null }
    $matches = @($rawMatches)
    if ($matches.Count -eq 0) { return $null }
    if ($matches.Count -ne 1) { throw 'Existing worker job discovery was ambiguous.' }
    $jobId = $matches[0].id
    if ($jobId -ne (Get-CanonicalWorkerJobId)) { throw 'The existing public worker job did not match the approved worker job id.' }
    return $jobId
}

function Get-ExistingWorkerIdentityId {
    $rawMatches = Invoke-AzureJson @('resource', 'list', '--resource-group', $group, '--subscription', $subscription,
        '--resource-type', 'Microsoft.ManagedIdentity/userAssignedIdentities', '--name', $workerIdentityName)
    if ($null -eq $rawMatches) { return $null }
    $matches = @($rawMatches)
    if ($matches.Count -eq 0) { return $null }
    if ($matches.Count -ne 1) { throw 'Existing worker identity discovery was ambiguous.' }
    $identityId = $matches[0].id
    if ($identityId -ne (Get-CanonicalWorkerIdentityId)) { throw 'The existing public worker identity did not match the approved fixed installation.' }
    return $identityId
}

function Get-WorkerOwnership {
    $path = Get-StatePath 'email-worker-owner.json'
    if (-not (Test-Path $path)) { return $null }
    return Get-Content -Raw $path | ConvertFrom-Json -Depth 64
}

function Get-CurrentWorkerJob {
    $jobId = Get-ExistingWorkerJobId
    if (-not $jobId) { return $null }
    $owner = Get-WorkerOwnership
    if ($null -eq $owner -or $owner.job_id -ne $jobId) {
        throw 'The existing public worker job has no matching local ownership record.'
    }
    return Invoke-AzureJson @('resource', 'show', '--ids', $jobId, '--api-version', '2024-03-01')
}

function Assert-WorkerJobBaseline([object]$Job, [string]$ExpectedImage, [string]$ExpectedIdentityId, [string]$ExpectedEnvironmentId, [string]$ExpectedOrigin) {
    if ($Job -eq $null) { return }
    if ($Job.location -ne 'eastus2' -and $Job.location -ne 'East US 2') { throw 'The existing public worker job location does not match the approved fixed installation.' }
    if ($Job.properties.environmentId -ne $ExpectedEnvironmentId -or $Job.properties.workloadProfileName -ne 'Consumption') {
        throw 'The existing public worker job does not match the approved fixed installation.'
    }
    if ($Job.identity.type -ne 'UserAssigned' -or @($Job.identity.userAssignedIdentities.PSObject.Properties.Name).Count -ne 1 -or
        @($Job.identity.userAssignedIdentities.PSObject.Properties.Name)[0].ToLowerInvariant() -ne $ExpectedIdentityId.ToLowerInvariant()) {
        throw 'The existing public worker job identity does not match the approved fixed installation.'
    }
    $configuration = $Job.properties.configuration
    if ($configuration.triggerType -ne 'Manual' -or $configuration.replicaTimeout -ne 3600 -or $configuration.replicaRetryLimit -ne 0) {
        throw 'The existing public worker job configuration does not match the approved fixed installation.'
    }
    if ($configuration.manualTriggerConfig.parallelism -ne 1 -or $configuration.manualTriggerConfig.replicaCompletionCount -ne 1) {
        throw 'The existing public worker job manual trigger configuration does not match the approved fixed installation.'
    }
    if ($configuration.eventTriggerConfig -ne $null -or $configuration.scheduleTriggerConfig -ne $null) {
        throw 'The existing public worker job trigger configuration does not match the approved fixed installation.'
    }
    if (-not (Test-NullOrEmptyCollection $configuration.secrets)) { throw 'The existing public worker job contains secrets and cannot be reset safely.' }
    if (@($configuration.registries).Count -ne 1 -or
        $configuration.registries[0].identity.ToLowerInvariant() -ne $ExpectedIdentityId.ToLowerInvariant() -or
        $configuration.registries[0].server -ne ($ExpectedImage.Split('@')[0].Split('/')[0]) -or
        (Get-PropertyValue $configuration.registries[0] 'username') -notin @($null, '') -or
        (Get-PropertyValue $configuration.registries[0] 'passwordSecretRef') -notin @($null, '')) {
        throw 'The existing public worker job registry identity does not match the approved fixed installation.'
    }
    $template = $Job.properties.template
    if (@($template.containers).Count -ne 1 -or $template.containers[0].image -ne $ExpectedImage) {
        throw 'The existing public worker job image does not match the current build receipt.'
    }
    if ($template.containers[0].resources.cpu -ne 1 -or $template.containers[0].resources.memory -ne '2Gi') {
        throw 'The existing public worker job resources do not match the approved fixed installation.'
    }
    if (-not (Test-NullOrEmptyCollection $template.initContainers) -or -not (Test-NullOrEmptyCollection $template.volumes)) {
        throw 'The existing public worker job template does not match the approved fixed installation.'
    }
    $envMap = @{}
    foreach ($entry in @($template.containers[0].env)) { $envMap[$entry.name] = $entry.value }
    if ($envMap.Count -ne 2 -or $envMap['TAWB_WORKER_MODE'] -ne 'hosted' -or $envMap['TAWB_BACKEND_BASE_URL'] -ne $ExpectedOrigin) {
        throw 'The existing public worker job environment does not match the approved fixed installation.'
    }
}

function Assert-NoActiveWorkerExecutions([string]$JobId) {
    if (-not $JobId) { return }
    $executions = Invoke-AzureJson @('rest', '--method', 'get', '--url', "https://management.azure.com$JobId/executions?api-version=2024-03-01")
    if ($executions -eq $null -or -not ($executions.PSObject.Properties.Name -contains 'value')) {
        throw 'The existing public worker job execution listing could not be validated safely.'
    }
    if (($executions.PSObject.Properties.Name -contains 'nextLink') -and $executions.nextLink) {
        throw 'The existing public worker job execution listing was paginated and cannot be validated safely.'
    }
    foreach ($execution in @($executions.value)) {
        if ($execution -eq $null -or $execution.properties.status -eq $null) {
            throw 'The existing public worker job execution listing could not be validated safely.'
        }
        if ($execution.properties.status -notin @('Succeeded', 'Failed', 'Stopped')) {
            throw 'The existing public worker job has a non-terminal execution and cannot be reset safely.'
        }
    }
}

function Save-State([string]$Name, $Value) {
    [IO.File]::WriteAllText((Get-StatePath $Name), ($Value | ConvertTo-Json -Depth 32), [Text.UTF8Encoding]::new($false))
}

function Get-State([string]$Name, [string]$Message) {
    $path = Get-StatePath $Name
    if (-not (Test-Path $path)) { throw $Message }
    return Get-Content -Raw $path | ConvertFrom-Json -Depth 32
}

function Get-Foundation {
    return Get-State 'foundation.json' 'Complete the Foundation phase first.'
}

function Get-LegacyImage {
    return Get-State 'image.json' 'Complete the legacy Build phase first.'
}

function Get-EmailImages {
    return Get-State 'email-images.json' 'Complete the email Build phase first.'
}

function Get-WorkerDeployment {
    return Get-State 'email-worker.json' 'Complete the email Worker phase first.'
}

function Assert-LegacyMode {
    Assert-LegacyPhase 'This phase'
}

function Assert-EmailMode {
    Assert-EmailPhase 'This phase'
}

function Build-Context([string]$ArchiveName, [string]$ContextPrefix, [switch]$Public) {
    $buildId = [Guid]::NewGuid().ToString('N')
    $contextPath = Join-Path $state "$ContextPrefix-$buildId"
    $archive = Join-Path $state "$ArchiveName-$buildId.tar.gz"
    $arguments = @('-m', 'tradingagents.workbench.public_source', '--root', $root, '--output', $archive, '--context-directory', $contextPath)
    if ($Public) { $arguments += '--public' }
    $sourceHash = & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'The bounded build context could not be prepared.' }
    return @{
        archive = $archive
        archive_sha256 = Get-Sha256 $archive
        context = $contextPath
        context_id = $buildId
        source_sha256 = $sourceHash.Trim()
    }
}

function Invoke-Foundation {
    Assert-LegacyPhase 'Foundation'
    $ownershipPath = Join-Path $state 'foundation-owner.json'
    $exists = Invoke-AzureJson @('group', 'exists', '--name', $group, '--subscription', $subscription)
    if ($exists) {
        $existing = Invoke-AzureJson @('group', 'show', '--name', $group, '--subscription', $subscription)
        if (-not (Test-Path $ownershipPath)) { throw 'Existing resource group has no local deployment ownership record.' }
        $owner = Get-Content -Raw $ownershipPath | ConvertFrom-Json
        if ($existing.tags.application -ne 'tradingagents-public-workbench' -or
            $existing.id -ne $owner.resource_group_id -or $existing.tags.tawbDeployment -ne $owner.deployment_id) {
            throw 'The hosting resource group does not match the saved deployment ownership.'
        }
    } else {
        $deploymentId = [Guid]::NewGuid().ToString()
        Save-State 'foundation-owner.json' @{ resource_group_id = "/subscriptions/$subscription/resourceGroups/$group"; deployment_id = $deploymentId }
        $null = Invoke-AzureJson @('group', 'create', '--name', $group, '--location', 'eastus2', '--subscription', $subscription,
            '--tags', 'application=tradingagents-public-workbench', "tawbDeployment=$deploymentId")
    }
    $deployer = Invoke-AzureJson @('ad', 'signed-in-user', 'show', '--query', 'id')
    $arguments = @('--resource-group', $group, '--subscription', $subscription, '--name', 'public-foundation',
        '--template-file', (Join-Path $root 'infra/public-foundation.bicep'), '--parameters',
        "tenantId=$tenant", "deployerPrincipalId=$deployer", "appName=$appName")
    $null = Invoke-AzureJson (@('deployment', 'group', 'validate') + $arguments)
    $result = Invoke-AzureJson (@('deployment', 'group', 'create') + $arguments + @('--query', 'properties.outputs'))
    Save-State 'foundation.json' $result
    Write-Output "Hosting foundation ready: $group"
}

function Invoke-Credentials {
    Assert-LegacyMode
    $foundation = Get-Foundation
    $vaultId = "/subscriptions/$subscription/resourceGroups/$group/providers/Microsoft.KeyVault/vaults/$($foundation.vaultName.value)"
    $identityId = "/subscriptions/$subscription/resourceGroups/$group/providers/Microsoft.ManagedIdentity/userAssignedIdentities/$($foundation.identityName.value)"
    & $python (Join-Path $PSScriptRoot 'public_bootstrap.py') --vault-id $vaultId --receipt (Join-Path $state 'bootstrap-receipt.json') `
        --origin $foundation.publicOrigin.value --tenant-id $tenant --client-id $clientId --object-id $objectId --identity-id $identityId
    if ($LASTEXITCODE -ne 0) { throw 'Application credential initialization failed; do not publish the application.' }
}

function Invoke-Build {
    $foundation = Get-ApprovedFoundation
    if ($RuntimeMode -eq 'entra') {
        $public = Build-Context -ArchiveName 'source' -ContextPrefix 'context' -Public
        $tag = 'public-' + (Get-Date -Format 'yyyyMMddHHmmss')
        $intent = @{ runtime_mode = 'entra'; image = @{ tag = $tag; source_sha256 = $public.source_sha256; archive_sha256 = $public.archive_sha256; context_id = $public.context_id } }
        Start-Intent 'legacy-build' $intent
        $result = Invoke-AzureJson @('acr', 'build', '--subscription', $subscription, '--registry', $foundation.registryName.value,
            '--image', "public-workbench:$tag", '--file', 'infra/Dockerfile.public-workbench', '--no-logs', $public.context,
            '--query', '{status:status,runId:runId}')
        if ($result.status -ne 'Succeeded') { throw 'The public image build did not succeed.' }
        $digest = Invoke-AzureJson @('acr', 'repository', 'show', '--name', $foundation.registryName.value, '--subscription', $subscription,
            '--image', "public-workbench:$tag", '--query', 'digest')
        if ($digest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'A verified image digest is required.' }
        $receipt = @{ digest = $digest; tag = $tag; source_sha256 = $public.source_sha256; archive_sha256 = $public.archive_sha256; context_id = $public.context_id; run_id = $result.runId }
        Save-State 'image.json' $receipt
        Complete-Intent 'legacy-build' $receipt
        Write-Output "Legacy public image ready: $tag $digest"
        return
    }
    $public = Build-Context -ArchiveName 'public-source' -ContextPrefix 'public-context' -Public
    $worker = Build-Context -ArchiveName 'worker-source' -ContextPrefix 'worker-context'
    $stamp = Get-Date -Format 'yyyyMMddHHmmss'
    $publicTag = 'public-' + $stamp
    $workerTag = 'worker-' + $stamp
    $generation = 'build-' + [Guid]::NewGuid().ToString('N')
    $intent = @{
        runtime_mode = 'email'
        generation = $generation
        repository = $workerRepository
        public = @{ tag = $publicTag; source_sha256 = $public.source_sha256; archive_sha256 = $public.archive_sha256; context_id = $public.context_id }
        worker = @{ tag = $workerTag; source_sha256 = $worker.source_sha256; archive_sha256 = $worker.archive_sha256; context_id = $worker.context_id }
    }
    Start-Intent 'email-build' $intent
    $publicBuild = Invoke-AzureJson @('acr', 'build', '--subscription', $subscription, '--registry', $foundation.registryName.value,
        '--image', "public-workbench:$publicTag", '--file', 'infra/Dockerfile.public-workbench', '--no-logs', $public.context,
        '--query', '{status:status,runId:runId}')
    if ($publicBuild.status -ne 'Succeeded') { throw 'The public image build did not succeed.' }
    $workerBuild = Invoke-AzureJson @('acr', 'build', '--subscription', $subscription, '--registry', $foundation.registryName.value,
        '--image', "${workerRepository}:$workerTag", '--file', 'infra/Dockerfile.workbench', '--no-logs', $worker.context,
        '--query', '{status:status,runId:runId}')
    if ($workerBuild.status -ne 'Succeeded') { throw 'The worker image build did not succeed.' }
    $publicDigest = Invoke-AzureJson @('acr', 'repository', 'show', '--name', $foundation.registryName.value, '--subscription', $subscription,
        '--image', "public-workbench:$publicTag", '--query', 'digest')
    $workerDigest = Invoke-AzureJson @('acr', 'repository', 'show', '--name', $foundation.registryName.value, '--subscription', $subscription,
        '--image', "${workerRepository}:$workerTag", '--query', 'digest')
    if ($publicDigest -notmatch '^sha256:[0-9a-f]{64}$' -or $workerDigest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw 'Verified immutable image digests are required.'
    }
    $receipt = @{
        generation = $generation
        repository = $workerRepository
        public = @{ digest = $publicDigest; tag = $publicTag; source_sha256 = $public.source_sha256; archive_sha256 = $public.archive_sha256; context_id = $public.context_id; run_id = $publicBuild.runId }
        worker = @{ digest = $workerDigest; tag = $workerTag; source_sha256 = $worker.source_sha256; archive_sha256 = $worker.archive_sha256; context_id = $worker.context_id; run_id = $workerBuild.runId }
    }
    Save-State 'email-images.json' $receipt
    Complete-Intent 'email-build' $receipt
    Write-Output "Email public image ready: $publicTag $publicDigest"
    Write-Output "Email worker image ready: $workerTag $workerDigest"
}

function Invoke-Worker {
    Assert-EmailPhase 'Worker'
    $foundation = Get-ApprovedFoundation
    $images = Get-EmailImages
    Assert-ImageReceipt $images
    $expectedImage = "$($foundation.registryName.value).azurecr.io/$workerRepository@$($images.worker.digest)"
    $expectedIdentityId = Get-CanonicalWorkerIdentityId
    $expectedEnvironmentId = "$(Get-CanonicalResourceGroupId)/providers/Microsoft.App/managedEnvironments/$($foundation.environmentName.value)"
    $job = Get-CurrentWorkerJob
    if ($job -ne $null) {
        Assert-WorkerJobBaseline $job $expectedImage $expectedIdentityId $expectedEnvironmentId $foundation.publicOrigin.value
        Assert-NoActiveWorkerExecutions $job.id
    } elseif (Get-ExistingWorkerIdentityId) {
        throw 'The existing public worker identity does not match the approved fixed installation.'
    }
    $intent = @{ build_receipt = $images; expected_image = $expectedImage; job_name = $workerJobName; worker_identity_id = $expectedIdentityId; environment_id = $expectedEnvironmentId }
    Start-Intent 'email-worker' $intent
    $arguments = @('--resource-group', $group, '--subscription', $subscription, '--name', 'public-worker',
        '--template-file', (Join-Path $root 'infra/public-worker.bicep'), '--parameters',
        "location=eastus2", "jobName=$workerJobName", "environmentName=$($foundation.environmentName.value)",
        "registryName=$($foundation.registryName.value)", "workerRepository=$workerRepository",
        "workerIdentityName=$workerIdentityName", "hostIdentityName=$($foundation.identityName.value)",
        "workspaceName=$logsName", "backendBaseUrl=$($foundation.publicOrigin.value)",
        "imageDigest=$($images.worker.digest)", "foundryResourceGroup=$foundryGroup", "foundryAccountName=$foundryName")
    $null = Invoke-AzureJson (@('deployment', 'group', 'validate') + $arguments)
    $result = Invoke-AzureJson (@('deployment', 'group', 'create') + $arguments + @('--query', 'properties.outputs'))
    $receipt = @{
        jobId = $result.jobId
        jobName = $result.jobName
        environmentId = $result.environmentId
        workerIdentityId = $result.workerIdentityId
        workspaceId = $result.workspaceId
        workspaceResourceId = $result.workspaceResourceId
        hostedImage = $result.hostedImage
        customRoleDefinitionId = $result.customRoleDefinitionId
        location = @{ value = 'eastus2' }
        buildReceipt = @{ value = $images }
    }
    Save-State 'email-worker.json' $receipt
    Save-State 'email-worker-owner.json' @{ job_id = $result.jobId.value; job_name = $result.jobName.value }
    Complete-Intent 'email-worker' $receipt
    Write-Output "Email worker job ready: $($result.jobName.value)"
}

function Invoke-Publish {
    $foundation = Get-ApprovedFoundation
    if ($RuntimeMode -eq 'entra') {
        $image = Get-LegacyImage
        if ($image.digest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'A verified image digest is required.' }
        $arguments = @('--resource-group', $group, '--subscription', $subscription, '--name', 'public-workbench',
            '--template-file', (Join-Path $root 'infra/public-app.bicep'), '--parameters',
            'runtimeMode=entra', "tenantId=$tenant", "clientId=$clientId", "appName=$appName", "imageDigest=$($image.digest)")
    } else {
        $images = Get-EmailImages
        $worker = Get-WorkerDeployment
        Assert-WorkerDeploymentReceipt $foundation $images $worker
        $job = Get-CurrentWorkerJob
        if ($job -eq $null) { throw 'The current public worker job could not be read back for publish.' }
        $expectedIdentityId = $worker.workerIdentityId.value
        $expectedEnvironmentId = $worker.environmentId.value
        $expectedImage = $worker.hostedImage.value
        Assert-WorkerJobBaseline $job $expectedImage $expectedIdentityId $expectedEnvironmentId $foundation.publicOrigin.value
        Assert-NoActiveWorkerExecutions $job.id
        if ($images.public.digest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'A verified image digest is required.' }
        Start-Intent 'email-public' @{ build_receipt = $images; worker_receipt = $worker }
        $arguments = @('--resource-group', $group, '--subscription', $subscription, '--name', 'public-workbench-email',
            '--template-file', (Join-Path $root 'infra/public-app.bicep'), '--parameters',
            'runtimeMode=email', "appName=$appName", "imageDigest=$($images.public.digest)",
            "hostedSubscriptionId=$subscription", "hostedResourceGroup=$group", "hostedJobName=$($worker.jobName.value)",
            "hostedJobResourceId=$($worker.jobId.value)", "hostedEnvironmentId=$($worker.environmentId.value)",
            "hostedWorkerIdentityId=$($worker.workerIdentityId.value)", "hostedImage=$($worker.hostedImage.value)",
            "hostedWorkspaceId=$($worker.workspaceId.value)", 'hostedLocation=eastus2')
    }
    $null = Invoke-AzureJson (@('deployment', 'group', 'validate') + $arguments)
    $result = Invoke-AzureJson (@('deployment', 'group', 'create') + $arguments + @('--query', 'properties.outputs'))
    if ($RuntimeMode -eq 'entra') {
        Save-State 'deployment.json' $result
    }
    $origin = $result.publicOrigin.value
    if ($origin -cne $foundation.publicOrigin.value) {
        throw 'The deployment output origin does not match the approved public origin.'
    }
    $health = Invoke-WebRequest -Uri "$origin/healthz" -SkipHttpErrorCheck -TimeoutSec 60
    if ($health.StatusCode -ne 200 -or ($health.Content | ConvertFrom-Json).status -ne 'ok') {
        throw 'The public HTTPS application is not healthy; inspect the new hosting resources before retrying.'
    }
    $anonymous = Invoke-WebRequest -Uri "$origin/api/me" -SkipHttpErrorCheck -TimeoutSec 60
    if ($anonymous.StatusCode -ne 401) { throw 'Anonymous API isolation failed; do not use the endpoint.' }
    if ($RuntimeMode -eq 'email') {
        $app = Get-CurrentPublicAppProjection
        $receipt = New-EmailPublishReceipt $foundation $images $worker $app
        Assert-BootstrapReceipt $receipt $foundation $images $worker
        Assert-PublicAppBaseline $app $foundation $worker $receipt
        Save-State 'email-public.json' $receipt
        Complete-Intent 'email-public' $receipt
    }
    Write-Output "Public endpoint: $($result.publicOrigin.value)"
}

function Invoke-Bootstrap {
    Assert-EmailPhase 'Bootstrap'
    $foundation = Get-ApprovedFoundation
    $images = Get-EmailImages
    $worker = Get-WorkerDeployment
    Assert-WorkerDeploymentReceipt $foundation $images $worker
    $deployment = Get-State 'email-public.json' 'Complete the email Publish phase first.'
    Assert-BootstrapReceipt $deployment $foundation $images $worker
    $app = Get-CurrentPublicAppProjection
    Assert-PublicAppBaseline $app $foundation $worker $deployment
    & az containerapp exec --name $appName --resource-group $group --subscription $subscription --command 'python -m tradingagents.workbench.email_bootstrap'
    if ($LASTEXITCODE -ne 0) { throw 'Owner bootstrap failed or was cancelled. No automatic retry was issued.' }
}

Push-Location $root
try {
    Assert-PhaseSelection
    Assert-LocalPhasePrerequisites
    foreach ($template in @('public-foundation', 'public-app', 'public-worker')) {
        & az bicep build --file "infra/$template.bicep" --outfile (Join-Path $state "$template.json") --only-show-errors
        if ($LASTEXITCODE -ne 0) { throw "Bicep compilation failed: $template" }
    }
    if ($Phase -eq 'Validate') { Write-Output 'Public hosting templates compiled. No Azure resources changed.'; return }
    if (-not (Test-Path $python)) { throw 'The research project interpreter is required.' }
    $account = Invoke-AzureJson @('account', 'show')
    if ($account.id -ne $subscription -or $account.tenantId -ne $tenant) { throw 'Select the approved Azure subscription and tenant before deployment.' }
    if ($Phase -in @('Foundation', 'All')) { Invoke-Foundation }
    if ($Phase -in @('Credentials', 'All')) { Invoke-Credentials }
    if ($Phase -in @('Build', 'All')) { Invoke-Build }
    if ($Phase -eq 'Worker') { Invoke-Worker }
    if ($Phase -in @('Publish', 'All')) { Invoke-Publish }
    if ($Phase -eq 'Bootstrap') { Invoke-Bootstrap }
} finally {
    Pop-Location
}