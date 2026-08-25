# MAADEN Corporate Communication System — Azure Static Web Apps Runbook

This runbook deploys the MAADEN Corporate Communication System to **Azure**, using **Azure Static Web Apps (SWA)** as the frontend host with the FastAPI backend running as a linked Azure-hosted API.

Reference topology:

| Tier | Azure service |
|------|---------------|
| Frontend static hosting + global CDN + TLS | **Azure Static Web Apps (Standard tier)** |
| Backend API (linked to SWA via "Bring Your Own API") | **Azure Container Apps** (alternative: Azure App Service for Containers) |
| Container image registry | **Azure Container Registry (ACR)** |
| Database | **Azure Database for PostgreSQL — Flexible Server** (v15/16) |
| Secrets | **Azure Key Vault** (referenced via backend managed identity) |
| Identity / SSO | **Microsoft Entra ID** (app registration, integrated with SWA auth) |
| File uploads | **Azure Blob Storage** (private container) |
| AI services | **Azure OpenAI** (optional) |
| Logs / metrics | **Azure Monitor** + **Log Analytics** + **Application Insights** |
| CI/CD | **GitHub Actions** (SWA default) or **Azure DevOps Pipelines** with federated credentials |

SWA proxies requests under `/api/*` to the linked backend, so the frontend can use `VITE_API_BASE_URL=/api` same-origin — no CORS config needed.

---

## 1. Prerequisites

### Local / CI build host

| Tool | Minimum version | Notes |
|------|-----------------|-------|
| Node.js | 20.x LTS | Vite 7 / `@vitejs/plugin-react-swc` |
| npm | 10.x | Ships with Node 20 |
| Python | 3.12 | `backend/pyproject.toml` `requires-python = ">=3.12"` |
| pip | 24.x | Backend dependencies |
| Docker | 24.x | Build backend image for ACR |
| Git | 2.40+ | Source checkout / tagging |
| Azure CLI (`az`) | 2.60+ | Signed in to the production subscription |
| `az staticwebapp` | Built-in | Part of Azure CLI |
| `az containerapp` extension | Latest | `az extension add --name containerapp` |
| **SWA CLI** (`@azure/static-web-apps-cli`) | 1.x | `npm i -g @azure/static-web-apps-cli` — manual deploys + local preview |
| `psql` client | 16.x | Release-time DB checks |

### Azure resources (must exist before first deploy)

| Resource | Example name | Notes |
|----------|--------------|-------|
| Resource group | `rg-maaden-comms-prod` | |
| Static Web App | `swa-maaden-comms-prod` | **Standard tier** (free tier does not support linked BYO API, custom auth, private endpoints, or SLA) |
| Container registry | `acrmaadencommsprod` | Standard / Premium |
| Container Apps environment | `cae-maaden-comms-prod` | Linked to Log Analytics |
| Container App (backend) | `ca-maaden-comms-backend` | Managed identity enabled; linked to SWA as BYO API |
| PostgreSQL Flexible Server | `pg-maaden-comms-prod` | Private endpoint, HA enabled |
| Storage account (uploads) | `stmaadencommsblobprod` | Private container |
| Key Vault | `kv-maaden-comms-prod` | RBAC auth |
| Log Analytics workspace | `log-maaden-comms-prod` | |
| App Insights | `appi-maaden-comms-prod` | Connection string in backend env |
| Entra app registration | `MAADEN Comms Prod` | Redirect URI: `https://<swa-default-host>/.auth/login/aad/callback` **and** `https://<swa-default-host>/api/auth/entra/callback` — SWA auth uses the first; the backend Entra flow uses the second if you bypass SWA auth |
| Azure OpenAI resource | `oai-maaden-comms-prod` | Optional |

### Access / role assignments

- Deployer: **Contributor** on `rg-maaden-comms-prod`, **AcrPush** on the registry.
- Backend managed identity:
  - **Key Vault Secrets User** on the Key Vault
  - **Storage Blob Data Contributor** on the uploads storage account
  - **AcrPull** on the registry
- SWA deployment: GitHub repo secret `AZURE_STATIC_WEB_APPS_API_TOKEN` or an Azure DevOps service connection using federated identity.
- Migration runner (CI job or jump host): network path to the Flexible Server (private endpoint / jump VM) and DB admin credentials pulled from Key Vault at run time.

---

