# Deployment — Same Steps Every Time (Azure SQL Edition)

All resources are private-endpoint only. Run **everything** from the jumpbox VM (Windows or Linux) inside the VNet. Connect via **Azure Bastion**.

There are exactly **two scripts**: one for the backend, one for the frontend. You run the same scripts every release.

---

## What changed (PostgreSQL → Azure SQL)

| Layer | Before | Now |
|---|---|---|
| DB engine | PostgreSQL Flexible Server `ccd-notif-prtl` | **Azure SQL Database** |
| Driver | `psycopg` | `pyodbc` + **ODBC Driver 18 for SQL Server** |
| URL prefix | `postgresql+psycopg://...` | `mssql+pyodbc://...` |
| Connection port | 5432 | **1433** |
| TLS flag | `?sslmode=require` | `?Encrypt=yes&TrustServerCertificate=no` |

The application code itself didn't change — it uses generic SQLAlchemy types, so the same migrations work on Azure SQL. The Dockerfile, dependencies, and connection string have been updated for you.

---

## One-time setup (do this ONCE)

### 1. Azure SQL resources

| Resource | Example name |
|---|---|
| Azure SQL Server (logical) | `ccd-notif-sqlsrv-dev` |
| Azure SQL Database | `maaden_comms` |
| Private endpoint on the SQL server | `ccd-notif-sqlsrv-dev-private-endpoint` |
| Private DNS zone | `privatelink.database.windows.net` (linked to the VNet) |

In the SQL Server **Networking** page:
- **Public network access** → **Disable**.
- **Private endpoints** → confirm the PE exists.
- The Private DNS zone `privatelink.database.windows.net` must be linked to your VNet AND have an A record `ccd-notif-sqlsrv-dev` pointing to the PE's private IP.

### 2. Create the database and login

From the jumpbox:

```powershell
# Test DNS first — must NOT return 168.63.129.16
Resolve-DnsName ccd-notif-prtl.database.windows.net
Test-NetConnection ccd-notif-prtl.database.windows.net -Port 1433  # TcpTestSucceeded : True

# Create DB and app user (run as the SQL admin)
sqlcmd -S ccd-notif-prtl.database.windows.net -d master -U sqladmin -P "<admin-pw>" -Q "-- DB ccd-notify-prtl-db already exists (created in portal)"
sqlcmd -S ccd-notif-prtl.database.windows.net -d ccd-notify-prtl-db -U sqladmin -P "<admin-pw>" -Q "
CREATE USER appuser WITH PASSWORD = '<strong-pw>';
ALTER ROLE db_datareader ADD MEMBER appuser;
ALTER ROLE db_datawriter ADD MEMBER appuser;
ALTER ROLE db_ddladmin  ADD MEMBER appuser;  -- needed for Alembic migrations
"
```

### 3. Key Vault secrets

```bash
KV=ccd-notif-prtl-kv
DBURL='mssql+pyodbc://appuser:<strong-pw>@ccd-notif-prtl.database.windows.net:1433/ccd-notify-prtl-db?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no'
az keyvault secret set --vault-name $KV --name database-url --value "$DBURL"
az keyvault secret set --vault-name $KV --name jwt-secret-key --value "$(openssl rand -hex 32)"
```

### 4. Container App wiring (one-time)

Portal → `ccd-notif-prtl-ca`:
- **Identity → System assigned → On** (and grant `Key Vault Secrets User` + `AcrPull` once).
- **Secrets** → add Key Vault references named `database-url` and `jwt-secret-key`.
- **Containers → Edit → Environment variables** →
  - `BACKEND_DATABASE_URL` = `secretref:database-url`
  - `BACKEND_SESSION_SECRET` = `secretref:jwt-secret-key`

### 5. Static Web App link

Portal → `ccd-notif-prtl-devswa` → **APIs → Link** → select `ccd-notif-prtl-ca`.

### 6. Jumpbox tools (one-time)

**Linux jumpbox:**
```bash
sudo apt update && sudo apt install -y unixodbc-dev curl gnupg
curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add -
curl https://packages.microsoft.com/config/debian/12/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt update && sudo ACCEPT_EULA=Y apt install -y msodbcsql18 mssql-tools18
```

**Windows jumpbox:** download "ODBC Driver 18 for SQL Server" + "sqlcmd" from microsoft.com. (Already installed on most Windows Server images.)

---

## Every deploy: backend

```bash
cd ~/app/backend
TAG=$(date +%Y%m%d-%H%M)
az acr build --registry acrccdnotifprtldev --image ccd-notif-backend:$TAG .
az containerapp update \
  -n ccd-notif-prtl-ca \
  -g Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --image acrccdnotifprtldev.azurecr.io/ccd-notif-backend:$TAG
```

If the schema changed, run migrations once after the deploy (jumpbox):

```bash
cd ~/app/backend && source .venv/bin/activate
export BACKEND_DATABASE_URL='mssql+pyodbc://appuser:<pw>@ccd-notif-prtl.database.windows.net:1433/ccd-notify-prtl-db?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no'
alembic upgrade head
```

> Windows PowerShell equivalent:
> ```powershell
> $env:BACKEND_DATABASE_URL = 'mssql+pyodbc://appuser:<pw>@ccd-notif-prtl.database.windows.net:1433/ccd-notify-prtl-db?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no'
> .venv\Scripts\alembic.exe upgrade head
> ```

---

## Every deploy: frontend

```bash
cd ~/app
npm ci && npm run build
TOKEN=$(az staticwebapp secrets list \
  -n ccd-notif-prtl-devswa \
  -g Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --query properties.apiKey -o tsv)
swa deploy ./dist --deployment-token "$TOKEN" --env production
```

---

## Verify

```bash
# Backend
FQDN=$(az containerapp show -n ccd-notif-prtl-ca \
  -g Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --query properties.configuration.ingress.fqdn -o tsv)
curl -fsS https://$FQDN/health

# SWA → backend
SWA=$(az staticwebapp show -n ccd-notif-prtl-devswa \
  -g Maaden-NE-NONPROD-CCD-Notif-PRTL-RG \
  --query defaultHostname -o tsv)
curl -fsS https://$SWA/api/health
```

Both must return `200 OK`.

---

## If something breaks

| Problem | Where to look |
|---|---|
| Backend won't start, error mentions `IM002` or `Data source name not found` | ODBC driver missing in image — confirm Dockerfile has `msodbcsql18` |
| Backend won't start, error `Login failed for user 'appuser'` | Key Vault `database-url` has wrong password, OR `appuser` not created in `maaden_comms` DB |
| Backend won't start, error `Cannot open server '...' requested by the login` | Private endpoint DNS not resolving from inside the Container App's VNet |
| `Resolve-DnsName` returns `168.63.129.16` | Private DNS zone `privatelink.database.windows.net` not linked to the VNet, or A record missing |
| `Test-NetConnection ... -Port 1433` fails | NSG blocks 1433, or PE in different subnet |
| Alembic fails on `db_ddladmin` operations | `appuser` missing the role — re-run the `ALTER ROLE db_ddladmin` step |

For deeper detail, see [DEPLOYMENT_RUNBOOK.md](DEPLOYMENT_RUNBOOK.md) and [DEPLOYMENT_RUNBOOK_SWA.md](DEPLOYMENT_RUNBOOK_SWA.md).
