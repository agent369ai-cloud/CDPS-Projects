import subprocess, json

def get_secret(name):
    result = subprocess.run(
        ['az', 'keyvault', 'secret', 'show',
         '--vault-name', 'ccd-notif-prtl-kv',
         '--name', name, '--query', 'value', '-o', 'tsv'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"ERROR fetching secret '{name}': {result.stderr.strip()}")
        exit(1)
    val = result.stdout.strip()
    print(f"  ✓ {name} ({len(val)} chars)")
    return val

print("Fetching secrets from Key Vault...")
secrets = {
    "database-url":          get_secret("backend-database-url"),
    "jwt-secret-key":        get_secret("jwt-secret-key"),
    "backend-database-url":  get_secret("backend-database-url"),
    "graph-tenant-id":       get_secret("graph-tenant-id"),
    "graph-client-id":       get_secret("graph-client-id"),
    "graph-client-secret":   get_secret("graph-client-secret"),
    "azure-openai-endpoint": get_secret("azure-openai-endpoint"),
    "azure-openai-api-key":  get_secret("azure-openai-api-key"),
    "azure-openai-deployment": get_secret("azure-openai-deployment"),
}

body = {
    "location": "North Europe",
    "identity": {"type": "SystemAssigned"},
    "properties": {
        "configuration": {
            "activeRevisionsMode": "Single",
            "ingress": {"external": True, "targetPort": 80, "transport": "Auto"},
            "registries": [{"identity": "system", "server": "acrccdnotifprtldev.azurecr.io"}],
            "secrets": [{"name": k, "value": v} for k, v in secrets.items()]
        },
        "environmentId": "/subscriptions/813a0fdc-f866-45f1-a78d-4fab79e064b2/resourceGroups/Maaden-NE-NONPROD-CCD-Notif-PRTL-RG/providers/Microsoft.App/managedEnvironments/ccd-notif-portal-container-apps-environment",
        "template": {
            "revisionSuffix": "v6retry",
            "containers": [{
                "name": "backend",
                "image": "acrccdnotifprtldev.azurecr.io/maaden-ccd-notif-prtl-backend:latest",
                "resources": {"cpu": 0.25, "memory": "0.5Gi"},
                "env": [
                    {"name": "DATABASE_URL",            "secretRef": "database-url"},
                    {"name": "JWT_SECRET_KEY",          "secretRef": "jwt-secret-key"},
                    {"name": "APP_ENV",                 "value": "production"},
                    {"name": "BACKEND_DATABASE_URL",    "secretRef": "backend-database-url"},
                    {"name": "BACKEND_SESSION_SECRET",  "secretRef": "jwt-secret-key"},
                    {"name": "GRAPH_TENANT_ID",         "secretRef": "graph-tenant-id"},
                    {"name": "GRAPH_CLIENT_ID",         "secretRef": "graph-client-id"},
                    {"name": "GRAPH_CLIENT_SECRET",     "secretRef": "graph-client-secret"},
                    {"name": "AZURE_OPENAI_ENDPOINT",   "secretRef": "azure-openai-endpoint"},
                    {"name": "AZURE_OPENAI_API_KEY",    "secretRef": "azure-openai-api-key"},
                    {"name": "AZURE_OPENAI_DEPLOYMENT", "secretRef": "azure-openai-deployment"}
                ]
            }],
            "scale": {"minReplicas": 1, "maxReplicas": 10}
        }
    }
}

with open('/tmp/containerapp_body.json', 'w') as f:
    json.dump(body, f, indent=2)

print("\nDeploying to Container App...")
result = subprocess.run([
    'az', 'rest', '--method', 'PUT',
    '--url', 'https://management.azure.com/subscriptions/813a0fdc-f866-45f1-a78d-4fab79e064b2/resourceGroups/Maaden-NE-NONPROD-CCD-Notif-PRTL-RG/providers/Microsoft.App/containerApps/ccd-notif-prtl-ca?api-version=2024-03-01',
    '--body', '@/tmp/containerapp_body.json',
    '--headers', 'Content-Type=application/json'
], capture_output=True, text=True)

if result.returncode == 0:
    resp = json.loads(result.stdout)
    state = resp.get("properties", {}).get("provisioningState", "unknown")
    print(f"\n✅ Deploy submitted! Provisioning state: {state}")
    print(f"   Revision suffix: v6retry")
    print(f"   Image: acrccdnotifprtldev.azurecr.io/maaden-ccd-notif-prtl-backend:latest")
else:
    print(f"\n❌ Deploy failed!")
    print("STDOUT:", result.stdout[:2000])
    print("STDERR:", result.stderr[:500])
