/* Modified for public distribution: installation identifiers replaced with examples. */
targetScope = 'resourceGroup'

param location string = 'eastus2'
@minLength(2)
@maxLength(32)
param jobName string = 'job-tradingagents-public'
param environmentName string = 'tradingagents-workbench-env-vnet'
param registryName string = 'previousregistryexample'
param workerRepository string = 'tradingagents-workbench-worker'
param workerIdentityName string = 'tradingagents-workbench-worker'
param hostIdentityName string = 'tradingagents-workbench-host'
param workspaceName string = 'tradingagents-workbench-logs'
param backendBaseUrl string
param imageDigest string
param foundrySubscriptionId string = subscription().subscriptionId
param foundryResourceGroup string = 'rg-research-example'
param foundryAccountName string = 'example-foundry-source'
param deployFoundryRole bool = true

var jobOperatorRoleName = guid(subscription().id, resourceGroup().id, jobName, 'PublicHostedWorkerOperator')
var foundryRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
var jobOperatorActions = [
  'Microsoft.App/jobs/read'
  'Microsoft.App/jobs/write'
  'Microsoft.App/jobs/start/action'
  'Microsoft.App/jobs/execution/read'
  'Microsoft.App/jobs/executions/read'
  'Microsoft.App/jobs/stop/execution/action'
  'Microsoft.App/jobs/stop/execution/backport/action'
  'Microsoft.App/jobs/listSecrets/action'
]

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: workerIdentityName
  location: location
}

resource hostIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: hostIdentityName
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

module jobOperatorRole './public-worker-custom-role.bicep' = {
  name: 'public-worker-custom-role'
  scope: subscription()
  params: {
    roleDefinitionName: jobOperatorRoleName
    roleName: '${jobName}-operator'
    description: 'Operate the dedicated public hosted worker job.'
    assignableScope: resourceGroup().id
    actions: jobOperatorActions
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, workerIdentity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${workerIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 3600
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: workerIdentity.id
        }
      ]
      secrets: []
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: '${registry.properties.loginServer}/${workerRepository}@${imageDigest}'
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          env: [
            { name: 'TAWB_WORKER_MODE', value: 'hosted' }
            { name: 'TAWB_BACKEND_BASE_URL', value: backendBaseUrl }
          ]
        }
      ]
    }
  }
  dependsOn: [
    acrPull
  ]
}

resource jobOperatorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(job.id, hostIdentity.id, 'PublicHostedWorkerOperator')
  scope: job
  properties: {
    principalId: hostIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: jobOperatorRole.outputs.roleDefinitionId
  }
}

resource logReaderAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(workspace.id, hostIdentity.id, 'Log Analytics Reader')
  scope: workspace
  properties: {
    principalId: hostIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
  }
}

module foundryAssignment './public-worker-foundry-role.bicep' = if (deployFoundryRole) {
  name: 'public-worker-foundry-role'
  scope: resourceGroup(foundrySubscriptionId, foundryResourceGroup)
  params: {
    foundryAccountName: foundryAccountName
    principalId: hostIdentity.properties.principalId
    roleDefinitionId: foundryRoleDefinitionId
  }
}

output jobId string = job.id
output jobName string = job.name
output environmentId string = environment.id
output workerIdentityId string = workerIdentity.id
output workspaceId string = workspace.properties.customerId
output workspaceResourceId string = workspace.id
output hostedImage string = '${registry.properties.loginServer}/${workerRepository}@${imageDigest}'
output customRoleDefinitionId string = jobOperatorRole.outputs.roleDefinitionId