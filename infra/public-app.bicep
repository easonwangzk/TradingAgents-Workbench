/* Modified for public distribution: installation identifiers replaced with examples. */
targetScope = 'resourceGroup'

param location string = 'eastus2'
param appName string = 'tradingagents-workbench'
param environmentName string = '${appName}-env-vnet'
param hostIdentityName string = '${appName}-host'
@allowed([
  'entra'
  'email'
])
param runtimeMode string = 'email'
param tenantId string = ''
param clientId string = ''
param imageDigest string
param hostedSubscriptionId string = ''
param hostedResourceGroup string = ''
param hostedJobName string = ''
param hostedJobResourceId string = ''
param hostedEnvironmentId string = ''
param hostedWorkerIdentityId string = ''
param hostedImage string = ''
param hostedWorkspaceId string = ''
param hostedLocation string = ''
param approvedCompatibleOrigins string = ''
param publicCompatibleOrigins string = 'https://research.example.com'
param foundryEndpoint string = ''
param foundryDeployment string = ''
param foundryApiVersion string = ''
param foundryModelFamily string = ''
param foundryModelVersion string = ''
param foundryContextWindowTokens string = ''
param foundryMaxOutputTokens string = ''
param foundryPricingVersion string = ''
param foundryInputMicrousdPerMillionTokens string = ''
param foundryCachedInputMicrousdPerMillionTokens string = ''
param foundryOutputMicrousdPerMillionTokens string = ''
param foundryVerificationStatus string = ''
param foundryVerificationEvidence string = ''
param foundryVerificationRecordedAt string = ''
param foundryModelsJson string = ''
param emailEndpoint string = ''
param emailSender string = ''

