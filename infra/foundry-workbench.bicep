targetScope = 'resourceGroup'

param accountName string

@allowed(['eastus2'])
param location string = 'eastus2'

param hostPrincipalId string

var models = [
  { name: 'gpt-5.6-sol', capacity: 100 }
  { name: 'gpt-5.6-luna', capacity: 150 }
  { name: 'gpt-5.6-terra', capacity: 150 }
]
var inferenceRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  tags: { application: 'tradingagents-public-workbench' }
  properties: {
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow', ipRules: [], virtualNetworkRules: [] }
  }
}

resource contentPolicy 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-06-01' = {
  parent: account
  name: 'CustomContentFilter'
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    mode: 'Default'
    contentFilters: [
      { name: 'Indirect Attack', source: 'Prompt', enabled: true, blocking: true }
      { name: 'DefenderForAI', source: 'Prompt', enabled: true, blocking: true }
      { name: 'DefenderForAI', source: 'Completion', enabled: true, blocking: true }
      { name: 'DefenderForAI', source: 'PostRun', enabled: true, blocking: true }
      { name: 'DefenderForAI', source: 'PostToolCall', enabled: true, blocking: true }
      { name: 'DefenderForAI', source: 'PreRun', enabled: true, blocking: true }
      { name: 'DefenderForAI', source: 'PreToolCall', enabled: true, blocking: true }
      { name: 'Indirect Attack', source: 'PostToolCall', enabled: true, blocking: false }
    ]
  }
}

@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for model in models: {
  parent: account
  name: model.name
  sku: { name: 'GlobalStandard', capacity: model.capacity }
  properties: {
    model: { format: 'OpenAI', name: model.name, version: '2026-07-09' }
    raiPolicyName: contentPolicy.name
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}]

resource hostInference 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, hostPrincipalId, inferenceRole)
  scope: account
  properties: {
    roleDefinitionId: inferenceRole
    principalId: hostPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output accountId string = account.id
output endpoint string = 'https://${accountName}.services.ai.azure.com/openai/v1/'
output deploymentNames array = [for model in models: model.name]
output inferenceAssignmentId string = hostInference.id