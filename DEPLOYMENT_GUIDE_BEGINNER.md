# Beginner Deployment Guide — Private Endpoint Setup

This guide walks you through deploying the **MAADEN Internal Communication Solution** to the Azure resources shown in your portal. It is written for a beginner. Read every step before running it.

> **Resource group:** `Maaden-NE-NONPROD-CCD-NotifPortal-RG` (region: North Europe)
> **Environment:** `dev`

---

## 0. What you already have in Azure

Match each item to its job. You will not create these — they exist already.

| Resource in portal | Type | What it does in this app |
|---|---|---|
| `acrccdnotifprtldev` | Container Registry (ACR) | Stores the **backend Docker image** that the Container App runs |
| `acr-ccd-notif-prtl-dev-private-endpoint` | Private endpoint | Lets resources inside the VNet pull images from ACR without going over the internet |
| `ccd-notif-portal-container-apps-environment` | Container Apps Environment | The "host" / VNet boundary that the backend Container App lives inside |
| `ccd-notif-prtl-ca` | Container App | Runs the **backend** (FastAPI on port 8000) |
| `ccd-notif-prtl-ca-private-endpoint` | Private endpoint | Lets the Static Web App reach the backend privately |
| `ccd-notif-prtl-devswa` | Static Web App (SWA) | Hosts the **React frontend** |
| `ccd-notif-prtl-devsw-prievate-endpoint` | Private endpoint | Private endpoint for the Static Web App |
| `ccd-notif-prtl` | Azure Database for PostgreSQL Flexible Server | Stores all application data |
| `ccd-notif-prtl-kv` | Key Vault | Stores secrets (DB password, JWT keys, API keys) |
| `ccd-notif-prtl-kv-private-endpoint` | Private endpoint | Lets the Container App read secrets privately |
| `*.nic.*` | Network Interface | Auto-created by each private endpoint — ignore |

### What "private endpoint" means for you

Each of those resources is **blocked from the public internet**. They only accept traffic from inside the Azure Virtual Network (VNet) they are attached to.

**Consequence:** You cannot run `docker push`, `psql`, `az keyvault secret set`, or `swa deploy` directly from your laptop. You must run those commands from somewhere **inside the VNet**.

You have three common options. Pick one before starting.

| Option | Best for | Cost / effort |
|---|---|---|
| **A. Jumpbox VM** in the same VNet (Windows or Linux + Bastion) | Beginners, manual one-off deploys | ~ small VM cost; click-ops setup |
| **B. Self-hosted GitHub Actions / Azure DevOps agent** in the VNet | Repeatable CI/CD | Slightly more setup, free runner |
| **C. Temporarily enable public access** on each resource, deploy, then disable | Emergency only — **not recommended** | Leaks attack surface |

This guide uses **Option A (Jumpbox)** because it is the simplest to learn on.

---

## 1. Prerequisites checklist

Before you start, confirm:

- [ ] You can sign in to the Azure Portal as a user with **Contributor** + **Key Vault Secrets Officer** + **AcrPush** roles on the resource group.
- [ ] You know the VNet + subnet name that the private endpoints are connected to. (Portal → any private endpoint → **Overview** → "Virtual network/subnet".)
- [ ] You have the source code on your laptop (this repo: `/Users/mobility/Documents/Internal Communication Solution`).
- [ ] Local tools installed:
  - Docker Desktop
  - Node.js 20 LTS + npm 10
  - Python 3.12
  - Azure CLI 2.60+ (`az --version`)
  - `psql` 16 client
- [ ] You have run the app locally at least once (`npm run dev` and `npm run dev:api`) so you know it works.

Run once on your laptop:

```bash
az login
az account set --subscription "<your-subscription-name-or-id>"
az extension add --name containerapp --upgrade
az extension add --name staticwebapp --upgrade
```

---

## 2. Create a jumpbox (one-time)

The jumpbox is a small Linux VM inside the same VNet as your private endpoints. You will SSH into it via **Azure Bastion** and run all deployment commands from there.

### 2.1 Create the VM

Portal → **Create a resource** → **Virtual machine**.