var suffix = uniqueString(subscription().id, resourceGroup().id)
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = { name: environmentName }
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: hostIdentityName }
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: 'taw${suffix}' }
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = { name: 'taw${suffix}' }
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: 'taw-${suffix}' }
var publicHost = '${appName}.${environment.properties.defaultDomain}'
var commonEnv = [
  { name: 'TAWB_PUBLIC_RUNTIME_MODE', value: runtimeMode }
  { name: 'TAWB_MANAGED_IDENTITY_CLIENT_ID', value: identity.properties.clientId }
  { name: 'TAWB_PUBLIC_ORIGIN', value: 'https://${publicHost}' }
  { name: 'TAWB_VAULT_URL', value: 'https://${vault.name}${az.environment().suffixes.keyvaultDns}' }
  { name: 'TAWB_STORAGE_ACCOUNT_URL', value: 'https://${storage.name}.blob.${az.environment().suffixes.storage}' }
]
var entraEnv = runtimeMode == 'entra' ? [
  { name: 'TAWB_TENANT_ID', value: tenantId }
  { name: 'TAWB_CLIENT_ID', value: clientId }
] : []
var emailEnv = runtimeMode == 'email' ? [
  { name: 'TAWB_HOSTED_SUBSCRIPTION_ID', value: hostedSubscriptionId }
  { name: 'TAWB_HOSTED_RESOURCE_GROUP', value: hostedResourceGroup }
  { name: 'TAWB_HOSTED_JOB_NAME', value: hostedJobName }
  { name: 'TAWB_HOSTED_JOB_RESOURCE_ID', value: hostedJobResourceId }
  { name: 'TAWB_HOSTED_ENVIRONMENT_ID', value: hostedEnvironmentId }
  { name: 'TAWB_HOSTED_WORKER_IDENTITY_ID', value: hostedWorkerIdentityId }
  { name: 'TAWB_HOSTED_IMAGE', value: hostedImage }
  { name: 'TAWB_HOSTED_WORKSPACE_ID', value: hostedWorkspaceId }
  { name: 'TAWB_HOSTED_LOCATION', value: hostedLocation }
] : []
var originEnv = empty(approvedCompatibleOrigins) ? [] : [
  { name: 'TAWB_APPROVED_COMPATIBLE_ORIGINS', value: approvedCompatibleOrigins }
]
var publicCompatibleOriginsEnv = runtimeMode == 'email' && !empty(publicCompatibleOrigins) ? [
  { name: 'TAWB_PUBLIC_COMPATIBLE_ORIGINS', value: publicCompatibleOrigins }
] : []
var foundryEnv = empty(foundryEndpoint) ? [] : [
  { name: 'TAWB_FOUNDRY_ENDPOINT', value: foundryEndpoint }
  { name: 'TAWB_FOUNDRY_DEPLOYMENT', value: foundryDeployment }
  { name: 'TAWB_FOUNDRY_API_VERSION', value: foundryApiVersion }
  { name: 'TAWB_FOUNDRY_MODEL_FAMILY', value: foundryModelFamily }
  { name: 'TAWB_FOUNDRY_MODEL_VERSION', value: foundryModelVersion }
  { name: 'TAWB_FOUNDRY_CONTEXT_WINDOW_TOKENS', value: foundryContextWindowTokens }
  { name: 'TAWB_FOUNDRY_MAX_OUTPUT_TOKENS', value: foundryMaxOutputTokens }
  { name: 'TAWB_FOUNDRY_PRICING_VERSION', value: foundryPricingVersion }
  { name: 'TAWB_FOUNDRY_INPUT_MICROUSD_PER_MILLION_TOKENS', value: foundryInputMicrousdPerMillionTokens }
  { name: 'TAWB_FOUNDRY_CACHED_INPUT_MICROUSD_PER_MILLION_TOKENS', value: foundryCachedInputMicrousdPerMillionTokens }
  { name: 'TAWB_FOUNDRY_OUTPUT_MICROUSD_PER_MILLION_TOKENS', value: foundryOutputMicrousdPerMillionTokens }
  { name: 'TAWB_FOUNDRY_VERIFICATION_STATUS', value: foundryVerificationStatus }
  { name: 'TAWB_FOUNDRY_VERIFICATION_EVIDENCE', value: foundryVerificationEvidence }
  { name: 'TAWB_FOUNDRY_VERIFICATION_RECORDED_AT', value: foundryVerificationRecordedAt }
]
var foundryModelsEnv = empty(foundryModelsJson) ? [] : [
  { name: 'TAWB_FOUNDRY_MODELS_JSON', value: foundryModelsJson }
]
var emailDeliveryEnv = runtimeMode == 'email' && !empty(emailEndpoint) ? [
  { name: 'TAWB_EMAIL_ENDPOINT', value: emailEndpoint }
  { name: 'TAWB_EMAIL_SENDER', value: emailSender }
] : []

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: { application: 'tradingagents-public-workbench' }
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, allowInsecure: false, targetPort: 8080, transport: 'http' }
      registries: [{ server: registry.properties.loginServer, identity: identity.id }]
    }
    template: {
      containers: [{
        name: 'workbench'
        image: '${registry.properties.loginServer}/public-workbench@${imageDigest}'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: concat(commonEnv, entraEnv, emailEnv, originEnv, publicCompatibleOriginsEnv, foundryEnv, foundryModelsEnv, emailDeliveryEnv)
        probes: [
          { type: 'Startup', httpGet: { path: '/healthz', port: 8080, httpHeaders: [{ name: 'Host', value: publicHost }] }, periodSeconds: 5, failureThreshold: 60 }
          { type: 'Readiness', httpGet: { path: '/healthz', port: 8080, httpHeaders: [{ name: 'Host', value: publicHost }] }, periodSeconds: 10, failureThreshold: 3 }
          { type: 'Liveness', httpGet: { path: '/healthz', port: 8080, httpHeaders: [{ name: 'Host', value: publicHost }] }, periodSeconds: 30, failureThreshold: 3 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 2, rules: [{ name: 'http', http: { metadata: { concurrentRequests: '20' } } }] }
    }
  }
}

output publicOrigin string = 'https://${app.properties.configuration.ingress.fqdn}'
output appId string = app.id