
# =============================================================================
# AI-200 CAPSTONE LAB
# AI Document Processing Platform
#
# Deployment method: Azure CLI commands executed from Python
# No Bicep, ARM templates, or Terraform
#
# Requirements:
#   - Python 3.11+
#   - Azure CLI
#   - Azure CLI containerapp extension
#   - An Azure subscription with sufficient permissions and quota
#
# Expected application folders:
#   api/Dockerfile
#   worker/function_app.py
#   worker/host.json
#   worker/requirements.txt
# =============================================================================

# =============================================================================
# CHANGE THESE VALUES
# =============================================================================

RESOURCE_GROUP = "rg-atbai200-dev"
LOCATION = "francecentral"

# Azure OpenAI model deployment
MODEL_NAME = "gpt-4o"
MODEL_VERSION = "2024-11-20"
MODEL_DEPLOYMENT = "document-classifier"

# Existing model deployment can be reused by setting this to False.
CREATE_AI_MODEL = True

# =============================================================================
# DO NOT MODIFY BELOW THIS LINE
# =============================================================================

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from pathlib import Path


# =============================================================================
# GLOBAL CONFIGURATION
# =============================================================================

ROOT = Path(__file__).resolve().parent
API_DIR = ROOT / "api"
WORKER_DIR = ROOT / "worker"
GENERATED_DIR = ROOT / "generated"

IMAGE_NAME = "document-api:v1"
QUEUE_NAME = "document-processing"
CONTAINER_NAME = "documents"
COSMOS_DATABASE = "documentsdb"
COSMOS_CONTAINER = "results"
TARGET_PORT = "8000"

AZURE_CORE_ONLY_SHOW_ERRORS = "true"
os.environ["AZURE_CORE_ONLY_SHOW_ERRORS"] = AZURE_CORE_ONLY_SHOW_ERRORS

NAMES = {}
STATE = {}


# =============================================================================
# CLI HELPERS
# =============================================================================

def run(command, description="", quiet=False, check=True):
    """
    Execute a command without using a shell.

    Azure CLI errors are displayed when a command fails.
    """

    if description:
        print(f"\n[+] {description}")

    executable = shutil.which(command[0])

    if not executable:
        raise RuntimeError(
            f"Executable not found: {command[0]}"
        )

    command = [executable, *command[1:]]

    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "AZURE_CORE_ONLY_SHOW_ERRORS": "true",
            "NO_COLOR": "1",
        },
        check=False
    )

    if result.returncode != 0:
        if check:
            error = result.stderr or result.stdout
            raise RuntimeError(
                f"Command failed:\n"
                f"{' '.join(command)}\n\n"
                f"{error}"
            )
        return None

    if not quiet and result.stdout.strip():
        print(result.stdout.strip())

    return result.stdout.strip()


def az(*args, description="", check=True):
    return run(
        ["az", *args, "--only-show-errors"],
        description=description,
        quiet=True,
        check=check
    )


def az_json(*args, description="", check=True):
    output = az(
        *args,
        "-o", "json",
        description=description,
        check=check
    )

    if not output:
        return None

    return json.loads(output)


def az_tsv(*args, check=True):
    return az(
        *args,
        "--query", "id",
        "-o", "tsv",
        check=check
    )


def exists(*args):
    return az_json(*args, check=False) is not None


def wait_for_role_propagation(seconds=30):
    print(f"Waiting {seconds}s for RBAC propagation...")
    time.sleep(seconds)


def load_state():
    path = GENERATED_DIR / "deployment-summary.json"

    if path.exists():
        with open(path, encoding="utf-8") as f:
            STATE.update(json.load(f))