| Field | Value |
|---|---|
| Resource group | `Maaden-NE-NONPROD-CCD-NotifPortal-RG` |
| VM name | `ccd-notif-jumpbox-dev` |
| Region | North Europe (same as the rest) |
| Image | Ubuntu Server 22.04 LTS |
| Size | `Standard_B2s` (cheap, enough for builds) |
| Authentication | SSH public key (paste your `~/.ssh/id_rsa.pub`) |
| Inbound ports | **None** (we use Bastion) |
| Networking → VNet | The same VNet as the private endpoints |
| Networking → Subnet | Any worker subnet (NOT the `AzureBastionSubnet` and NOT the private-endpoint subnet) |
| Public IP | **None** |

Click **Review + create** → **Create**.

### 2.2 Create Azure Bastion (if not already there)

Portal → search **Bastion** → **Create**. Attach to the same VNet, accept defaults. Wait ~5 min.

### 2.3 Connect

Portal → your VM → **Connect** → **Bastion** → enter the SSH username and private key. You now have a shell *inside the VNet*.

### 2.4 Install tools on the jumpbox

```bash
sudo apt update && sudo apt install -y \
    docker.io git curl ca-certificates gnupg \
    postgresql-client-16 python3.12 python3.12-venv python3-pip

# Azure CLI
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# Node 20
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt install -y nodejs

# Allow your user to use docker without sudo
sudo usermod -aG docker $USER
newgrp docker

# SWA CLI
sudo npm i -g @azure/static-web-apps-cli

# Login
az login --use-device-code
az account set --subscription "<your-subscription>"
```

### 2.5 Get the source code on the jumpbox

Either `git clone` from your repo, or `scp` the project folder via Bastion file transfer. The rest of this guide assumes the project is at `~/Internal Communication Solution`.

```bash
cd ~/"Internal Communication Solution"
```

---

## 3. Populate Key Vault with secrets

Do this once (and any time a secret changes). Run from the **jumpbox**.

### 3.1 List the secrets the app needs

The backend reads (at minimum):

| Secret name in Key Vault | Value |
|---|---|
| `database-url` | `postgresql+psycopg://<dbuser>:<dbpass>@ccd-notif-prtl.postgres.database.azure.com:5432/<dbname>?sslmode=require` |
| `jwt-secret-key` | A random 64-char string (`openssl rand -hex 32`) |
| `entra-client-id` | (if SSO enabled) |
| `entra-client-secret` | (if SSO enabled) |
| `entra-tenant-id` | (if SSO enabled) |
| `openai-api-key` | (only if AI review enabled) |

### 3.2 Set each secret

```bash
KV=ccd-notif-prtl-kv

az keyvault secret set --vault-name $KV --name database-url \
  --value "postgresql+psycopg://maadenadmin:CHANGE_ME@ccd-notif-prtl.postgres.database.azure.com:5432/maaden_comms?sslmode=require"

az keyvault secret set --vault-name $KV --name jwt-secret-key \
  --value "$(openssl rand -hex 32)"
```

> If `az keyvault secret set` returns `403 Forbidden`, you don't have **Key Vault Secrets Officer** role yet. Ask your Azure admin to assign it on the Key Vault.

### 3.3 Give the Container App permission to read the vault

Portal → `ccd-notif-prtl-ca` → **Identity** → **System assigned** → **On** → save. Copy the **Object (principal) ID**.

```bash
PRINCIPAL_ID=<paste-object-id>
KV_ID=$(az keyvault show -n ccd-notif-prtl-kv --query id -o tsv)

az role assignment create --assignee $PRINCIPAL_ID \
  --role "Key Vault Secrets User" --scope $KV_ID
```

---

## 4. Prepare the PostgreSQL database

Run from the **jumpbox** (the DB private endpoint resolves only inside the VNet).

### 4.1 First connection — create the application database

```bash
# Replace with the admin user you set when the server was created
PGPASSWORD='<admin-password>' psql \
  -h ccd-notif-prtl.postgres.database.azure.com \
  -U maadenadmin -d postgres -c "CREATE DATABASE maaden_comms;"
```