## 2. Environment Variables

Store every secret in **Key Vault**. Reference from Container Apps via `secretref:`. Variable names mirror `.env.example` and `backend/.env.example`. **Never commit values.**

### 2.1 Frontend (build-time — baked into the Vite bundle, uploaded to SWA)

| Variable | Purpose |
|----------|---------|
| `VITE_API_BASE_URL` | `/api` (SWA proxies to the linked backend same-origin) |
| `VITE_APP_ENV` | `production` |
| `VITE_LOCAL_MODE` | `false` |
| `VITE_USE_MSW` | `false` |
| `VITE_ENTRA_ENABLED` | `true` |
| `VITE_ENABLE_DEV_LOGIN` | `false` |
| `VITE_LLM_VALIDATION_API_BASE_URL` | Path to LLM sidecar if deployed (e.g. `/llm-api`) |

In the SWA portal (`Configuration` → `Application settings`) you can also set runtime env vars, but Vite consumes them at **build** time. Always set them as build-time vars in the CI workflow.

### 2.2 Backend (runtime — Container App env + Key Vault `secretref:`)

| Variable | Source | Purpose |
|----------|--------|---------|
| `BACKEND_APP_ENV` | plain | `production` |
| `BACKEND_APP_PORT` | plain | `8000` |
| `BACKEND_FRONTEND_URL` | plain | `https://<public-host>` (SWA custom domain) |
| `BACKEND_DATABASE_URL` | Key Vault | `postgresql+psycopg://…@pg-maaden-comms-prod.postgres.database.azure.com:5432/<db>?sslmode=require` |
| `BACKEND_AUTH_MODE` | plain | `entra` |
| `BACKEND_LOCAL_MODE` | plain | `false` |
| `BACKEND_DEV_LOGIN_ENABLED` | plain | `false` |
| `BACKEND_ENTRA_TENANT_ID` | Key Vault | |
| `BACKEND_ENTRA_CLIENT_ID` | Key Vault | |
| `BACKEND_ENTRA_CLIENT_SECRET` | Key Vault | |
| `BACKEND_ENTRA_AUDIENCE` | Key Vault | |
| `BACKEND_ENTRA_REDIRECT_URI` | plain | `https://<public-host>/api/auth/entra/callback` |
| `BACKEND_ENTRA_DISABLE_SIGNATURE_VALIDATION` | plain | `false` |
| `BACKEND_SESSION_SECRET` | Key Vault | 32+ bytes, rotate quarterly |
| `BACKEND_SESSION_COOKIE_NAME` | plain | `maaden_session` |
| `BACKEND_SESSION_COOKIE_SECURE` | plain | `true` |
| `BACKEND_SESSION_TTL_MINUTES` | plain | `480` |
| `BACKEND_AZURE_OPENAI_ENABLED` | plain | `true` if AI review is live |
| `BACKEND_AZURE_OPENAI_ENDPOINT` | Key Vault | |
| `BACKEND_AZURE_OPENAI_API_KEY` | Key Vault | |
| `BACKEND_AZURE_OPENAI_API_VERSION` | plain | e.g. `2024-10-21` |
| `BACKEND_AZURE_OPENAI_DEPLOYMENT` | plain | |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Key Vault | Backend auto-instrumentation |

### 2.3 Optional LLM validation sidecar (`server/index.js`)

| Variable | Purpose |
|----------|---------|
| `LLM_VALIDATION_SERVICE_PORT` | Container port (default `8787`) |
| `LLM_VALIDATION_PROVIDER` | `azure` in production |
| `OPENAI_API_KEY` | If provider is `openai` |
| `OPENAI_BASE_URL` | Azure OpenAI-compatible endpoint |
| `OPENAI_MODEL` | Model / deployment name |

---

## 3. Step-by-Step Deployment Procedure

Tag-based release: cut `vX.Y.Z` from `main`, build artifacts, migrate DB, roll backend, publish frontend to SWA.

### 3.1 Pre-flight (build host)

```bash
git fetch --tags
git checkout vX.Y.Z

npm ci
python3.12 -m venv backend/.venv
source backend/.venv/bin/activate
pip install --upgrade pip
pip install -e "backend[dev]"

npm run test
npm run lint
(cd backend && pytest)
```

### 3.2 Sign in to Azure

