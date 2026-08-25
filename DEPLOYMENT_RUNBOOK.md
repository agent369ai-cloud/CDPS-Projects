# MAADEN Corporate Communication System — Deployment Runbook (Azure)

This runbook describes how to deploy the MAADEN Corporate Communication System to **Microsoft Azure**. All services — frontend, backend, database, storage, secrets, identity, and observability — are hosted on Azure.

Reference Azure topology:

| Tier | Azure service |
|------|---------------|
| Frontend static hosting | **Azure Storage (Static Website)** fronted by **Azure Front Door** (CDN + WAF + TLS) |
| Backend API | **Azure Container Apps** (alternative: Azure App Service for Containers / AKS) |
| Database | **Azure Database for PostgreSQL — Flexible Server** (v15/16) |
| Container registry | **Azure Container Registry (ACR)** |
| Secrets | **Azure Key Vault** (referenced from Container Apps via managed identity) |
| Identity / SSO | **Microsoft Entra ID** (app registration) |
| AI services | **Azure OpenAI** (optional, for AI review workflow) |
| File uploads | **Azure Blob Storage** (private container) |
| Logs / metrics | **Azure Monitor** + **Log Analytics workspace** + **Application Insights** |
| CI/CD | **Azure DevOps Pipelines** or **GitHub Actions** with federated credentials to Azure |

---

## 1. Prerequisites

### Local / CI build host

| Tool | Minimum version | Notes |
|------|-----------------|-------|
| Node.js | 20.x LTS | Vite 7 / `@vitejs/plugin-react-swc` |
| npm | 10.x | Ships with Node 20 |
| Python | 3.12 | Enforced by `backend/pyproject.toml` |
| pip | 24.x | Backend dependencies |
| Docker | 24.x | Required to build backend image for ACR |
| Git | 2.40+ | Source checkout / tagging |
| Azure CLI (`az`) | 2.60+ | Authenticated to the production subscription |
| `az containerapp` extension | Latest | `az extension add --name containerapp` |
| `psql` client | 16.x | For release-time DB sanity checks |

### Azure resources (must exist before first deploy)

| Resource | Example name | Notes |
|----------|--------------|-------|
| Resource group | `rg-maaden-comms-prod` | Single RG for the environment |
| Container registry | `acrmaadencommsprod` | SKU: Standard or Premium |
| Log Analytics workspace | `log-maaden-comms-prod` | Used by Container Apps + App Insights |
| Container Apps environment | `cae-maaden-comms-prod` | Linked to the Log Analytics workspace |
| Container App (backend) | `ca-maaden-comms-backend` | Managed identity enabled |
| PostgreSQL Flexible Server | `pg-maaden-comms-prod` | Private endpoint, HA enabled |
| Storage account (frontend) | `stmaadencommswebprod` | Static website enabled, `$web` container |
| Storage account (uploads) | `stmaadencommsblobprod` | Private container, managed-identity access |
| Key Vault | `kv-maaden-comms-prod` | RBAC auth; backend managed identity has Secrets User |
| Front Door profile | `afd-maaden-comms-prod` | Origins: storage `$web` endpoint + backend Container App FQDN |
| App Insights | `appi-maaden-comms-prod` | Connection string injected into backend |
| Entra app registration | `MAADEN Comms Prod` | Redirect URI = `https://<public-host>/api/auth/entra/callback` |
| Azure OpenAI resource | `oai-maaden-comms-prod` | Optional; deployment `gpt-4o-mini` or equivalent |

### Access / role assignments

- Deployer has **Contributor** on `rg-maaden-comms-prod` and **AcrPush** on the registry.
- Backend Container App managed identity has:
  - **Key Vault Secrets User** on the Key Vault
  - **Storage Blob Data Contributor** on the uploads storage account
  - **AcrPull** on the container registry
- Migration runner (CI job or jump host) has network path to the Flexible Server (private endpoint / bastion / VPN) and DB admin credentials retrieved from Key Vault at run time.
- Entra admin can rotate the client secret when required.

---

## 2. Environment Variables

Store every secret in **Key Vault**. Reference them from Container Apps using the `secretref:` syntax so rotation propagates without a redeploy. Never commit actual values. Variable names mirror `.env.example` and `backend/.env.example`.

### 2.1 Frontend (build-time — baked into the Vite bundle)

| Variable | Purpose |
|----------|---------|
| `VITE_API_BASE_URL` | Public API base — `https://<front-door-host>/api` |
| `VITE_APP_ENV` | `production` |
| `VITE_LOCAL_MODE` | `false` |
| `VITE_USE_MSW` | `false` |
| `VITE_ENTRA_ENABLED` | `true` |
| `VITE_ENABLE_DEV_LOGIN` | `false` |
| `VITE_LLM_VALIDATION_API_BASE_URL` | Path to LLM sidecar if deployed |

### 2.2 Backend (runtime — Container App env + Key Vault secret refs)