def save_state():
    GENERATED_DIR.mkdir(exist_ok=True)

    with open(
        GENERATED_DIR / "deployment-summary.json",
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(STATE, f, indent=2)


# =============================================================================
# RESOURCE NAMING
# =============================================================================

def initialize_names():
    subscription = az_json("account", "show")

    if not subscription:
        raise RuntimeError(
            "Not authenticated. Run: az login"
        )

    subscription_id = subscription["id"]

    suffix = hashlib.sha256(
        f"{subscription_id}:{RESOURCE_GROUP}".encode()
    ).hexdigest()[:8]

    prefix = f"ai200{suffix}"

    NAMES.update({
        "acr": f"acr{prefix}",
        "storage": f"st{prefix}",
        "servicebus": f"sb-{prefix}",
        "cosmos": f"cosmos-{prefix}",
        "keyvault": f"kv-{prefix}",
        "openai": f"aoai-{prefix}",
        "workspace": f"log-{prefix}",
        "appinsights": f"appi-{prefix}",
        "aca_environment": f"acaenv-{prefix}",
        "containerapp": f"api-{prefix}",
        "function": f"func-{prefix}",
        "plan": f"plan-{prefix}",
        "api_identity": f"id-api-{prefix}",
    })

    STATE["subscription_id"] = subscription_id
    STATE["resource_group"] = RESOURCE_GROUP
    STATE["location"] = LOCATION
    STATE["names"] = NAMES.copy()

    load_state()


# =============================================================================
# PREFLIGHT
# =============================================================================

def preflight():
    print("\nChecking deployment prerequisites...")

    if not shutil.which("az"):
        raise RuntimeError("Azure CLI is not installed.")

    az("account", "show")

    extension = az_json(
        "extension", "show",
        "--name", "containerapp",
        check=False
    )

    if not extension:
        az(
            "extension", "add",
            "--name", "containerapp",
            "--yes",
            description="Installing Container Apps CLI extension"
        )

    providers = [
        "Microsoft.App",
        "Microsoft.ContainerRegistry",
        "Microsoft.Storage",
        "Microsoft.ServiceBus",
        "Microsoft.DocumentDB",
        "Microsoft.KeyVault",
        "Microsoft.ManagedIdentity",
        "Microsoft.Web",
        "Microsoft.Insights",
        "Microsoft.OperationalInsights",
        "Microsoft.CognitiveServices",
    ]

    for provider in providers:
        az(
            "provider", "register",
            "--namespace", provider,
            "--wait",
            description=f"Registering {provider}"
        )

    print("\nPrerequisites completed.")


# =============================================================================
# RESOURCE GROUP
# =============================================================================

def create_resource_group():
    az(
        "group", "create",
        "--name", RESOURCE_GROUP,
        "--location", LOCATION,
        description="Creating resource group"
    )


# =============================================================================
# AZURE CONTAINER REGISTRY
# =============================================================================

def create_acr():
    name = NAMES["acr"]

    if exists("acr", "show", "--name", name):
        print(f"ACR already exists: {name}")
        return

    az(
        "acr", "create",
        "--resource-group", RESOURCE_GROUP,
        "--name", name,
        "--sku", "Basic",
        "--admin-enabled", "false",
        description="Creating Azure Container Registry"
    )


# =============================================================================
# AZURE STORAGE
# =============================================================================

def create_storage():
    name = NAMES["storage"]

    if not exists(
        "storage", "account", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP
    ):
        az(
            "storage", "account", "create",
            "--name", name,
            "--resource-group", RESOURCE_GROUP,
            "--location", LOCATION,
            "--sku", "Standard_LRS",
            "--kind", "StorageV2",
            "--allow-blob-public-access", "false",
            description="Creating Storage Account"
        )

    # Container creation uses an account key for bootstrap.
    # The deployed applications use managed identities.
    key = az(
        "storage", "account", "keys", "list",
        "--account-name", name,
        "--resource-group", RESOURCE_GROUP,
        "--query", "[0].value",
        "-o", "tsv"
    )

    az(
        "storage", "container", "create",
        "--name", CONTAINER_NAME,
        "--account-name", name,
        "--account-key", key,
        description="Creating document container"
    )


# =============================================================================
# AZURE SERVICE BUS
# =============================================================================

def create_service_bus():
    namespace = NAMES["servicebus"]

    if not exists(
        "servicebus", "namespace", "show",
        "--name", namespace,
        "--resource-group", RESOURCE_GROUP
    ):
        az(
            "servicebus", "namespace", "create",
            "--name", namespace,
            "--resource-group", RESOURCE_GROUP,
            "--location", LOCATION,
            "--sku", "Standard",
            description="Creating Service Bus namespace"
        )

    if not exists(
        "servicebus", "queue", "show",
        "--namespace-name", namespace,
        "--resource-group", RESOURCE_GROUP,
        "--name", QUEUE_NAME
    ):
        az(
            "servicebus", "queue", "create",
            "--namespace-name", namespace,
            "--resource-group", RESOURCE_GROUP,
            "--name", QUEUE_NAME,
            "--max-delivery-count", "5",
            description="Creating document processing queue"
        )


# =============================================================================
# AZURE COSMOS DB
# =============================================================================

def create_cosmos():
    account = NAMES["cosmos"]

    if not exists(
        "cosmosdb", "show",
        "--name", account,
        "--resource-group", RESOURCE_GROUP
    ):
        az(
            "cosmosdb", "create",
            "--name", account,
            "--resource-group", RESOURCE_GROUP,
            "--locations", f"regionName={LOCATION}",
            "--capabilities", "EnableServerless",
            description="Creating Cosmos DB serverless account"
        )

    if not exists(
        "cosmosdb", "sql", "database", "show",
        "--account-name", account,
        "--resource-group", RESOURCE_GROUP,
        "--name", COSMOS_DATABASE
    ):
        az(
            "cosmosdb", "sql", "database", "create",
            "--account-name", account,
            "--resource-group", RESOURCE_GROUP,
            "--name", COSMOS_DATABASE,
            description="Creating Cosmos DB database"
        )

    if not exists(
        "cosmosdb", "sql", "container", "show",
        "--account-name", account,
        "--resource-group", RESOURCE_GROUP,
        "--database-name", COSMOS_DATABASE,
        "--name", COSMOS_CONTAINER
    ):
        az(
            "cosmosdb", "sql", "container", "create",
            "--account-name", account,
            "--resource-group", RESOURCE_GROUP,
            "--database-name", COSMOS_DATABASE,
            "--name", COSMOS_CONTAINER,
            "--partition-key-path", "/documentId",
            description="Creating Cosmos DB container"
        )


# =============================================================================
# AZURE KEY VAULT
# =============================================================================

def create_keyvault():
    name = NAMES["keyvault"]

    if exists(
        "keyvault", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP
    ):
        print(f"Key Vault already exists: {name}")
        return

    az(
        "keyvault", "create",
        "--name", name,
        "--resource-group", RESOURCE_GROUP,
        "--location", LOCATION,
        "--enable-rbac-authorization", "true",
        description="Creating Azure Key Vault"
    )


# =============================================================================
# MONITORING
# =============================================================================

def create_monitoring():
    workspace = NAMES["workspace"]
    insights = NAMES["appinsights"]

    if not exists(
        "monitor", "log-analytics", "workspace", "show",
        "--resource-group", RESOURCE_GROUP,
        "--workspace-name", workspace
    ):
        az(
            "monitor", "log-analytics", "workspace", "create",
            "--resource-group", RESOURCE_GROUP,
            "--workspace-name", workspace,
            "--location", LOCATION,
            description="Creating Log Analytics workspace"
        )

    workspace_id = az_tsv(
        "monitor", "log-analytics", "workspace", "show",
        "--resource-group", RESOURCE_GROUP,
        "--workspace-name", workspace
    )

    if not exists(
        "monitor", "app-insights", "component", "show",
        "--app", insights,
        "--resource-group", RESOURCE_GROUP
    ):
        az(
            "monitor", "app-insights", "component", "create",
            "--app", insights,
            "--location", LOCATION,
            "--resource-group", RESOURCE_GROUP,
            "--application-type", "web",
            "--workspace", workspace_id,
            description="Creating Application Insights"
        )


# =============================================================================
# MANAGED IDENTITIES
# =============================================================================

def create_api_identity():
    name = NAMES["api_identity"]

    identity = az_json(
        "identity", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP,
        check=False
    )

    if not identity:
        identity = az_json(
            "identity", "create",
            "--name", name,
            "--resource-group", RESOURCE_GROUP,
            "--location", LOCATION,
            description="Creating API managed identity"
        )

    STATE["api_principal_id"] = identity["principalId"]
    STATE["api_identity_id"] = identity["id"]
    STATE["api_client_id"] = identity["clientId"]

    save_state()


# =============================================================================
# ROLE ASSIGNMENTS
# =============================================================================

def assign_role(principal_id, role, scope):
    existing = az_json(
        "role", "assignment", "list",
        "--assignee", principal_id,
        "--scope", scope,
        "--role", role,
        check=False
    )

    if existing:
        print(f"Role already assigned: {role}")
        return

    az(
        "role", "assignment", "create",
        "--assignee-object-id", principal_id,
        "--assignee-principal-type", "ServicePrincipal",
        "--role", role,
        "--scope", scope,
        description=f"Assigning {role}"
    )


def resource_id(*args):
    return az_tsv(*args)


def assign_cosmos_data_role(principal_id):
    account = NAMES["cosmos"]

    account_id = resource_id(
        "cosmosdb", "show",
        "--name", account,
        "--resource-group", RESOURCE_GROUP
    )

    # Built-in Cosmos DB SQL Data Contributor role.
    role_id = (
        f"{account_id}/sqlRoleDefinitions/"
        "00000000-0000-0000-0000-000000000002"
    )

    existing = az_json(
        "cosmosdb", "sql", "role", "assignment", "list",
        "--account-name", account,
        "--resource-group", RESOURCE_GROUP
    ) or []

    for assignment in existing:
        if (
            assignment.get("principalId", "").lower()
            == principal_id.lower()
            and assignment.get("roleDefinitionId", "").lower()
            == role_id.lower()
        ):
            return

    az(
        "cosmosdb", "sql", "role", "assignment", "create",
        "--account-name", account,
        "--resource-group", RESOURCE_GROUP,
        "--principal-id", principal_id,
        "--role-definition-id", role_id,
        "--scope", "/",
        description="Assigning Cosmos DB data-plane role"
    )


def configure_identity_roles(principal_id, is_worker=False):
    storage_id = resource_id(
        "storage", "account", "show",
        "--name", NAMES["storage"],
        "--resource-group", RESOURCE_GROUP
    )

    bus_id = resource_id(
        "servicebus", "namespace", "show",
        "--name", NAMES["servicebus"],
        "--resource-group", RESOURCE_GROUP
    )

    queue_scope = (
        f"{bus_id}/queues/{QUEUE_NAME}"
    )

    vault_id = resource_id(
        "keyvault", "show",
        "--name", NAMES["keyvault"],
        "--resource-group", RESOURCE_GROUP
    )

    blob_role = (
        "Storage Blob Data Reader"
        if is_worker
        else "Storage Blob Data Contributor"
    )

    bus_role = (
        "Azure Service Bus Data Receiver"
        if is_worker
        else "Azure Service Bus Data Sender"
    )

    assign_role(principal_id, blob_role, storage_id)
    assign_role(principal_id, bus_role, queue_scope)

    assign_role(
        principal_id,
        "Key Vault Secrets User",
        vault_id
    )

    assign_cosmos_data_role(principal_id)


def configure_acr_role():
    acr_id = resource_id(
        "acr", "show",
        "--name", NAMES["acr"],
        "--resource-group", RESOURCE_GROUP
    )

    assign_role(
        STATE["api_principal_id"],
        "AcrPull",
        acr_id
    )


# =============================================================================
# AZURE OPENAI
# =============================================================================

def create_ai_services():
    if not CREATE_AI_MODEL:
        print(
            "AI model creation disabled. "
            "Configure an existing endpoint separately."
        )
        return

    name = NAMES["openai"]

    if not exists(
        "cognitiveservices", "account", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP
    ):
        az(
            "cognitiveservices", "account", "create",
            "--name", name,
            "--resource-group", RESOURCE_GROUP,
            "--location", LOCATION,
            "--kind", "OpenAI",
            "--sku", "S0",
            "--custom-domain", name,
            "--yes",
            description="Creating Azure OpenAI resource"
        )

    deployment = az_json(
        "cognitiveservices", "account", "deployment", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP,
        "--deployment-name", MODEL_DEPLOYMENT,
        check=False
    )

    if not deployment:
        az(
            "cognitiveservices", "account", "deployment", "create",
            "--name", name,
            "--resource-group", RESOURCE_GROUP,
            "--deployment-name", MODEL_DEPLOYMENT,
            "--model-name", MODEL_NAME,
            "--model-version", MODEL_VERSION,
            "--model-format", "OpenAI",
            "--sku-name", "Standard",
            "--sku-capacity", "1",
            description="Deploying AI model"
        )

    STATE["ai_endpoint"] = (
        f"https://{name}.openai.azure.com/"
    )

    save_state()


def configure_ai_role(principal_id):
    if not CREATE_AI_MODEL:
        return

    ai_id = resource_id(
        "cognitiveservices", "account", "show",
        "--name", NAMES["openai"],
        "--resource-group", RESOURCE_GROUP
    )

    assign_role(
        principal_id,
        "Cognitive Services OpenAI User",
        ai_id
    )


# =============================================================================
# CONTAINER APPS ENVIRONMENT
# =============================================================================

def create_container_environment():
    name = NAMES["aca_environment"]

    if exists(
        "containerapp", "env", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP
    ):
        return

    workspace = az_json(
        "monitor", "log-analytics", "workspace", "show",
        "--resource-group", RESOURCE_GROUP,
        "--workspace-name", NAMES["workspace"]
    )

    workspace_customer_id = workspace["customerId"]

    shared_key = az(
        "monitor", "log-analytics", "workspace", "get-shared-keys",
        "--resource-group", RESOURCE_GROUP,
        "--workspace-name", NAMES["workspace"],
        "--query", "primarySharedKey",
        "-o", "tsv"
    )

    az(
        "containerapp", "env", "create",
        "--name", name,
        "--resource-group", RESOURCE_GROUP,
        "--location", LOCATION,
        "--logs-workspace-id", workspace_customer_id,
        "--logs-workspace-key", shared_key,
        description="Creating Container Apps environment"
    )


# =============================================================================
# BUILD CONTAINER IMAGE
# =============================================================================

def build_api_image():
    print("\n[+] Checking API container image")

    registry = NAMES["acr"]
    image = "document-api:v1"

    # Check whether the image already exists
    result = subprocess.run(
        [
            shutil.which("az") or shutil.which("az.cmd") or
            r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.CMD",
            "acr", "repository", "show",
            "--name", registry,
            "--image", image,
            "--output", "json",
            "--only-show-errors"
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    if result.returncode == 0:
        print(f"[OK] Image {image} already exists. Skipping build.")
        return

    print("[+] Building and pushing API container")

    az(
        "acr", "build",
        "--registry", registry,
        "--resource-group", RESOURCE_GROUP,
        "--image", image,
        "--file", str(API_DIR / "Dockerfile"),
        "--no-logs",
        str(API_DIR),
        description="Building and pushing API container"
    )


# =============================================================================
# DEPLOY CONTAINER APP
# =============================================================================

def deploy_container_app():
    name = NAMES["containerapp"]

    registry = f"{NAMES['acr']}.azurecr.io"
    image = f"{registry}/{IMAGE_NAME}"

    identity_id = STATE["api_identity_id"]

    connection_string = az(
        "monitor", "app-insights", "component", "show",
        "--app", NAMES["appinsights"],
        "--resource-group", RESOURCE_GROUP,
        "--query", "connectionString",
        "-o", "tsv"
    )

    env_vars = [
        f"STORAGE_ACCOUNT={NAMES['storage']}",
        f"BLOB_CONTAINER={CONTAINER_NAME}",
        f"SERVICEBUS_NAMESPACE={NAMES['servicebus']}.servicebus.windows.net",
        f"SERVICEBUS_QUEUE={QUEUE_NAME}",
        f"COSMOS_ENDPOINT=https://{NAMES['cosmos']}.documents.azure.com:443/",
        f"COSMOS_DATABASE={COSMOS_DATABASE}",
        f"COSMOS_CONTAINER={COSMOS_CONTAINER}",
        f"AZURE_CLIENT_ID={STATE['api_client_id']}",
        f"APPLICATIONINSIGHTS_CONNECTION_STRING={connection_string}",
    ]

    if exists(
        "containerapp", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP
    ):
        az(
            "containerapp", "update",
            "--name", name,
            "--resource-group", RESOURCE_GROUP,
            "--image", image,
            "--set-env-vars", *env_vars,
            description="Updating Container App"
        )
    else:
        az(
            "containerapp", "create",
            "--name", name,
            "--resource-group", RESOURCE_GROUP,
            "--environment", NAMES["aca_environment"],
            "--image", image,
            "--ingress", "external",
            "--target-port", TARGET_PORT,
            "--user-assigned", identity_id,
            "--registry-server", registry,
            "--registry-identity", identity_id,
            "--env-vars", *env_vars,
            description="Deploying API to Container Apps"
        )

    fqdn = az(
        "containerapp", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP,
        "--query", "properties.configuration.ingress.fqdn",
        "-o", "tsv"
    )

    STATE["api_url"] = f"https://{fqdn}"
    save_state()

    print(f"\nAPI URL: {STATE['api_url']}")


# =============================================================================
# AZURE FUNCTIONS
# =============================================================================

def create_function_app():
    name = NAMES["function"]
    plan = NAMES["plan"]

    # if not exists(
    #     "functionapp", "plan", "show",
    #     "--name", plan,
    #     "--resource-group", RESOURCE_GROUP
    # ):
    #     az(
    #         "functionapp", "plan", "create",
    #         "--name", plan,
    #         "--resource-group", RESOURCE_GROUP,
    #         "--location", LOCATION,
    #         "--sku", "Y1",
    #         "--is-linux",
    #         description="Creating Linux Consumption plan"
    #     )

    if not exists(
        "functionapp", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP
    ):
        az(
            "functionapp", "create",
            "--name", name,
            "--resource-group", RESOURCE_GROUP,
            "--consumption-plan-location", LOCATION,
            "--storage-account", NAMES["storage"],
            "--runtime", "python",
            "--runtime-version", "3.11",
            "--functions-version", "4",
            "--os-type", "Linux",
            description="Creating Azure Function App"
        )

    az(
        "functionapp", "identity", "assign",
        "--name", name,
        "--resource-group", RESOURCE_GROUP,
        description="Enabling Function managed identity"
    )

    identity = az_json(
        "functionapp", "identity", "show",
        "--name", name,
        "--resource-group", RESOURCE_GROUP
    )

    STATE["worker_principal_id"] = identity["principalId"]
    save_state()


def configure_function_app():
    name = NAMES["function"]

    connection_string = az(
        "monitor", "app-insights", "component", "show",
        "--app", NAMES["appinsights"],
        "--resource-group", RESOURCE_GROUP,
        "--query", "connectionString",
        "-o", "tsv"
    )

    settings = [
        f"STORAGE_ACCOUNT={NAMES['storage']}",
        f"BLOB_CONTAINER={CONTAINER_NAME}",
        f"COSMOS_ENDPOINT=https://{NAMES['cosmos']}.documents.azure.com:443/",
        f"COSMOS_DATABASE={COSMOS_DATABASE}",
        f"COSMOS_CONTAINER={COSMOS_CONTAINER}",
        f"ServiceBusConnection__fullyQualifiedNamespace={NAMES['servicebus']}.servicebus.windows.net",
        f"APPLICATIONINSIGHTS_CONNECTION_STRING={connection_string}",
        f"MODEL_DEPLOYMENT={MODEL_DEPLOYMENT}",
        "SCM_DO_BUILD_DURING_DEPLOYMENT=true",
        "ENABLE_ORYX_BUILD=true",
    ]

    if CREATE_AI_MODEL:
        settings.append(
            f"AZURE_OPENAI_ENDPOINT={STATE['ai_endpoint']}"
        )

    az(
        "functionapp", "config", "appsettings", "set",
        "--name", name,
        "--resource-group", RESOURCE_GROUP,
        "--settings", *settings,
        description="Configuring Function App settings"
    )


def create_function_package():
    if not (WORKER_DIR / "function_app.py").exists():
        raise RuntimeError(
            "Missing worker/function_app.py"
        )

    if not (WORKER_DIR / "requirements.txt").exists():
        raise RuntimeError(
            "Missing worker/requirements.txt"
        )

    package = GENERATED_DIR / "worker.zip"
    GENERATED_DIR.mkdir(exist_ok=True)

    with zipfile.ZipFile(
        package,
        "w",
        zipfile.ZIP_DEFLATED
    ) as archive:
        for file in WORKER_DIR.rglob("*"):
            if not file.is_file():
                continue

            if any(
                part in {
                    ".venv", "__pycache__", ".git", ".pytest_cache"
                }
                for part in file.relative_to(WORKER_DIR).parts
            ):
                continue

            archive.write(
                file,
                file.relative_to(WORKER_DIR)
            )

    return package


def deploy_function_code():
    package = create_function_package()

    az(
        "functionapp", "deployment", "source", "config-zip",
        "--name", NAMES["function"],
        "--resource-group", RESOURCE_GROUP,
        "--src", str(package),
        "--build-remote", "true",
        description="Deploying Azure Function code"
    )


# =============================================================================
# ENVIRONMENT FILES
# =============================================================================

def generate_environment_files():
    GENERATED_DIR.mkdir(exist_ok=True)

    variables = {
        "RESOURCE_GROUP": RESOURCE_GROUP,
        "LOCATION": LOCATION,
        "ACR_NAME": NAMES["acr"],
        "STORAGE_ACCOUNT": NAMES["storage"],
        "SERVICEBUS_NAMESPACE": NAMES["servicebus"],
        "SERVICEBUS_QUEUE": QUEUE_NAME,
        "COSMOS_ACCOUNT": NAMES["cosmos"],
        "COSMOS_DATABASE": COSMOS_DATABASE,
        "COSMOS_CONTAINER": COSMOS_CONTAINER,
        "KEYVAULT_NAME": NAMES["keyvault"],
        "CONTAINER_APP_NAME": NAMES["containerapp"],
        "FUNCTION_APP_NAME": NAMES["function"],
        "APPLICATION_INSIGHTS": NAMES["appinsights"],
        "API_URL": STATE.get("api_url", ""),
    }

    def bash_quote(value):
        return "'" + value.replace("'", "'\\''") + "'"

    def ps_quote(value):
        return "'" + value.replace("'", "''") + "'"

    with open(
        GENERATED_DIR / ".env",
        "w",
        encoding="utf-8"
    ) as f:
        for key, value in variables.items():
            f.write(f"export {key}={bash_quote(value)}\n")

    with open(
        GENERATED_DIR / ".env.ps1",
        "w",
        encoding="utf-8"
    ) as f:
        for key, value in variables.items():
            f.write(f"$env:{key} = {ps_quote(value)}\n")

    save_state()

    print("\nEnvironment files generated.")


# =============================================================================
# DEPLOYMENT WORKFLOWS
# =============================================================================

def deploy_infrastructure():
    create_resource_group()
    create_acr()
    create_storage()
    create_service_bus()
    create_cosmos()
    create_keyvault()
    create_monitoring()
    create_container_environment()
    create_api_identity()
    create_function_app()
    save_state()


def deploy_security():
    if "api_principal_id" not in STATE:
        create_api_identity()

    if "worker_principal_id" not in STATE:
        create_function_app()

    configure_identity_roles(
        STATE["api_principal_id"],
        is_worker=False
    )

    configure_identity_roles(
        STATE["worker_principal_id"],
        is_worker=True
    )

    configure_acr_role()

    if CREATE_AI_MODEL:
        configure_ai_role(STATE["worker_principal_id"])

    wait_for_role_propagation()


def deploy_applications():
    build_api_image()
    deploy_container_app()
    configure_function_app()
    deploy_function_code()
    generate_environment_files()


def deploy_all():
    print("\nStarting complete AI-200 deployment...")

    preflight()

    deploy_infrastructure()

    create_ai_services()

    deploy_security()

    deploy_applications()

    print("\n========================================")
    print("DEPLOYMENT COMPLETED")
    print("========================================")

    show_status()


# =============================================================================
# VALIDATION
# =============================================================================

def show_status():
    print("\n========================================")
    print("AI-200 CAPSTONE - DEPLOYMENT STATUS")
    print("========================================")

    print(f"Resource Group: {RESOURCE_GROUP}")
    print(f"Region: {LOCATION}")

    print("\nResources:")

    resources = az_json(
        "resource", "list",
        "--resource-group", RESOURCE_GROUP
    ) or []

    for resource in resources:
        print(
            f"  {resource['name']}"
            f"  [{resource['type']}]"
        )

    if STATE.get("api_url"):
        print(f"\nAPI: {STATE['api_url']}")

    print("\nFunction App:", NAMES["function"])
    print("Service Bus Queue:", QUEUE_NAME)

    print("\n========================================")


def validate_deployment():
    print("\nValidating Azure resources...")

    required = [
        ("acr", "show", "--name", NAMES["acr"]),
        (
            "storage", "account", "show",
            "--name", NAMES["storage"],
            "--resource-group", RESOURCE_GROUP
        ),
        (
            "servicebus", "namespace", "show",
            "--name", NAMES["servicebus"],
            "--resource-group", RESOURCE_GROUP
        ),
        (
            "cosmosdb", "show",
            "--name", NAMES["cosmos"],
            "--resource-group", RESOURCE_GROUP
        ),
        (
            "containerapp", "show",
            "--name", NAMES["containerapp"],
            "--resource-group", RESOURCE_GROUP
        ),
        (
            "functionapp", "show",
            "--name", NAMES["function"],
            "--resource-group", RESOURCE_GROUP
        ),
    ]

    failed = []

    for command in required:
        result = az_json(*command, check=False)

        if result:
            print(f"[OK] {command[0]}")
        else:
            print(f"[FAIL] {command[0]}")
            failed.append(command[0])

    if failed:
        raise RuntimeError(
            "Missing resources: " + ", ".join(failed)
        )

    if STATE.get("api_url"):
        import urllib.request

        url = STATE["api_url"] + "/health"

        try:
            with urllib.request.urlopen(
                url,
                timeout=30
            ) as response:
                print(
                    f"[OK] API health: HTTP {response.status}"
                )
        except Exception as exc:
            print(f"[FAIL] API health: {exc}")

    print("\nResource validation finished.")
    print(
        "Note: Full end-to-end processing and RBAC "
        "validation require the application test suite."
    )


# =============================================================================
# CLEANUP
# =============================================================================

def cleanup():
    print("\nWARNING: This deletes the entire resource group.")
    print(f"Resource Group: {RESOURCE_GROUP}")

    confirmation = input(
        f"Type '{RESOURCE_GROUP}' to confirm: "
    )

    if confirmation != RESOURCE_GROUP:
        print("Cleanup cancelled.")
        return

    az(
        "group", "delete",
        "--name", RESOURCE_GROUP,
        "--yes",
        "--no-wait",
        description="Deleting lab resources"
    )

    print("\nResource group deletion initiated.")


# =============================================================================
# INTERACTIVE MENU
# =============================================================================

def show_menu():
    print("""
=======================================================
       AI-200 CAPSTONE - DEPLOYMENT SCRIPT
=======================================================

 1. Check prerequisites
 2. Deploy Azure infrastructure
 3. Deploy Azure AI model
 4. Configure managed identities and RBAC
 5. Build and deploy applications
 6. Validate deployment
 7. Show deployment status
 8. Generate environment files
 9. DEPLOY EVERYTHING
10. Clean up Azure resources
 0. Exit

=======================================================
""")


def main():
    initialize_names()

    actions = {
        "1": preflight,
        "2": deploy_infrastructure,
        "3": create_ai_services,
        "4": deploy_security,
        "5": deploy_applications,
        "6": validate_deployment,
        "7": show_status,
        "8": generate_environment_files,
        "9": deploy_all,
        "10": cleanup,
    }

    while True:
        show_menu()

        choice = input("Select an option: ").strip()

        if choice == "0":
            print("Goodbye.")
            break

        action = actions.get(choice)

        if not action:
            print("Invalid option.")
            continue

        try:
            action()
        except Exception as exc:
            print("\n[ERROR]")
            print(exc)

        input("\nPress Enter to continue...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDeployment interrupted.")
        sys.exit(130)