If you get `could not translate host name`, the DNS for the private endpoint isn't reaching your jumpbox. Confirm the **Private DNS zone** `privatelink.postgres.database.azure.com` is linked to the jumpbox VNet (Portal → Private DNS Zones → that zone → **Virtual network links**).

### 4.2 Run the Alembic migrations

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .

export DATABASE_URL="postgresql+psycopg://maadenadmin:<admin-password>@ccd-notif-prtl.postgres.database.azure.com:5432/maaden_comms?sslmode=require"

alembic upgrade head
```

You should see each migration applied with no errors.

---

## 5. Build and push the backend image to ACR

Still on the **jumpbox**.

### 5.1 Log in to ACR

```bash
az acr login --name acrccdnotifprtldev
```

This works because the ACR private endpoint is reachable from inside the VNet.

### 5.2 Build the image

```bash
cd ~/"Internal Communication Solution"/backend

IMAGE=acrccdnotifprtldev.azurecr.io/ccd-notif-backend:$(date +%Y%m%d-%H%M)
docker build -t $IMAGE .
```

Builds typically take 3–5 minutes. The image is ~250 MB.

### 5.3 Push

```bash
docker push $IMAGE
echo "Pushed: $IMAGE"
```

Note the full image tag — you need it in the next step.

> **Alternative (no Docker on jumpbox):** Use ACR build tasks instead:
> ```
> az acr build --registry acrccdnotifprtldev \
>   --image ccd-notif-backend:$(date +%Y%m%d-%H%M) ./backend
> ```

---

## 6. Deploy the backend to Container Apps

### 6.1 Allow the Container App to pull from ACR

```bash
ACR_ID=$(az acr show -n acrccdnotifprtldev --query id -o tsv)
PRINCIPAL_ID=$(az containerapp show -n ccd-notif-prtl-ca \
  -g Maaden-NE-NONPROD-CCD-NotifPortal-RG \
  --query identity.principalId -o tsv)

az role assignment create --assignee $PRINCIPAL_ID \
  --role AcrPull --scope $ACR_ID
```

### 6.2 Update the Container App to use the new image

```bash
az containerapp update \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-NotifPortal-RG \
  --image $IMAGE \
  --set-env-vars \
      DATABASE_URL=secretref:database-url \
      JWT_SECRET_KEY=secretref:jwt-secret-key \
      APP_ENV=production
```

### 6.3 Wire Key Vault secrets into Container Apps secrets

Portal → `ccd-notif-prtl-ca` → **Secrets** → **Add**.

| Name | Type | Key Vault URL |
|---|---|---|
| `database-url` | Key Vault reference | `https://ccd-notif-prtl-kv.vault.azure.net/secrets/database-url` |
| `jwt-secret-key` | Key Vault reference | `https://ccd-notif-prtl-kv.vault.azure.net/secrets/jwt-secret-key` |

Then **Containers → Edit and deploy → Environment variables** → confirm `DATABASE_URL` and `JWT_SECRET_KEY` are set as **Reference a secret** pointing at those names.

### 6.4 Verify the backend is running

From the jumpbox:

```bash
FQDN=$(az containerapp show -n ccd-notif-prtl-ca \
  -g Maaden-NE-NONPROD-CCD-NotifPortal-RG \
  --query properties.configuration.ingress.fqdn -o tsv)

curl -fsS https://$FQDN/health
# expect: {"status":"ok"}
```

If this fails: Portal → `ccd-notif-prtl-ca` → **Log stream** to see startup errors. The most common cause is a wrong `DATABASE_URL`.

---

## 7. Build and deploy the frontend (Static Web App)

You can do this from your **laptop** OR the jumpbox — SWA deploy uses an outbound HTTPS deploy token, so it doesn't need to be inside the VNet. The jumpbox is fine.

### 7.1 Build the frontend

```bash
cd ~/"Internal Communication Solution"
npm ci

# Tell the build to send /api calls to the SWA proxy (which forwards to Container App)
cat > .env.production <<EOF
VITE_API_BASE_URL=/api
VITE_APP_ENV=production
VITE_LOCAL_MODE=false
VITE_USE_MSW=false
VITE_ENTRA_ENABLED=true
VITE_ENABLE_DEV_LOGIN=false
EOF

npm run build
# output is in ./dist
```