| Variable | Source | Purpose |
|----------|--------|---------|
| `BACKEND_APP_ENV` | plain | `production` |
| `BACKEND_APP_PORT` | plain | `8000` (matches Container App ingress target port) |
| `BACKEND_FRONTEND_URL` | plain | Public Front Door URL |
| `BACKEND_DATABASE_URL` | Key Vault | `postgresql+psycopg://…@pg-maaden-comms-prod.postgres.database.azure.com:5432/<db>?sslmode=require` |
| `BACKEND_AUTH_MODE` | plain | `entra` |
| `BACKEND_LOCAL_MODE` | plain | `false` |
| `BACKEND_DEV_LOGIN_ENABLED` | plain | `false` |
| `BACKEND_ENTRA_TENANT_ID` | Key Vault | Entra tenant GUID |
| `BACKEND_ENTRA_CLIENT_ID` | Key Vault | App registration client ID |
| `BACKEND_ENTRA_CLIENT_SECRET` | Key Vault | App registration secret |
| `BACKEND_ENTRA_AUDIENCE` | Key Vault | Expected JWT `aud` |
| `BACKEND_ENTRA_REDIRECT_URI` | plain | `https://<public-host>/api/auth/entra/callback` |
| `BACKEND_ENTRA_DISABLE_SIGNATURE_VALIDATION` | plain | `false` |
| `BACKEND_SESSION_SECRET` | Key Vault | 32+ byte random; rotate quarterly |
| `BACKEND_SESSION_COOKIE_NAME` | plain | `maaden_session` |
| `BACKEND_SESSION_COOKIE_SECURE` | plain | `true` |
| `BACKEND_SESSION_TTL_MINUTES` | plain | `480` |
| `BACKEND_AZURE_OPENAI_ENABLED` | plain | `true` if AI review is live |
| `BACKEND_AZURE_OPENAI_ENDPOINT` | Key Vault | Azure OpenAI endpoint |
| `BACKEND_AZURE_OPENAI_API_KEY` | Key Vault | Azure OpenAI key |
| `BACKEND_AZURE_OPENAI_API_VERSION` | plain | e.g. `2024-10-21` |
| `BACKEND_AZURE_OPENAI_DEPLOYMENT` | plain | Deployment name |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Key Vault | App Insights SDK auto-instrumentation |

### 2.3 Optional LLM validation sidecar (`server/index.js`)

| Variable | Purpose |
|----------|---------|
| `LLM_VALIDATION_SERVICE_PORT` | Container port (default `8787`) |
| `LLM_VALIDATION_PROVIDER` | `azure` in production |
| `OPENAI_API_KEY` | If provider is `openai` |
| `OPENAI_BASE_URL` | Override for Azure OpenAI-compatible endpoint |
| `OPENAI_MODEL` | Model / deployment name |

---

## 3. Step-by-Step Deployment Procedure

Tag-based release: cut `vX.Y.Z` from `main`, build both artifacts, run DB migrations, roll forward the services.

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
CAE=cae-maaden-comms-prod
APP=ca-maaden-comms-backend
KV=kv-maaden-comms-prod
STATIC_SA=stmaadencommswebprod
AFD_PROFILE=afd-maaden-comms-prod
AFD_ENDPOINT=afd-maaden-comms
```

### 3.3 Build the frontend bundle

Inject build-time values via a CI-generated `.env.production` (pulled from Key Vault), then build:

```bash
npm run build
# Output: dist/
```

### 3.4 Build and push the backend image to ACR

```bash
az acr login --name "$ACR"

IMAGE="$ACR.azurecr.io/maaden-comms-backend:vX.Y.Z"

docker build -f backend/Dockerfile -t "$IMAGE" backend/
docker push "$IMAGE"
```

(Optional: replace the two commands above with `az acr build --registry $ACR --image maaden-comms-backend:vX.Y.Z backend/` to build inside ACR tasks.)

### 3.5 Run database migrations

Migrations run **before** the backend rollout, from a host with network access to the Flexible Server (Azure DevOps agent in the same VNet, a jump VM, or `az containerapp job`).

```bash
# Pull DSN from Key Vault at run time — do not persist it to disk
export BACKEND_DATABASE_URL="$(az keyvault secret show \
  --vault-name $KV --name BACKEND-DATABASE-URL --query value -o tsv)"

cd backend
alembic current          # record current revision (rollback reference)
alembic upgrade head
alembic current          # verify revision matches expected head
```

If migration fails, stop the release and execute the rollback plan (Section 5).

### 3.6 Deploy the backend to Container Apps

```bash
az containerapp update \
  --name "$APP" \
  --resource-group "$RG" \
  --image "$IMAGE" \
  --revision-suffix "vX-Y-Z"

# Traffic shift (blue/green). Validate the new revision on its preview URL first,
# then flip 100% of traffic:
az containerapp ingress traffic set \
  --name "$APP" --resource-group "$RG" \
  --revision-weight latest=100

