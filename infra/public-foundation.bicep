/* Modified for public distribution: installation identifiers replaced with examples. */
targetScope = 'resourceGroup'

param location string = 'eastus2'
param tenantId string
param deployerPrincipalId string
param appName string = 'tradingagents-workbench'

var suffix = uniqueString(subscription().id, resourceGroup().id)
var tags = { application: 'tradingagents-public-workbench' }

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-host'
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'taw${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false, anonymousPullEnabled: false }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'taw${suffix}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Disabled'
    encryption: { keySource: 'Microsoft.Storage', services: { blob: { enabled: true } } }
  }
}
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: { isVersioningEnabled: false }
}
resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'state'
  properties: { publicAccess: 'None' }
}
resource cleanup 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [{
        enabled: true
        name: 'expired-auth-state'
        type: 'Lifecycle'
        definition: {
          filters: { blobTypes: ['blockBlob'], prefixMatch: ['state/flows/', 'state/sessions/', 'state/email-v1/sessions/'] }
          actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: 1 } } }
        }
      }]
    }
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'taw-${suffix}'
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    publicNetworkAccess: 'Disabled'
    accessPolicies: []
  }
}
resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${appName}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.84.0.0/23'] }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: '10.84.0.0/26'
          delegations: [{ name: 'container-apps', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
      {
        name: 'endpoints'
        properties: { addressPrefix: '10.84.0.64/27', privateEndpointNetworkPolicies: 'Disabled' }
      }
    ]
  }
}
var privateServices = [
  { name: 'vault', zone: 'privatelink.vaultcore.azure.net', resourceId: vault.id, groupId: 'vault' }
  { name: 'blob', zone: 'researchstateexample.blob.core.windows.net', resourceId: storage.id, groupId: 'blob' }
]
resource privateZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for service in privateServices: {
  name: service.zone
  location: 'global'
  tags: tags
}]
resource privateLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (service, index) in privateServices: {
  name: '${privateZones[index].name}/${appName}'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: network.id } }
}]
resource endpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for service in privateServices: {
  name: '${appName}-${service.name}-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${network.id}/subnets/endpoints' }
    privateLinkServiceConnections: [{
      name: service.name
      properties: { privateLinkServiceId: service.resourceId, groupIds: [service.groupId] }
    }]
  }
}]
resource zoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (service, index) in privateServices: {
  name: '${endpoints[index].name}/default'
  properties: { privateDnsZoneConfigs: [{ name: service.name, properties: { privateDnsZoneId: privateZones[index].id } }] }
}]
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env-vnet'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: { infrastructureSubnetId: '${network.id}/subnets/apps', internal: false }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: { customerId: logs.properties.customerId, sharedKey: logs.listKeys().primarySharedKey }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

resource hostStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(stateContainer.id, identity.id, 'BlobDataContributor')
  scope: stateContainer
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}
resource hostSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identity.id, 'SecretsUser')
  scope: vault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}
resource deployerSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, deployerPrincipalId, 'SecretsOfficer')
  scope: vault
  properties: {
    principalId: deployerPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  }
}
resource hostPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output vaultName string = vault.name
output vaultUrl string = vault.properties.vaultUri
output storageUrl string = storage.properties.primaryEndpoints.blob
output identityName string = identity.name
output identityClientId string = identity.properties.clientId
output environmentName string = environment.name
output publicOrigin string = 'https://${appName}.${environment.properties.defaultDomain}'
output appName string = appName