### 7.2 Get the SWA deploy token

```bash
az staticwebapp secrets list \
  --name ccd-notif-prtl-devswa \
  -g Maaden-NE-NONPROD-CCD-NotifPortal-RG \
  --query properties.apiKey -o tsv
```

Copy the value.

### 7.3 Deploy

```bash
swa deploy ./dist \
  --deployment-token "<paste-token>" \
  --env production
```

Typical output ends with a public URL like `https://<random>.azurestaticapps.net`.

### 7.4 Link the SWA to the Container App backend ("Bring Your Own API")

Portal → `ccd-notif-prtl-devswa` → **APIs** → **Link** → choose **Container App** → select `ccd-notif-prtl-ca` → save.

After this, requests to `https://<swa-url>/api/*` are proxied to the Container App, same-origin. No CORS setup needed.

### 7.5 Smoke test

Open the SWA URL in a browser. Sign in. Check:
- Login redirects work
- The dashboard loads
- Browser DevTools → Network → `/api/...` calls return 200

---

## 8. Post-deploy checks

Run from the jumpbox:

```bash
# Backend healthy?
curl -fsS https://$FQDN/health

# DB reachable from the Container App? Check logs:
az containerapp logs show -n ccd-notif-prtl-ca \
  -g Maaden-NE-NONPROD-CCD-NotifPortal-RG --follow

# SWA + backend wired correctly?
curl -fsS https://<swa-url>/api/health
```

All three must return `200 OK`.

---

## 9. Common beginner pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `docker push` hangs forever | You're on your laptop, not the jumpbox. ACR rejects public pushes. | Run from the jumpbox. |
| `az acr login` says "unauthorized" | Your account doesn't have `AcrPush`. | Ask admin to assign `AcrPush` on the registry. |
| Container App stuck in "Activating" | Image tag wrong, or `AcrPull` role not granted. | Check **Revisions → Failed** logs. |
| Backend returns 500 on `/health` | `DATABASE_URL` wrong or DB not migrated. | Re-check secret value; re-run `alembic upgrade head`. |
| `psql` says "host not found" from jumpbox | Private DNS zone not linked to the jumpbox VNet. | Portal → Private DNS Zones → link VNet. |
| SWA `/api/*` returns 404 | API not linked, or backend ingress is **internal**. | Portal → SWA → APIs → Link. Make sure Container App ingress is **External** *or* the link uses the private endpoint. |
| Build on jumpbox runs out of disk | `Standard_B2s` has only 30 GB. | Resize to `B2ms` or prune old Docker images: `docker system prune -af`. |

---

## 10. Releasing a new version (the short version)

After the first deploy, every new release is just steps **5 → 6 → 7**:

```bash
# Backend
cd ~/"Internal Communication Solution"/backend
IMAGE=acrccdnotifprtldev.azurecr.io/ccd-notif-backend:$(date +%Y%m%d-%H%M)
az acr build --registry acrccdnotifprtldev --image ${IMAGE#*/} .
az containerapp update -n ccd-notif-prtl-ca \
  -g Maaden-NE-NONPROD-CCD-NotifPortal-RG --image $IMAGE

# Frontend
cd ~/"Internal Communication Solution"
npm ci && npm run build
swa deploy ./dist --deployment-token "<token>" --env production
```

That's the whole loop. Save the deploy token in a `.envrc` (gitignored) so you don't have to re-fetch it each time.

---

## 11. Where to get help

- Container App not starting → **Log stream** tab in the portal.
- Database connection issues → run `psql` from the jumpbox first, *then* from inside the Container App via `az containerapp exec`.
- Private endpoint / DNS issues → Portal → the private endpoint resource → **DNS configuration** tab. Every entry there must show "yes" under "Private DNS zone group integration".
- For deeper detail than this beginner guide, see [DEPLOYMENT_RUNBOOK.md](DEPLOYMENT_RUNBOOK.md) and [DEPLOYMENT_RUNBOOK_SWA.md](DEPLOYMENT_RUNBOOK_SWA.md).