az containerapp revision list \
  --name "$APP" --resource-group "$RG" -o table
```

### 3.7 Deploy the frontend to Azure Storage + Front Door

```bash
# Upload hashed assets first (safe to add), then index.html (cache-busting pointer flip)
az storage blob upload-batch \
  --account-name "$STATIC_SA" \
  --destination '$web' \
  --source dist \
  --pattern "assets/*" \
  --overwrite \
  --content-cache "public,max-age=31536000,immutable"

az storage blob upload \
  --account-name "$STATIC_SA" \
  --container-name '$web' \
  --name index.html \
  --file dist/index.html \
  --overwrite \
  --content-cache "no-cache"

# Purge CDN so clients pick up the new index.html + asset manifest immediately
az afd endpoint purge \
  --resource-group "$RG" \
  --profile-name "$AFD_PROFILE" \
  --endpoint-name "$AFD_ENDPOINT" \
  --content-paths "/index.html" "/assets/*"
```

### 3.8 Optional: roll the LLM validation sidecar

Deploy `server/index.js` as a second Container App (`ca-maaden-comms-llm`) following 3.4 and 3.6. Ensure `LLM_VALIDATION_PROVIDER=azure` and the Azure OpenAI key comes from Key Vault.

---

## 4. Verification

Every step must pass before declaring the deployment green.

### 4.1 Backend health

```bash
# Liveness via Front Door (public path)
curl -sSf https://<public-host>/api/health | jq .
# Expected: { "status": "ok", ... }

# Direct Container App FQDN (bypasses Front Door caching)
BACKEND_FQDN=$(az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv)
curl -sSf "https://$BACKEND_FQDN/api/health" | jq .
```

Check Container App logs for the first 2 minutes after rollout:

```bash
az containerapp logs show \
  --name "$APP" --resource-group "$RG" \
  --revision "$(az containerapp revision list -n $APP -g $RG --query '[?properties.active].name | [0]' -o tsv)" \
  --tail 200
# Look for: "Application startup complete" and no ERROR-level lines
```

### 4.2 Database

```bash
alembic current     # matches deployed version
alembic heads       # no pending heads

psql "$BACKEND_DATABASE_URL" -c "SELECT COUNT(*) FROM users;"
psql "$BACKEND_DATABASE_URL" -c "SELECT MAX(created_at) FROM email_requests;"
```

### 4.3 Frontend

1. Load `https://<public-host>/` in a clean browser window.
2. Confirm:
   - The MAADEN brand header renders (no blank page, no FOUC)
   - DevTools Network: `index.html` served with `Cache-Control: no-cache`, hashed assets with `max-age=31536000,immutable`
   - Front Door sets the expected `x-azure-ref` header; no 4xx/5xx on `/api/*`
3. Sidebar → **Home** (`/home`) renders the hero video playlist + Communication Formats gallery.
4. Sidebar → **Dashboard** (`/`) renders KPIs and status cards with live counts.

### 4.4 End-to-end smoke tests

Using a non-admin Entra account that exists in production:

- Sign in via Entra ID and land on `/`.
- Open **My Requests**, confirm the list loads.
- Create a draft email request, save, and reload — the draft persists.
- Open **Delegation** — the page loads without a 500 (regression guard for the tz-aware datetime fix on Postgres).
- Open **Pending Approvals** as an approver account — the list renders.

### 4.5 Azure observability

- **Application Insights** → Live Metrics: new revision is receiving traffic; failed-request rate stays within baseline (< 1% non-2xx) for 15 minutes.
- **Log Analytics**: `ContainerAppConsoleLogs_CL` for `$APP` shows no repeated stack traces; `app.delegations` logger is quiet.
- **Front Door metrics**: origin health is 100%; no spike in 5xx from the backend origin.
- **PostgreSQL Flexible Server metrics**: CPU, active connections, and query duration are within normal bands.

### 4.6 Sign-off

If 4.1 – 4.5 all pass, post the release note in the deployment channel with:

- Version tag deployed
- Alembic head before → after
- Container App revision name
- Front Door purge confirmation
- Operator on call
- Links to App Insights + Front Door dashboards

---

## 5. Rollback (Summary)

If any verification step fails:

1. **Backend** — shift traffic back to the prior healthy revision:
   ```bash
   az containerapp ingress traffic set \
     --name "$APP" --resource-group "$RG" \
     --revision-weight <previous-revision>=100 latest=0
   ```
   Then deactivate the bad revision: `az containerapp revision deactivate --name $APP -g $RG --revision <bad-revision>`.
2. **Frontend** — re-upload the previous `dist/` snapshot to `$web` and re-run the Front Door purge. Storage account versioning (if enabled) can restore prior blobs.
3. **Database** — run `alembic downgrade <prev_revision>` **only** if the prior backend is incompatible with the new schema. Most migrations are additive; coordinate with the DBA before downgrading.
4. File a post-mortem ticket with the failing verification step, App Insights trace IDs, and root cause.