```bash
az login
az account set --subscription "<SUBSCRIPTION_ID>"

RG=rg-maaden-comms-prod
ACR=acrmaadencommsprod
APP=ca-maaden-comms-backend
KV=kv-maaden-comms-prod
SWA=swa-maaden-comms-prod
```

### 3.3 `staticwebapp.config.json` (checked into repo root)

SWA requires a config file at the deployed root. Ensure `staticwebapp.config.json` contains:

```json
{
  "routes": [
    { "route": "/api/*", "allowedRoles": ["anonymous"] },
    { "route": "/assets/*", "headers": { "cache-control": "public,max-age=31536000,immutable" } },
    { "route": "/index.html", "headers": { "cache-control": "no-cache" } }
  ],
  "navigationFallback": {
    "rewrite": "/index.html",
    "exclude": ["/assets/*", "/api/*"]
  },
  "mimeTypes": {
    ".mp4": "video/mp4"
  }
}
```

The `navigationFallback` block makes the React Router SPA work under deep links (e.g. `/home`, `/email-requests/123`).

### 3.4 Build the frontend bundle

Populate build-time values via a CI-generated `.env.production` (values pulled from Key Vault), then:

```bash
npm run build
# Output: dist/
# staticwebapp.config.json must end up inside dist/ — Vite copies anything from
# /public automatically, so place the file at public/staticwebapp.config.json
# OR copy it into dist/ after `npm run build`.
cp staticwebapp.config.json dist/staticwebapp.config.json
```

### 3.5 Build and push the backend image to ACR

```bash
az acr login --name "$ACR"
IMAGE="$ACR.azurecr.io/maaden-comms-backend:vX.Y.Z"

docker build -f backend/Dockerfile -t "$IMAGE" backend/
docker push "$IMAGE"
# Or in-registry: az acr build --registry $ACR --image maaden-comms-backend:vX.Y.Z backend/
```

### 3.6 Run database migrations

Before the backend rollout, from a host with network access to the Flexible Server:

```bash
export BACKEND_DATABASE_URL="$(az keyvault secret show \
  --vault-name $KV --name BACKEND-DATABASE-URL --query value -o tsv)"

cd backend
alembic current          # record for rollback
alembic upgrade head
alembic current          # verify head
```

If the migration fails, stop and execute Section 5 rollback.

### 3.7 Deploy the backend to Container Apps (blue/green)

```bash
az containerapp update \
  --name "$APP" --resource-group "$RG" \
  --image "$IMAGE" \
  --revision-suffix "vX-Y-Z"

# Validate the new revision on its preview URL, then flip traffic:
az containerapp ingress traffic set \
  --name "$APP" --resource-group "$RG" \
  --revision-weight latest=100

az containerapp revision list -n "$APP" -g "$RG" -o table
```

### 3.8 Link the backend to SWA (one-time, or if the backend was replaced)

```bash
BACKEND_RID=$(az containerapp show -n $APP -g $RG --query id -o tsv)

az staticwebapp backends link \
  --name "$SWA" --resource-group "$RG" \
  --backend-resource-id "$BACKEND_RID" \
  --backend-region "<region>"
```

Once linked, `https://<swa-host>/api/*` is proxied to the Container App. Re-linking is only required if the backend resource is recreated.

### 3.9 Deploy the frontend to Static Web Apps

**Option A — GitHub Actions (default flow).**
Push the `vX.Y.Z` tag; the SWA-generated workflow (`.github/workflows/azure-static-web-apps-*.yml`) builds and publishes automatically. Confirm the workflow includes:

```yaml
- name: Build And Deploy
  uses: Azure/static-web-apps-deploy@v1
  with:
    azure_static_web_apps_api_token: ${{ secrets.AZURE_STATIC_WEB_APPS_API_TOKEN }}
    repo_token: ${{ secrets.GITHUB_TOKEN }}
    action: "upload"
    app_location: "/"
    output_location: "dist"
    app_build_command: "npm run build"
    production_branch: "main"
```

**Option B — manual deploy via SWA CLI** (for hotfixes from the build host):

```bash
SWA_TOKEN=$(az staticwebapp secrets list --name $SWA -g $RG \
  --query properties.apiKey -o tsv)

swa deploy ./dist \
  --deployment-token "$SWA_TOKEN" \
  --env production
```

