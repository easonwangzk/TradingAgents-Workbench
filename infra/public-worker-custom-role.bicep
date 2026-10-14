targetScope = 'subscription'

param roleDefinitionName string
param roleName string
param description string
param assignableScope string
param actions array

resource customRole 'Microsoft.Authorization/roleDefinitions@2022-05-01-preview' = {
  name: roleDefinitionName
  properties: {
    roleName: roleName
    description: description
    type: 'CustomRole'
    assignableScopes: [
      assignableScope
    ]
    permissions: [
      {
        actions: actions
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
  }
}

output roleDefinitionId string = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', customRole.name)