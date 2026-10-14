# Self-Hosting And Configuration

This repository supplies source and infrastructure examples, not access to a shared installation. Deploy only to subscriptions, resources, domains and provider accounts you control. No deployment, inference or email operation is required to run offline tests.

## Local CLI

Create and activate a Python environment, install `.[dev]`, and copy `.env.example` to an untracked `.env`. Set only the credentials for providers you intend to use. `python -m cli.main --help` makes no provider call; starting research does.

## Local Browser Workbench

Install `.[workbench,dev]`, authenticate with Azure CLI, provision a separate Container Apps Job and configure `.local-run/workbench/deployment.json`. The local browser mode uses local JSON history and its configured Azure resources; it is not a client of an existing hosted service.

The configuration has the following shape. Replace every example value before use:

```json
{
  "subscription_id": "00000000-0000-4000-8000-000000000001",
  "resource_group": "rg-research-example",
  "job_name": "job-research-example",
  "workspace_id": "00000000-0000-4000-8000-000000000002",
  "model": "your-model-deployment",
  "endpoint": "https://example-foundry.services.ai.azure.com/"
}
```

Review `infra/workbench.bicep` and `scripts/deploy-workbench.ps1` before provisioning. The script requires process-local provider settings and can create billable resources. It is not a read-only validation command. Once configured, run `scripts/start-workbench.ps1` from PowerShell.

## Hosted Email Runtime

The Web/Worker runtime needs your own private Blob state, Key Vault encryption key, managed identities and scoped roles, registry/images, Container Apps environment and Job, log workspace, HTTPS origin, email sender and configured model/data providers. `infra/` contains the templates; deployment scripts contain example installation bindings that must be replaced and reviewed, not reused as an approved production installation.

Use `.[public-workbench,dev]`. The Web entrypoint is `tradingagents.workbench.public_runtime:create_runtime_app` with the environment explicitly selecting `TAWB_PUBLIC_RUNTIME_MODE=email`. Relevant settings use `TAWB_PUBLIC_*`, `TAWB_HOSTED_*`, `TAWB_EMAIL_*` and optional `TAWB_FOUNDRY_*` names. The complete, validated schemas are defined in `public_runtime.py` and the corresponding Bicep templates; incomplete or invalid configuration fails closed.

Set the reserved owner identity consistently before account initialization. This distribution uses `owner@example.com` in `public_accounts.py` and synthetic fixtures. Account bootstrap is an intentional operator step; never initialize or migrate real accounts from a test run, and never collect a password in a script or issue. Type passwords directly into the trusted interactive bootstrap terminal.

Generate and preserve your own encryption key using the provided bootstrap workflow. Do not put keys, runtime state, password verifiers, provider credentials or deployment receipts in Git. Worker capabilities are short-lived run credentials, not provider keys. The Worker must not receive direct access to host state or host provider credentials.

`config/owner-foundry.json` is an example model/pricing contract. Its endpoint and approval metadata are synthetic, and verification defaults to `pending`. Independently validate the actual model/version, pricing evidence, limits, identity permissions and expiry for your installation before marking it verified and enabling managed inference. No example is a promise of provider access or current pricing.

## Before Offering Public Signup

- Decide whether assistant access should default on; add admission/per-account spending limits appropriate to your service.
- The shared application allowance is not an Azure invoice cap. Configure independent alerts and service controls for hosting, mail, data and inference.
- Review trusted ingress/client identification. Never trust arbitrary forwarded headers; configure only known proxies or enforce limits at the ingress.
- Review the low-volume registration-mail quota and sender/domain entitlements before promoting signup.
- Verify account isolation, logout/revocation, stop/reconciliation and your actual deployed identities with bounded authorized acceptance tests.
- Preserve unresolved dispatched model-cost holds; do not automatically refund or retry unknown outcomes.

## Validation

Run Python tests, Ruff, Node checks/UI tests and both frontend builds as documented in the root README. Tests use mocks; optional live-provider checks require their own explicit credentials and approval. Do not run deployment or operator scripts as smoke tests.

Private operational histories and personal research exports are intentionally absent. Record your own deployment decisions outside the public source tree. The upstream `CHANGELOG.md` documents the upstream framework, not a history of this self-hosted installation.