SWA publishes the new bundle globally within ~1 minute; there is no separate cache purge because SWA uses content-hash–based cache keys per revision.

### 3.10 Optional: roll the LLM validation sidecar

If deployed as a second Container App, repeat 3.5 – 3.7 with its image and update the SWA route (if any) to proxy `/llm-api/*` to it.

---

## 4. Verification

Every step must pass before declaring the deployment green.

### 4.1 SWA deployment status

```bash
az staticwebapp show -n "$SWA" -g "$RG" \
  --query "{host:defaultHostname, customDomains:customDomains}" -o table
```

In the portal: `Static Web Apps → <SWA> → Deployment history` — the latest deployment for branch `main` / tag `vX.Y.Z` must be **Ready**.

### 4.2 Backend health (via SWA proxy and direct)

```bash
# Through SWA — validates the linked-backend path end to end
curl -sSf https://<public-host>/api/health | jq .

# Direct Container App FQDN
BACKEND_FQDN=$(az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv)
curl -sSf "https://$BACKEND_FQDN/api/health" | jq .
```

Logs for the new revision:

```bash
az containerapp logs show \
  --name "$APP" --resource-group "$RG" \
  --revision "$(az containerapp revision list -n $APP -g $RG --query '[?properties.active].name | [0]' -o tsv)" \
  --tail 200
# Look for: "Application startup complete" and no ERROR-level lines
```

### 4.3 Database

```bash
alembic current     # matches deployed revision
alembic heads       # no pending heads

psql "$BACKEND_DATABASE_URL" -c "SELECT COUNT(*) FROM users;"
psql "$BACKEND_DATABASE_URL" -c "SELECT MAX(created_at) FROM email_requests;"
```

### 4.4 Frontend

1. Load `https://<public-host>/` in a clean browser window.
2. Confirm:
   - MAADEN brand header renders (no blank page, no FOUC)
   - DevTools Network: `index.html` → `Cache-Control: no-cache`; `/assets/*` → `max-age=31536000,immutable`
   - Response header `x-azure-ref` present (SWA edge); no 4xx/5xx on `/api/*`
   - Deep link reload (e.g. paste `https://<public-host>/home` into a new tab) returns `index.html`, confirming `navigationFallback` is honored
3. Sidebar → **Home** (`/home`) renders the hero video playlist + Communication Formats gallery.
4. Sidebar → **Dashboard** (`/`) renders KPIs and status cards with live counts.

### 4.5 End-to-end smoke tests

Using a non-admin Entra account in production:

- Sign in via Entra ID, land on `/`.
- Open **My Requests** — list loads.
- Create a draft email request, save, reload — draft persists.
- Open **Delegation** — loads without a 500 (regression guard for the tz-aware datetime fix on Postgres).
- Open **Pending Approvals** as an approver — list renders.

### 4.6 Azure observability

- **App Insights** → Live Metrics: new revision receiving traffic; failed-request rate < 1% for 15 minutes.
- **Log Analytics** `ContainerAppConsoleLogs_CL` for `$APP`: no repeated stack traces; `app.delegations` logger quiet.
- **SWA Metrics** (portal): request count rising on the new deployment; no spike in 4xx/5xx.
- **PostgreSQL Flexible Server metrics**: CPU, active connections, query duration within normal bands.

### 4.7 Sign-off

If 4.1 – 4.6 pass, post the release note in the deployment channel with:

- Version tag deployed
- SWA deployment ID (from portal)
- Alembic head before → after
- Container App revision name
- Operator on call
- Links to App Insights + SWA overview

---

## 5. Rollback

1. **Frontend (SWA)** — in the portal, `Static Web Apps → <SWA> → Deployment history`, select the previous successful deployment and click **Set as production**. For GitHub-driven deploys you can also revert the release tag and re-run the workflow.
2. **Backend** — shift traffic back to the prior healthy revision:
   ```bash
   az containerapp ingress traffic set \
     --name "$APP" --resource-group "$RG" \
     --revision-weight <previous-revision>=100 latest=0
   az containerapp revision deactivate --name $APP -g $RG --revision <bad-revision>
   ```
3. **Database** — run `alembic downgrade <prev_revision>` **only** if the prior backend is incompatible with the new schema. Most migrations are additive; coordinate with the DBA first.
4. File a post-mortem ticket with the failing verification step, App Insights trace IDs, SWA deployment ID, and root cause.
