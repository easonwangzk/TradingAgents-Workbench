/* Modified for public distribution: installation identifiers replaced with examples. */
targetScope = 'resourceGroup'

param location string = 'eastus2'
param jobName string = 'job-research-example'
param environmentName string = 'research-environment-example'
param registryName string = 'researchregistryexample'
param foundryName string = 'example-foundry-source'
param imageTag string
param modelName string = 'gpt-5.6-sol'
@secure()
param alphaVantageKey string
@secure()
param fredKey string = ''

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource foundry 'Microsoft.CognitiveServices/accounts@2023-05-01' existing = {
  name: foundryName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${jobName}-identity'
  location: location
}
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource modelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, identity.id, 'OpenAIUser')
  scope: foundry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 3600
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [{
        server: registry.properties.loginServer
        identity: identity.id
      }]
      secrets: concat([{ name: 'alpha-vantage-key', value: alphaVantageKey }], empty(fredKey) ? [] : [{ name: 'fred-key', value: fredKey }])
    }
    template: {
      containers: [{
        name: 'worker'
        image: '${registry.properties.loginServer}/tradingagents-workbench:${imageTag}'
        resources: {
          cpu: 1
          memory: '2Gi'
        }
        env: concat([
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'AZURE_OPENAI_AUTH_MODE', value: 'managed_identity' }
          { name: 'AZURE_OPENAI_ENDPOINT', value: 'https://${foundryName}.services.ai.azure.com/' }
          { name: 'AZURE_OPENAI_DEPLOYMENT_NAME', value: modelName }
          { name: 'ALPHA_VANTAGE_API_KEY', secretRef: 'alpha-vantage-key' }
          { name: 'TRADINGAGENTS_LLM_PROVIDER', value: 'azure' }
        ], empty(fredKey) ? [] : [{ name: 'FRED_API_KEY', secretRef: 'fred-key' }])
      }]
    }
  }
  dependsOn: [acrPull, modelUser]
}

output jobId string = job.id
output jobName string = job.name
output identityId string = identity.id
output model string = modelName
output endpoint string = 'https://${foundryName}.services.ai.azure.com/'