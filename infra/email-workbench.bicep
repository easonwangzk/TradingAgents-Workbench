targetScope = 'resourceGroup'

param hostPrincipalId string
param emailServiceName string = 'email-tradingagents-workbench'
param communicationServiceName string = 'acs-tradingagents-workbench'

resource email 'Microsoft.Communication/emailServices@2026-03-18' = {
  name: emailServiceName
  location: 'global'
  tags: { application: 'tradingagents-public-workbench' }
  properties: { dataLocation: 'United States' }
}

resource domain 'Microsoft.Communication/emailServices/domains@2026-03-18' = {
  parent: email
  name: 'AzureManagedDomain'
  location: 'global'
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communication 'Microsoft.Communication/communicationServices@2026-03-18' = {
  name: communicationServiceName
  location: 'global'
  tags: { application: 'tradingagents-public-workbench' }
  properties: {
    dataLocation: 'United States'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    linkedDomains: [domain.id]
  }
}

resource emailRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'workbench-email-delivery')
  properties: {
    roleName: 'TradingAgents Workbench Email Delivery'
    description: 'Communication email authentication, assigned only to the dedicated communication resource.'
    type: 'CustomRole'
    assignableScopes: [resourceGroup().id]
    permissions: [{
      actions: [
        'Microsoft.Communication/CommunicationServices/Read'
        'Microsoft.Communication/CommunicationServices/Write'
        'Microsoft.Communication/EmailServices/Write'
      ]
      notActions: []
      dataActions: []
      notDataActions: []
    }]
  }
}

resource hostEmail 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(communication.id, hostPrincipalId, emailRole.id)
  scope: communication
  properties: {
    roleDefinitionId: emailRole.id
    principalId: hostPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output communicationId string = communication.id
output domainId string = domain.id
output endpoint string = 'https://${communication.properties.hostName}'
output senderDomain string = domain.properties.fromSenderDomain
output roleId string = emailRole.id
output assignmentId string = hostEmail.id