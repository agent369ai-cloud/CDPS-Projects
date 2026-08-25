# CCD Notification Portal — Backend Deployment Guide

**Owner (developer):** NumanF@maaden.com.sa
**Date:** 2026-05-03
**Goal:** Deploy the new backend image to Container App `ccd-notif-prtl-ca`, configure private DNS / Key Vault / SQL networking, and bring the production app online at https://icy-mushroom-0f7313803.7.azurestaticapps.net/

This guide is a **single sequential runbook** with 22 steps. Each step says:
- **Owner** — who runs it (Network admin / Developer / either)
- **Why** — what problem this step solves
- **GUI walkthrough** — exact Azure Portal navigation
- **CLI alternative** — equivalent `az` command(s)
- **Verify** — how to confirm it worked

Run steps in order top-to-bottom unless explicitly told otherwise.

---

## Environment summary (read first)

| Item | Value |
|---|---|
| Subscription | `Maaden Non-Production Environment` (`813a0fdc-f866-45f1-a78d-4fab79e064b2`) |
| Workload RG | `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG` |
| Workload VNet | `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet` |
| Container App | `ccd-notif-prtl-ca` (internal env, ingress HTTP/80) |
| Container Apps Env | `ccd-notif-portal-container-apps-environment` |
| ACR | `acrccdnotifprtldev` |
| ACR PE | `acr-ccd-notif-prtl-dev-private-endpoint` |
| Azure SQL Server | `ccd-notif-prtl` (no PE — public + firewall) |
| Azure SQL Database | `ccd-notify-prtl-db` (Paused — serverless auto-pause) |
| Key Vault | `ccd-notif-prtl-kv` |
| Key Vault PE | `ccd-notif-prtl-kv-private-endpoint` |
| Storage account | `maadenccdprtlstgblob` |
| Storage PE | `maadenccdprtlstgblob-private-endpoint` |
| Static Web App | `ccd-notif-prtl-devswa` (West Europe) |
| New backend image | `acrccdnotifprtldev.azurecr.io/ccd-notif-backend:20260503-154409` |
| Existing zones present | `privatelink.7.azurestaticapps.net`, `privatelink.northeurope.azurecontainerapps.io` |
| Zones MISSING (we'll create) | `privatelink.azurecr.io`, `privatelink.vaultcore.azure.net`, `privatelink.blob.core.windows.net` |

---

## Why this is needed

1. The Container App is currently running Microsoft's hello-world placeholder (`mcr.microsoft.com/k8se/quickstart:latest`). It must be rolled to the new backend image we just built.
2. `az containerapp update` to the new image fails because the workload VNet's corporate DNS server (`100.100.246.133`) cannot resolve `acrccdnotifprtldev.azurecr.io`. The expected `privatelink.azurecr.io` zone does not exist anywhere in this subscription.
3. Key Vault has a private endpoint but no `privatelink.vaultcore.azure.net` zone exists, so the backend can't reach KV → no secrets, no startup.
4. Storage account has a private endpoint but `privatelink.blob.core.windows.net` is missing → backend cannot upload rendered template images.
5. SQL Server has **no private endpoint** — it's reached over its public hostname. Container Apps subnet must be allow-listed.
6. SQL DB is auto-paused; first connection takes ~30 s cold start (informational, no action needed).
7. Static Web App's `/api/*` returns HTTP 500 because SWA outbound is not VNet-integrated, so it can't reach the Container App's private endpoint.
8. **(Discovered 2026-05-03)** The Container Apps Environment runs on the central enterprise VNet `maaden-ne-nonprod-vnet` (in `Maaden-NE-NONPROD-Network-RG`), and is **internal-only** (`internal: true`). Its DNS resolver (`100.100.246.133`) forwards lookups to that VNet's corporate DNS, which currently returns **SERVFAIL** for `login.microsoftonline.com` (public Microsoft endpoint). Without resolving that hostname, the Container App's managed identity cannot acquire an AAD token, and **all Key Vault references fail at provisioning time** even when RBAC is correct. See **Step 4.5** below.

---

# STEP 1 — Pre-flight verification (Developer)

**Why:** Before doing anything destructive, confirm the build artefact, the developer's local environment, and the credentials we'll need.

### CLI walkthrough

```bash
# 1.1 Confirm the new image exists in ACR
az acr repository show-tags \
  --name acrccdnotifprtldev \
  --repository ccd-notif-backend \
  --orderby time_desc -o table
```

Look for tag `20260503-154409` near the top. If missing, rerun the build:

```bash
cd "/Users/mobility/Documents/Internal Communication Solution/backend"
az acr build --registry acrccdnotifprtldev \
  --image ccd-notif-backend:$(date +%Y%m%d-%H%M%S) \
  --platform linux/amd64 .
```

```bash
# 1.2 Confirm the Container App is reachable to your account
az containerapp show \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --query "{name:name, image:properties.template.containers[0].image, identity:identity.type}" -o table
```

```bash
# 1.3 Confirm local .env still has all 7 secrets needed for Key Vault upload
grep -E '^(BACKEND_DATABASE_URL|BACKEND_GRAPH_|AZURE_OPENAI_)' "/Users/mobility/Documents/Internal Communication Solution/backend/.env"
```

You should see 7 lines. If any are missing, rebuild your `.env` from your password manager / 1Password before continuing — admin will need them in Step 11.

---

# STEP 2 — Confirm Microsoft Graph app registration is healthy (Developer)

**Why:** The backend uses an Azure AD app registration (not delegated, not user-bound — **Application** permission) with `User.Read.All` to read Azure AD users, departments, and distribution groups for the Audience Selector and Users & Roles features.

### GUI walkthrough

1. Azure Portal → **Microsoft Entra ID** → **App registrations**.
2. Filter by your `BACKEND_GRAPH_CLIENT_ID` GUID (paste it in the search box).
3. Click into the app → **API permissions**:
   - Confirm row: **Microsoft Graph → User.Read.All → Application** with green check **"Granted for ..."**.
   - If grey "Not granted", click **Grant admin consent for ...** at the top (only an Azure AD admin can do this).
4. Left blade → **Certificates & secrets** → **Client secrets** tab:
   - Confirm the secret you stored as `BACKEND_GRAPH_CLIENT_SECRET` is **not expired** (look at the **Expires** column).
   - If expired, click **+ New client secret** → 24 months → copy value → update your local `.env` and remember to update Step 11 too.

### Verify

The "Granted for ..." status must be green. If it's still grey after admin consent, the backend will throw 401s on Graph queries.

---

# STEP 3 — Create `privatelink.azurecr.io` zone, link to VNet, re-bind ACR private endpoint (Network Admin)

**Why:** This is the core blocker. The Container App can't pull the new image because DNS lookup of `acrccdnotifprtldev.azurecr.io` fails. The ACR has a private endpoint already, but no DNS zone tells the VNet how to resolve the hostname to the private IP. We create the zone, link it to the workload VNet, then bind the existing PE so A-records auto-populate.

### GUI walkthrough

#### 3a. Create the private DNS zone

1. Azure Portal → top search bar → **Private DNS zones** → click the service result.
2. Click **+ Create** at the top.
3. Form values:
   - **Subscription:** Maaden Non-Production Environment
   - **Resource group:** `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG`
   - **Name:** `privatelink.azurecr.io` (must be exact — no prefix, no suffix; it's case-sensitive)
4. Click **Review + create** → **Create**. Wait until "Your deployment is complete".

#### 3b. Link the new zone to the workload VNet

1. Open the newly-created zone (Portal home → Recent → `privatelink.azurecr.io`).
2. Left blade → **Virtual network links** → **+ Add**.
3. Form values:
   - **Link name:** `link-ccd-notif-prtl-vnet`
   - **I know the resource ID of the virtual network:** OFF
   - **Subscription:** Maaden Non-Production Environment
   - **Virtual network:** `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet`
   - **Enable auto registration:** ☐ **OFF** (important — auto-reg is for VMs, we want PE-driven records only)
4. **OK**. Wait ~30s for "Succeeded".

#### 3c. Re-bind the ACR private endpoint to this new zone

1. Portal → search for `acr-ccd-notif-prtl-dev-private-endpoint` → open it (it's a **Private endpoint** resource).
2. Left blade → **DNS configuration**.
3. If there's an existing entry pointing at a missing zone:
   - Tick the row → **Remove configuration** → confirm.
4. Click **+ Add configuration** at the top.
5. Form values:
   - **Configuration name:** `default`
   - **Subscription:** Maaden Non-Production Environment
   - **Resource group:** `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG`
   - **Private DNS zone:** `privatelink.azurecr.io`
6. **Save**.

#### 3d. Verify A-records appeared

1. Open the `privatelink.azurecr.io` zone → left blade → **Recordsets**.
2. You should see two A-records (within 30s):
   - `acrccdnotifprtldev` → `10.62.18.6`
   - `acrccdnotifprtldev-a4bshpd2eqchhuf3.northeurope.data` → `10.62.18.5`

If they don't appear, click **Refresh**. If still missing after 1 minute, the PE binding didn't save — repeat 3c.

### CLI alternative

```bash
RG=Maaden-NE-NONPROD-CCD-Notif-PRTL-RG
ZONE=privatelink.azurecr.io
PE=acr-ccd-notif-prtl-dev-private-endpoint
VNET=Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet

az network private-dns zone create --resource-group $RG --name $ZONE
az network private-dns link vnet create \
  --resource-group $RG --zone-name $ZONE \
  --name link-ccd-notif-prtl-vnet \
  --virtual-network $VNET --registration-enabled false

# Remove stale zone-group(s)
for NAME in $(az network private-endpoint dns-zone-group list \
  --resource-group $RG --endpoint-name $PE --query "[].name" -o tsv); do
  az network private-endpoint dns-zone-group delete \
    --resource-group $RG --endpoint-name $PE --name $NAME
done

# Bind to new zone
az network private-endpoint dns-zone-group create \
  --resource-group $RG --endpoint-name $PE \
  --name default --private-dns-zone $ZONE --zone-name acr

# Verify
az network private-dns record-set a list --resource-group $RG --zone-name $ZONE -o table
```

---

# STEP 4 — Create `privatelink.vaultcore.azure.net` zone, link, re-bind Key Vault PE (Network Admin)

**Why:** Backend reads its 7 secrets (DB connection string, Graph credentials, OpenAI key, etc.) from `ccd-notif-prtl-kv`. KV has a private endpoint already, but no DNS zone exists → backend can't even reach KV at startup → container crashloops.

### GUI walkthrough

Same procedure as Step 3, swap the names:

#### 4a. Create zone

1. **Private DNS zones** → **+ Create**.
2. RG: `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG`. Name: `privatelink.vaultcore.azure.net`. **Review + create** → **Create**.

#### 4b. Add VNet link

1. Open the zone → **Virtual network links** → **+ Add**.
2. Link name: `link-ccd-notif-prtl-vnet`. VNet: `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet`. Auto-registration: OFF. **OK**.

#### 4c. Re-bind KV private endpoint

1. Portal → search `ccd-notif-prtl-kv-private-endpoint` → open the PE.
2. **DNS configuration** → remove any stale row → **+ Add configuration**.
3. Configuration name: `default`. Private DNS zone: `privatelink.vaultcore.azure.net`. **Save**.

#### 4d. Verify

1. Zone → **Recordsets** → expect A-record `ccd-notif-prtl-kv` pointing to a `10.62.18.x` IP.

### CLI alternative

```bash
RG=Maaden-NE-NONPROD-CCD-Notif-PRTL-RG
ZONE=privatelink.vaultcore.azure.net
PE=ccd-notif-prtl-kv-private-endpoint

az network private-dns zone create --resource-group $RG --name $ZONE
az network private-dns link vnet create \
  --resource-group $RG --zone-name $ZONE \
  --name link-ccd-notif-prtl-vnet \
  --virtual-network Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet \
  --registration-enabled false
az network private-endpoint dns-zone-group create \
  --resource-group $RG --endpoint-name $PE \
  --name default --private-dns-zone $ZONE --zone-name kv
az network private-dns record-set a list --resource-group $RG --zone-name $ZONE -o table
```

---

# STEP 5 — Create `privatelink.blob.core.windows.net` zone, link, re-bind Storage PE (Network Admin)

**Why:** Backend writes Playwright-rendered template PNGs to `maadenccdprtlstgblob` for the vision pipeline cache. Same DNS issue as ACR/KV — PE exists, zone missing, no resolution.

### GUI walkthrough

Same as Steps 3–4. Swap names:

- Zone: `privatelink.blob.core.windows.net`
- PE to bind: `maadenccdprtlstgblob-private-endpoint`
- DNS configuration **resource type / sub-resource**: `blob`

Step-by-step:

1. **Private DNS zones** → **+ Create** in workload RG → name `privatelink.blob.core.windows.net` → **Create**.
2. Open zone → **Virtual network links** → **+ Add** → link name `link-ccd-notif-prtl-vnet`, VNet `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet`, auto-registration OFF.
3. Portal → open `maadenccdprtlstgblob-private-endpoint` → **DNS configuration** → **+ Add configuration** → zone `privatelink.blob.core.windows.net`. Save.
4. Verify A-record `maadenccdprtlstgblob` appears in the zone's Recordsets.

### CLI alternative

```bash
RG=Maaden-NE-NONPROD-CCD-Notif-PRTL-RG
ZONE=privatelink.blob.core.windows.net
PE=maadenccdprtlstgblob-private-endpoint

az network private-dns zone create --resource-group $RG --name $ZONE
az network private-dns link vnet create \
  --resource-group $RG --zone-name $ZONE \
  --name link-ccd-notif-prtl-vnet \
  --virtual-network Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet \
  --registration-enabled false
az network private-endpoint dns-zone-group create \
  --resource-group $RG --endpoint-name $PE \
  --name default --private-dns-zone $ZONE --zone-name blob
```

---

# STEP 5.5 — Fix corporate DNS resolution for `login.microsoftonline.com` from the CAE (Network Admin) — **CRITICAL BLOCKER**

**Why:** Discovered 2026-05-03 during Step 12 / 13 attempts. When the Container App tries to resolve a Key Vault reference, its managed identity calls AAD at `https://login.microsoftonline.com/...` to fetch an access token. The CAE's internal DNS resolver (`100.100.246.133`) forwards public lookups to the corporate DNS server in the central network VNet, which is currently returning **SERVFAIL** for `login.microsoftonline.com`. Result:

```
authentication with Azure Key Vault failed using managed identity 'system'.
… unable to resolve an endpoint: server response error:
Get "https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration":
dial tcp: lookup login.microsoftonline.com on 100.100.246.133:53: server misbehaving
```

This will block **every** revision that uses KV-ref secrets (Steps 12–14). RBAC and zone setup are irrelevant until DNS is fixed.

### Confirmed environment facts
| Property | Value |
|---|---|
| CAE infrastructure subnet | `/subscriptions/.../Maaden-NE-NONPROD-Network-RG/.../maaden-ne-nonprod-vnet/subnets/CCD-Cont-App-Private-Endpoint-sNet` |
| CAE `internal` | `true` (no public ingress; egress depends on VNet routing) |
| CAE platform reserved DNS IP | inherited (resolver is `100.100.246.133`) |
| Affected lookups | `login.microsoftonline.com` (public) — and likely all other `*.microsoft.com`, `*.azure.com` public endpoints |

### What network admin must verify / fix

1. **DNS forwarders on the corporate DNS server** (the upstream behind `100.100.246.133` on `maaden-ne-nonprod-vnet`):
   - Confirm it forwards unknown / public queries to a working public resolver (Azure DNS `168.63.129.16`, or a corporate egress resolver).
   - Confirm `login.microsoftonline.com` resolves successfully from any VM on `maaden-ne-nonprod-vnet`:
     ```
     nslookup login.microsoftonline.com
     ```
     Expected: a public `*.cloudapp.azure.com` or Akamai CNAME resolving to a public IP. SERVFAIL = broken.
2. **Outbound 443 to AAD** (`login.microsoftonline.com`, `login.microsoft.com`) from the CAE subnet via NSG / Azure Firewall / corporate egress proxy. Even if DNS resolves, the connection must be allowed.
3. **Other public endpoints** the Container App will need at startup:
   - `login.microsoftonline.com` (token)
   - `<tenant>.vault.azure.net` (KV — resolved via `privatelink.vaultcore.azure.net` from Step 4)
   - `acrccdnotifprtldev.azurecr.io` (image pull — via `privatelink.azurecr.io` from Step 3)
   - `*.openai.azure.com` (Azure OpenAI — public endpoint unless you have a private endpoint for it)
   - `<account>.blob.core.windows.net` (Storage — via `privatelink.blob.core.windows.net` from Step 5)
   - `graph.microsoft.com` (Graph API — public)

### Verify (Network Admin)

After fix, on a VM inside `maaden-ne-nonprod-vnet`:
```bash
nslookup login.microsoftonline.com 100.100.246.133
nslookup graph.microsoft.com 100.100.246.133
curl -v https://login.microsoftonline.com/common/discovery/keys
```
All three should return successful answers / HTTP 200.

### Verify (Developer, after admin reports done)

Re-run the failing flow. Update the container app to trigger a fresh revision; if DNS is fixed, KV refs will resolve and the revision will reach `Provisioned`/`Healthy`:
```bash
az containerapp update --name $CA --resource-group $RG --revision-suffix dnsfix-$(date +%s)
az containerapp revision list --name $CA --resource-group $RG \
  --query "[].{name:name,state:properties.provisioningState,replicas:properties.replicas,active:properties.active}" -o table
az containerapp logs show --name $CA --resource-group $RG --type system --tail 30
```
Look for absence of `lookup login.microsoftonline.com … server misbehaving` in system logs.

---

# STEP 6 — Authorise Container Apps subnet on SQL Server (Network Admin)

**Why:** SQL Server `ccd-notif-prtl` has **no private endpoint** in this RG. Backend reaches the public hostname `ccd-notif-prtl.database.windows.net`. Without a firewall or VNet rule, every backend SQL connection is rejected with `Cannot open server requested by the login`.

### GUI walkthrough

#### 6a. Find the Container Apps subnet name

1. Portal → open `ccd-notif-portal-container-apps-environment` → **Networking** (left blade).
2. Note the **Infrastructure subnet** value, e.g. `infra-subnet` or `default`. You'll need this in step 6c.

#### 6b. Open SQL Server networking

1. Portal → open SQL server `ccd-notif-prtl` (the **server**, NOT the `ccd-notify-prtl-db` database).
2. Left blade → **Networking** (under Security).

#### 6c. Choose access policy

**Public network access:** select `Selected networks`.

Then **either**:

**Option 6c.A (broadest, easiest)** — tick **Allow Azure services and resources to access this server**. Click **Save**. Done.

**Option 6c.B (preferred, scoped)** — Under **Virtual networks** click **+ Add existing virtual network**:
- **Name:** `allow-ca-subnet`
- **Subscription:** Maaden Non-Production Environment
- **Virtual network:** `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet`
- **Subnet:** the infrastructure subnet from 6a
- If a banner says "Microsoft.Sql service endpoint is not enabled on this subnet → click **Enable**". Approve it.
Click **Save** at the top.

### CLI alternative

```bash
# Option B (preferred): scoped to Container Apps subnet
SUBNET_ID=$(az network vnet subnet show \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --vnet-name Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet \
  --name <infra-subnet-name> --query id -o tsv)

az sql server vnet-rule create \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --server ccd-notif-prtl \
  --name allow-ca-subnet \
  --subnet $SUBNET_ID
```

> **Note about the paused DB:** `ccd-notify-prtl-db` is in **Paused** state (serverless auto-pause). The first SQL query from the backend after deploy will warm-start it (~30 s). This is normal — don't panic if the first request hangs briefly.

---

# STEP 7 — Confirm outbound 443 from Container Apps subnet (Network Admin)

**Why:** Even with private endpoints set up, the backend still calls public Azure endpoints for: (a) Azure AD token acquisition, (b) Microsoft Graph for user lookups, (c) Azure OpenAI gpt-5 (assuming AOAI is public — there's no PE for it in this RG).

### GUI walkthrough

1. Portal → `ccd-notif-portal-container-apps-environment` → **Networking** → note the **Virtual network** and **Infrastructure subnet**.
2. Open the subnet → check **Network security group** assignments (if any).
3. If an NSG is attached, review **Outbound security rules**. Ensure no `Deny` rules block `443` to:
   - `login.microsoftonline.com` (Azure AD token endpoint)
   - `graph.microsoft.com` (Graph user/group/department search)
   - `*.openai.azure.com` (Azure OpenAI vision pipeline)
   - `*.azurecr.io` (image pull — already handled by PE in Step 3, but public fallback may still apply)
4. If an Azure Firewall sits between the subnet and the internet, ensure these FQDNs are in its **Application rules** allowlist.

### Verify

There's no clean automated check. After Step 14, if the backend logs show `httpx.ConnectError` to `graph.microsoft.com`, return here.

---

# STEP 8 — Set up SWA → Container App private routing (Network Admin)

**Why:** Static Web App `ccd-notif-prtl-devswa` (West Europe) currently 500s on `/api/*` because its outbound traffic can't reach the private Container App in North Europe. We solve this by integrating the SWA with the workload VNet.

### Option 8.A (recommended): SWA VNet integration

**Pre-requisites:**
- SWA must be on **Standard** SKU (Free SKU does not support VNet integration).

#### 8A.1 — Confirm SWA SKU

1. Portal → `ccd-notif-prtl-devswa` → **Overview**.
2. Look for **Plan: Standard** in the Essentials section. If "Free", upgrade first: **Hosting plan** (left blade) → switch to Standard. *(Costs ~$9/month per app.)*

#### 8A.2 — Prepare a delegated subnet inside the workload VNet

SWA outbound integration needs a subnet that's:
- Empty (no other resources)
- Delegated to `Microsoft.Web/serverFarms`
- /27 minimum size

Create one if needed:
1. Portal → `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet` → **Subnets** → **+ Subnet**.
2. Form values:
   - **Name:** `swa-outbound-subnet`
   - **Subnet address range:** `10.62.18.32/27` (pick an unused /27 inside the VNet's address space — verify against Address space first)
   - **Delegate subnet to a service:** `Microsoft.Web/serverFarms`
   - Leave Service endpoints empty.
3. **Save**.

#### 8A.3 — Bind SWA outbound to that subnet

1. Open `ccd-notif-prtl-devswa` → **Networking** (left blade) → **Outbound traffic configuration** (or **Private endpoint integration** depending on Portal version).
2. Under **Virtual network integration**, click **Configure**.
3. Choose:
   - **Subscription:** Maaden Non-Production Environment
   - **Virtual network:** `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG-vnet`
   - **Subnet:** `swa-outbound-subnet`
4. **Save**.

#### 8A.4 — Link the Container App as a backend

1. SWA → **APIs** (left blade, sometimes named **Linked backends**).
2. Click the **Production** environment row → **Link**.
3. Form values:
   - **Backend resource type:** Container App
   - **Subscription / RG:** Maaden Non-Production Environment / `Maaden-NE-NONPROD-CCD-Notif-PRTL-RG`
   - **Container app name:** `ccd-notif-prtl-ca`
4. **Link**. Wait ~5 minutes for routing to propagate.

### Option 8.B (only if 8.A is blocked): External Container App env

Switching the Container Apps env from **Internal** to **External** requires recreating the env (data plane redeploy — destroys & recreates the env). Then the Container App gets a public FQDN. Front it with Front Door / App Gateway + WAF + Azure AD auth. This is heavier and changes the security posture — only do this if 8.A is denied by policy.

### Verify

After this step is done (and the backend is also up after Steps 14–17), the validation checklist in Step 22 will tell you if it's working.

---

# STEP 9 — Populate Key Vault secrets (Network/Platform Admin, with developer-supplied values)

**Why:** The Container App can't authenticate to anything until KV holds the credentials. Developer hands the admin the values; admin uploads them.

### Values to upload (developer provides these)

| Secret name in KV | Value source | Example format |
|---|---|---|
| `backend-database-url` | Azure SQL connection string | `mssql+pyodbc://<u>:<p>@ccd-notif-prtl.database.windows.net:1433/ccd-notify-prtl-db?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no` |
| `graph-tenant-id` | Azure AD tenant GUID | `bfe67e2e-bec6-4055-9aac-7cf4b793e890` |
| `graph-client-id` | Graph app registration ID | GUID |
| `graph-client-secret` | Graph app client secret value | Long random string |
| `azure-openai-endpoint` | AOAI resource endpoint | `https://<aoai>.openai.azure.com` |
| `azure-openai-api-key` | AOAI key (Key1 from Keys and Endpoint) | Long random string |
| `azure-openai-deployment` | gpt-5 deployment name | e.g. `gpt-5-mini` |

### GUI walkthrough

1. Portal → `ccd-notif-prtl-kv` → **Secrets** (left blade, under Objects).
2. For each row in the table above:
   - Click **+ Generate/Import** at the top.
   - **Upload options:** Manual.
   - **Name:** the secret name from column 1 (exact, lowercase, hyphens).
   - **Secret value:** paste from developer.
   - **Content type:** leave blank.
   - **Activation/Expiration date:** leave default unless you have a rotation policy.
   - **Enabled:** Yes.
   - **Create**.
3. After 7 secrets are uploaded, **Refresh** the list. All 7 must show **Enabled = Yes**.

### CLI alternative

```bash
KV=ccd-notif-prtl-kv

az keyvault secret set --vault-name $KV --name backend-database-url   --value "<connection-string>"
az keyvault secret set --vault-name $KV --name graph-tenant-id        --value "<tenant-guid>"
az keyvault secret set --vault-name $KV --name graph-client-id        --value "<client-id>"
az keyvault secret set --vault-name $KV --name graph-client-secret    --value "<client-secret>"
az keyvault secret set --vault-name $KV --name azure-openai-endpoint  --value "https://<aoai>.openai.azure.com"
az keyvault secret set --vault-name $KV --name azure-openai-api-key   --value "<aoai-key>"
az keyvault secret set --vault-name $KV --name azure-openai-deployment --value "<gpt5-deployment-name>"
```

---

# STEP 10 — Assign managed identity to Container App + grant ACR pull (Developer)

**Why:** The Container App needs an identity to (a) pull images from ACR and (b) read secrets from Key Vault. We assign it a **system-assigned managed identity** (auto-managed by Azure, no secrets to rotate) and grant it `AcrPull` on the registry.

### CLI walkthrough

```bash
RG=Maaden-NE-NONPROD-CCD-Notif-PRTL-RG
CA=ccd-notif-prtl-ca
ACR=acrccdnotifprtldev

# 10a. Enable system-assigned managed identity
az containerapp identity assign \
  --name $CA --resource-group $RG --system-assigned
```

```bash
# 10b. Capture the identity's principalId for Step 11
PRINCIPAL_ID=$(az containerapp identity show \
  --name $CA --resource-group $RG --query principalId -o tsv)

echo ""
echo "▶▶▶ PRINCIPAL_ID = $PRINCIPAL_ID"
echo "    Send this GUID to the network admin for Step 11."
echo ""
```

```bash
# 10c. Grant AcrPull role on the registry
ACR_ID=$(az acr show --name $ACR --query id -o tsv)
az role assignment create \
  --assignee $PRINCIPAL_ID --role AcrPull --scope $ACR_ID
```

> ⚠️ **If CLI fails with `AADSTS53001: domain_joined`** (Conditional Access blocks role assignments from non-domain-joined Macs):
> Use the **Portal GUI** instead — same effect.
> 1. Portal → search **`acrccdnotifprtldev`** → open the ACR.
> 2. Left nav → **Access control (IAM)** → **+ Add** → **Add role assignment**.
> 3. **Role tab:** search `AcrPull` → select → **Next**.
> 4. **Members tab:** Assign access to → **Managed identity** → **+ Select members** → Managed identity = **Container App** → pick `ccd-notif-prtl-ca` → **Select** → **Next**.
> 5. **Review + assign**.
> Verify on the same IAM page → **Role assignments** tab → filter **AcrPull** → row for `ccd-notif-prtl-ca` should appear.

```bash
# 10d. Tell the Container App to use that identity for ACR pulls
az containerapp registry set \
  --name $CA --resource-group $RG \
  --server $ACR.azurecr.io --identity system
```

### Verify

```bash
az containerapp registry list \
  --name $CA --resource-group $RG --query "[].{server:server,identity:identity}" -o table
```

Should show `server = acrccdnotifprtldev.azurecr.io` and `identity = system`.

---

# STEP 11 — Grant Container App identity `Key Vault Secrets User` role (Network/Platform Admin)

**Why:** Even though the identity exists (Step 10) and KV secrets exist (Step 9), the identity doesn't yet have permission to **read** those secrets. This step grants the minimal RBAC role.

### Pre-requisite

Developer must complete Step 10 first and share the `PRINCIPAL_ID` GUID with admin.

### GUI walkthrough

1. Portal → `ccd-notif-prtl-kv` → **Access control (IAM)** (left blade).
2. Click **+ Add** → **Add role assignment**.
3. **Role tab:**
   - In the search box type `Key Vault Secrets User` → click the row → **Next**.
4. **Members tab:**
   - **Assign access to:** Managed identity (radio).
   - Click **+ Select members**.
   - **Subscription:** Maaden Non-Production Environment.
   - **Managed identity:** drop-down → **Container App**.
   - In the list, find and click `ccd-notif-prtl-ca` → **Select**.
   - Back in the Members tab → **Next**.
5. **Review + assign tab** → **Review + assign**.

### CLI alternative

```bash
PRINCIPAL_ID=<from-developer>
KV_ID=$(az keyvault show --name ccd-notif-prtl-kv --query id -o tsv)

az role assignment create \
  --assignee $PRINCIPAL_ID \
  --role "Key Vault Secrets User" \
  --scope $KV_ID
```

> ⚠️ **If the developer is doing this from a non-domain-joined Mac** and gets `AADSTS53001: domain_joined`, fall back to the GUI walkthrough above. The CA policy blocks Microsoft Graph–backed role assignments from such devices, but the Portal flow runs server-side and works fine.

> **Key Vault access mode check:** The CLI/Portal RBAC flow above assumes the KV is using **RBAC**. If it's still on legacy **Access Policies** (Portal → KV → **Access configuration**), you must instead add an Access Policy granting `Get` on **Secrets** to the Container App's principal — the role assignment alone won't take effect.

### Verify

Portal → KV → IAM → **Role assignments** tab → confirm `ccd-notif-prtl-ca` is listed with role **Key Vault Secrets User**.

---

# STEP 12 — Bind Key Vault references on the Container App (Developer)

**Why:** The Container App needs to map each KV secret into a Container-App-level secret reference. Then we'll use those refs as environment variables in Step 13.

### Pre-requisites

Steps 9 (secrets exist), 10 (identity assigned), 11 (RBAC granted) must all be done.

### CLI walkthrough

```bash
RG=Maaden-NE-NONPROD-CCD-Notif-PRTL-RG
CA=ccd-notif-prtl-ca
KV=ccd-notif-prtl-kv

az containerapp secret set \
  --name $CA --resource-group $RG \
  --secrets \
    backend-database-url=keyvaultref:https://$KV.vault.azure.net/secrets/backend-database-url,identityref:system \
    graph-tenant-id=keyvaultref:https://$KV.vault.azure.net/secrets/graph-tenant-id,identityref:system \
    graph-client-id=keyvaultref:https://$KV.vault.azure.net/secrets/graph-client-id,identityref:system \
    graph-client-secret=keyvaultref:https://$KV.vault.azure.net/secrets/graph-client-secret,identityref:system \
    azure-openai-endpoint=keyvaultref:https://$KV.vault.azure.net/secrets/azure-openai-endpoint,identityref:system \
    azure-openai-api-key=keyvaultref:https://$KV.vault.azure.net/secrets/azure-openai-api-key,identityref:system \
    azure-openai-deployment=keyvaultref:https://$KV.vault.azure.net/secrets/azure-openai-deployment,identityref:system
```

### Verify

```bash
az containerapp secret list --name $CA --resource-group $RG -o table
```

Should list 7 entries with `keyVaultUrl` populated for each.

> **If this command fails with `KeyVaultReferenceNotFound`** — DNS Step 4 isn't propagated yet, or RBAC Step 11 isn't applied. Wait 60 s and retry; if still failing, re-verify the A-record exists in `privatelink.vaultcore.azure.net`.

---

# STEP 13 — Roll the Container App to the new image + set env vars (Developer)

**Why:** Single command that replaces the placeholder hello-world image with the new backend image AND wires the 7 secret references as environment variables. This triggers a new revision deploy.

### CLI walkthrough

```bash
az containerapp update \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --image acrccdnotifprtldev.azurecr.io/ccd-notif-backend:20260503-154409 \
  --set-env-vars \
    BACKEND_DATABASE_URL=secretref:backend-database-url \
    BACKEND_GRAPH_TENANT_ID=secretref:graph-tenant-id \
    BACKEND_GRAPH_CLIENT_ID=secretref:graph-client-id \
    BACKEND_GRAPH_CLIENT_SECRET=secretref:graph-client-secret \
    AZURE_OPENAI_ENDPOINT=secretref:azure-openai-endpoint \
    AZURE_OPENAI_API_KEY=secretref:azure-openai-api-key \
    AZURE_OPENAI_DEPLOYMENT=secretref:azure-openai-deployment
```

This kicks off a new revision. The CLI returns once the platform has accepted the change — the new revision still needs ~60–120 s to be Healthy.

---

# STEP 14 — Wait for new revision to be Healthy (Developer)

**Why:** A new revision can be in Activating state for up to ~2 minutes. Confirm it's actually serving traffic before running migrations.

### CLI walkthrough

```bash
az containerapp revision list \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --query "[?properties.active].{name:name, replicas:properties.replicas, healthState:properties.healthState, image:properties.template.containers[0].image}" \
  -o table
```

Look for:
- `replicas >= 1`
- `healthState = Healthy`
- `image = acrccdnotifprtldev.azurecr.io/ccd-notif-backend:20260503-154409`

If `replicas = 0` after 3 minutes → see Troubleshooting at Step 21.

If `healthState = Unhealthy` → grab logs:

```bash
az containerapp logs show \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --tail 200
```

---

# STEP 15 — Run database migrations inside the running revision (Developer)

**Why:** The schema in `ccd-notify-prtl-db` is empty (or stale). The backend includes Alembic migrations under `backend/alembic/versions/` that must be applied before any API call works.

### CLI walkthrough

```bash
az containerapp exec \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --command "alembic upgrade head"
```

Expected output: a series of `Running upgrade ...` lines, ending with the latest revision (around `20260502_0025`).

> **First run will be slow** — the SQL DB is auto-paused. The first connection wakes it (~30 s). Subsequent migrations are normal.

> **If you see `connection refused` or `Cannot open server`** — Step 6 isn't applied. The Container App can't reach SQL. Have admin re-verify firewall / VNet rule.

> **If you see `KeyVaultReferenceNotFound`** — secrets aren't resolved. Re-check Steps 4, 9, 11, 12.

### Verify

After the migrations succeed, check that tables exist:

```bash
az containerapp exec \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --command "python -c \"from app.database import engine; from sqlalchemy import inspect; print(inspect(engine).get_table_names())\""
```

You should see at least: `users`, `email_templates`, `certificate_templates`, `email_requests`, `certificate_requests`, `workflow_configurations`, etc. — about 25 tables in total.

---

# STEP 16 — Seed the initial admin user + reference data (Developer)

**Why:** Migrations create empty tables. The app needs at least one `system_admin` user to log in, plus the seeded workflow configuration and brand guidelines.

### CLI walkthrough

```bash
az containerapp exec \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --command "python -m app.seed_data"
```

This script (in `backend/app/seed_data.py`) creates:
- The `numanf@maaden.com.sa` `system_admin` user
- Default workflow configurations for email + certificate request types
- Default brand guideline rows (logo placement rules, color palette, etc.)
- The 4 default email templates and 2 default certificate templates

### Verify

```bash
az containerapp exec \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --command "python -c \"from app.database import SessionLocal; from app.models import User; db=SessionLocal(); print([(u.email, u.role) for u in db.query(User).all()])\""
```

Expect at least one row with role `system_admin`.

---

# STEP 17 — Re-deploy the frontend to SWA with current build (Developer)

**Why:** The SWA was last deployed before the backend even existed. While the static files themselves don't change much, re-deploying ensures the latest UI code (line-manager fix, AudienceSelector, AI brand review panel) is live on https://icy-mushroom-0f7313803.7.azurestaticapps.net/.

### CLI walkthrough

```bash
cd "/Users/mobility/Documents/Internal Communication Solution"

# Build the production bundle
npm install
npm run build

# Deploy to SWA (assumes you have swa CLI configured)
swa deploy ./dist --env production
```

If `swa` isn't installed:
```bash
npm install -g @azure/static-web-apps-cli
swa login   # only first time
```

Watch for `✔ Project deployed to https://icy-mushroom-0f7313803.7.azurestaticapps.net 🚀`.

### Verify

Browse https://icy-mushroom-0f7313803.7.azurestaticapps.net/ — the login page should load (no API calls yet, just static HTML/JS).

---

# STEP 18 — Run a synthetic backend smoke test (Developer)

**Why:** Before involving the SWA layer (which has its own auth pipeline), confirm the backend itself responds correctly. This isolates "is the backend healthy" from "is SWA→backend wired up".

### Pre-requisite

If your machine has any access to the workload VNet (e.g. via VPN or the Windows server you mentioned), run from there. If not, this step is observational — proceed to Step 19 and test from the SWA.

### CLI walkthrough (only if you can reach the VNet)

```bash
# Health endpoint (no auth required)
curl -i https://ccd-notif-prtl-ca.ashybush-ac194f17.northeurope.azurecontainerapps.io/api/health
```

Expect HTTP 200 with `{"status":"ok"}`.

```bash
# OpenAPI doc (no auth)
curl -i https://ccd-notif-prtl-ca.ashybush-ac194f17.northeurope.azurecontainerapps.io/api/openapi.json | head -30
```

Expect a JSON response starting with `{"openapi":"3.1.0",...}`.

If you can't reach the URL from your Mac, skip and let the SWA-level test in Step 19 prove it.

---

# STEP 19 — End-to-end smoke test from the SWA (Developer)

**Why:** This is the ultimate proof that everything — DNS, SQL, KV, Graph, AOAI, SWA routing — is wired up correctly.

### Manual test plan

Open https://icy-mushroom-0f7313803.7.azurestaticapps.net/ in a fresh incognito window:

1. **Sign in** with `NumanF@maaden.com.sa` (Azure AD redirects).
   - ✅ If the dashboard loads → SWA → Container App → SQL → KV all wired up.
   - ❌ If the dashboard stays blank with 500 errors in DevTools → Step 8 isn't done.

2. Open **Users & Roles** (left nav) → click **Add User from Azure AD** → type "Faisal".
   - ✅ Live results pop within ~500ms → Graph integration works.
   - ❌ Empty list / "Azure AD search is not configured" → Steps 9 (graph secrets), 11 (KV access), or 7 (outbound 443 to graph.microsoft.com).

3. Open **Templates → Email Templates** → click any template.
   - ✅ Loads, renders preview → Storage account access works.
   - ❌ "Template image failed to load" → Step 5 (blob private DNS) not propagated.

4. Open **Submit Request → Email Communication** → fill in a test request → click **Submit for Approval**.
   - ✅ Redirects to "My Requests" page with status `Pending Review`.
   - Open the new request → ✅ Workflow timeline shows **Line Manager** stage with a non-self approver.

5. Open **Templates** as `system_admin` → run **AI Brand Review** on a template.
   - ✅ Returns a JSON report with passed/failed checks → AOAI vision pipeline works.
   - ❌ Spinning forever / 500 → Step 7 (outbound to `*.openai.azure.com`) or Step 9 (AOAI key wrong).

If any step fails, jump to **Step 21 (Troubleshooting)**.

---

# STEP 20 — Validation checklist (Developer)

Tick off each as confirmed:

- [ ] Container App revision shows new image tag and `Healthy` (Step 14)
- [ ] Database has 25 tables and seeded admin user (Steps 15, 16)
- [ ] Frontend builds and deploys to SWA URL without errors (Step 17)
- [ ] `https://icy-mushroom-...azurestaticapps.net/` loads the login page (Step 17)
- [ ] Sign-in via Azure AD reaches the dashboard (Step 19.1)
- [ ] **Users & Roles** Azure AD search returns live results (Step 19.2)
- [ ] Templates list loads with no 500s (Step 19.3)
- [ ] Submit-request flow advances to `Pending Review` with **non-submitter** line manager (Step 19.4)
- [ ] AI Brand Review returns a JSON report on a sample template (Step 19.5)
- [ ] Vision pipeline `/admin/vision-pipeline/stats` returns counts (admin-only)
- [ ] DevTools Console shows zero errors on dashboard load

If all 11 items are ✅, the deployment is complete.

---

# STEP 21 — Troubleshooting (Developer reference)

| Symptom | Likely cause | Fix |
|---|---|---|
| `failed to resolve registry 'acrccdnotifprtldev.azurecr.io'` | Step 3 not done | Re-run 3a–3d |
| Container App revision stuck `Activating` for >5 min | Image pull failure (10c skipped) or KV ref unresolved | `az containerapp logs show ... --tail 200` and check for `KeyVaultReferenceNotFound` or `unauthorized` |
| `KeyVaultReferenceNotFound` in logs | Step 11 RBAC not applied OR Step 4 DNS not propagated | Re-verify both |
| `Cannot open server "ccd-notif-prtl"` | Step 6 firewall / VNet rule missing | Have admin add the subnet |
| Graph `401 InvalidAuthenticationToken` | Step 2 admin consent missing OR `graph-client-secret` wrong in KV | Re-grant consent / re-upload secret |
| AOAI `401` | Wrong `azure-openai-api-key` or wrong `azure-openai-deployment` | Re-upload to KV (Step 9) and re-roll Container App (Step 13) |
| SWA `/api/*` returns 500 | Step 8 not done | SWA → APIs → Link Container App |
| First SQL query takes 30 s | DB auto-paused (expected) | Wait — subsequent queries fast |
| Migrations fail with `pyodbc.OperationalError` | DB Paused + Step 6 firewall missing | Open Portal SQL DB → click **Resume** to manually wake → re-run migrations |

### Useful diagnostic commands

```bash
# Live logs
az containerapp logs show \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --follow

# Show env vars
az containerapp show --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --query "properties.template.containers[0].env" -o table

# Show current revision
az containerapp revision list --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG -o table

# Restart current revision
az containerapp revision restart \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --revision <revision-name-from-above>
```

---

# STEP 22 — Rollback (only if disaster strikes)

**Why:** If the new image misbehaves catastrophically, revert to the placeholder so the Container App is at least running. SWA will continue to 500 (expected).

```bash
az containerapp update \
  --name ccd-notif-prtl-ca \
  --resource-group Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --image mcr.microsoft.com/k8se/quickstart:latest
```

For data rollback (if migrations broke something), restore the SQL DB to the earliest restore point (visible on the DB Overview blade — was `2026-05-02 08:34 UTC` earlier).

---

# Quick reference — execution order at a glance

| # | Step | Owner | Pre-req |
|---|---|---|---|
| 1 | Pre-flight verification | Developer | — |
| 2 | Confirm Graph app registration | Developer | — |
| 3 | Create `privatelink.azurecr.io` + bind PE | Network admin | — |
| 4 | Create `privatelink.vaultcore.azure.net` + bind PE | Network admin | — |
| 5 | Create `privatelink.blob.core.windows.net` + bind PE | Network admin | — |
| **5.5** | **Fix corporate DNS for `login.microsoftonline.com` from CAE subnet** | **Network admin** | **— (BLOCKER for 12+)** |
| 6 | SQL Server firewall / VNet rule | Network admin | — |
| 7 | Outbound 443 firewall confirmation | Network admin | — |
| 8 | SWA → Container App private routing | Network admin | — |
| 9 | Populate KV secrets | Network admin (developer provides) | — |
| 10 | Assign MI + AcrPull on Container App | Developer | 3 |
| 11 | KV Secrets User on MI | Network admin | 9, 10 |
| 12 | Bind KV refs on Container App | Developer | 5.5, 11 |
| 13 | Roll image + env vars | Developer | 4, 12 |
| 14 | Verify revision Healthy | Developer | 13 |
| 15 | Alembic migrations | Developer | 6, 14 |
| 16 | Seed admin user + reference data | Developer | 15 |
| 17 | Re-deploy frontend | Developer | — (parallel) |
| 18 | Backend smoke test | Developer | 14 |
| 19 | End-to-end SWA smoke test | Developer | 8, 16, 17 |
| 20 | Validation checklist | Developer | 19 |
| 21 | (Troubleshooting reference) | — | — |
| 22 | (Rollback reference) | — | — |

**Total: 8 admin steps + 12 developer steps + 2 reference sections.**

---

## Hand-off summary

When sending this guide:

- **To network admin:** "Please complete Steps 3–8, 9, and 11. Steps 3–8 can run in parallel. Step 11 needs the developer's `PRINCIPAL_ID` GUID (they'll send after their Step 10). Step 9 needs the 7 secret values from the developer."
- **To self (developer):** "Steps 1, 2, 10, 12–20. Step 17 (frontend redeploy) can run any time after build verification."

Once both sides finish, run the validation checklist (Step 20). If anything fails, see Step 21.
