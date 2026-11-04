#!/bin/bash
# Build the Azure side of the demo environment for "Practical AI Integration".
#
# Creates a resource group, an Azure OpenAI resource, a chat model deployment,
# and an embedding model deployment. Assigns you the data-plane role, makes one
# real inference call to prove it works, then writes code/.env.
#
# Safe to re-run: every step checks before it creates.
#
# Usage:
#   ./infra/setup.sh
#   AOAI_LOCATION=swedencentral ./infra/setup.sh     # override any setting below
#
# Tear it down afterward with ./infra/teardown.sh.
#
# None of this is required to run the demos locally: set LLM_PROVIDER=ollama in
# code/.env and everything runs against Ollama instead.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration. Override any of these as environment variables.
# ---------------------------------------------------------------------------

LOCATION="${AOAI_LOCATION:-eastus2}"
RESOURCE_GROUP="${AOAI_RESOURCE_GROUP:-rg-practical-ai}"

CHAT_MODEL="${AOAI_CHAT_MODEL:-gpt-5.4-mini}"
CHAT_DEPLOYMENT="${AOAI_CHAT_DEPLOYMENT:-gpt-5.4-mini}"
CHAT_CAPACITY="${AOAI_CHAT_CAPACITY:-50}"

EMBEDDING_MODEL="${AOAI_EMBEDDING_MODEL:-text-embedding-3-small}"
EMBEDDING_DEPLOYMENT="${AOAI_EMBEDDING_DEPLOYMENT:-text-embedding-3-small}"
EMBEDDING_CAPACITY="${AOAI_EMBEDDING_CAPACITY:-50}"

SKU_NAME="${AOAI_SKU_NAME:-GlobalStandard}"

# The resource name doubles as the custom subdomain, which must be globally
# unique. A suffix avoids collisions with everyone else who ran this.
RESOURCE_NAME="${AOAI_RESOURCE_NAME:-aoai-practical-$(whoami | tr -cd 'a-z0-9' | cut -c1-8)}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/code/.env"

# shellcheck source=_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
skip() { printf '    (exists, skipping) %s\n' "$1"; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

say "Checking prerequisites"

if ! command -v az > /dev/null 2>&1; then
    echo "ERROR: Azure CLI not found. See https://learn.microsoft.com/cli/azure/install-azure-cli"
    exit 1
fi

if ! az account show > /dev/null 2>&1; then
    echo "ERROR: Not signed in. Run: az login"
    exit 1
fi

SUBSCRIPTION=$(az account show --query name --output tsv)
echo "    Subscription: $SUBSCRIPTION"
echo "    Location:     $LOCATION"
echo "    Resource:     $RESOURCE_NAME"

say "Resource providers"
ensure_providers Microsoft.CognitiveServices

# ---------------------------------------------------------------------------
# Resource group
# ---------------------------------------------------------------------------

say "Resource group: $RESOURCE_GROUP"
if [ "$(az group exists --name "$RESOURCE_GROUP")" = "true" ]; then
    skip "$RESOURCE_GROUP"
else
    az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none
    echo "    Created."
fi

# ---------------------------------------------------------------------------
# Azure OpenAI resource
# ---------------------------------------------------------------------------

say "Azure OpenAI resource: $RESOURCE_NAME"
if az cognitiveservices account show \
        --name "$RESOURCE_NAME" --resource-group "$RESOURCE_GROUP" > /dev/null 2>&1; then
    skip "$RESOURCE_NAME"
else
    # --custom-domain is what gives you https://<name>.openai.azure.com, and
    # Entra ID auth requires it.
    az cognitiveservices account create \
        --name "$RESOURCE_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --kind OpenAI \
        --sku S0 \
        --location "$LOCATION" \
        --custom-domain "$RESOURCE_NAME" \
        --yes \
        --output none
    echo "    Created."
fi

STATE=$(az cognitiveservices account show \
    --name "$RESOURCE_NAME" --resource-group "$RESOURCE_GROUP" \
    --query properties.provisioningState --output tsv)
if [ "$STATE" != "Succeeded" ]; then
    echo "ERROR: Resource is in state '$STATE', expected 'Succeeded'."
    exit 1
fi

# ---------------------------------------------------------------------------
# Model deployments
# ---------------------------------------------------------------------------

# deploy_model <deployment-name> <model-name> <capacity>
deploy_model() {
    local deployment="$1" model="$2" capacity="$3" version

    say "Model deployment: $deployment ($model)"
    if az cognitiveservices account deployment show \
            --name "$RESOURCE_NAME" --resource-group "$RESOURCE_GROUP" \
            --deployment-name "$deployment" > /dev/null 2>&1; then
        skip "$deployment"
        return
    fi

    # Model versions move. Ask the region what it has rather than hard-coding
    # a version that will rot between deliveries.
    version=$(az cognitiveservices model list \
        --location "$LOCATION" \
        --query "[?kind=='OpenAI' && model.name=='$model'] | [-1].model.version" \
        --output tsv 2>/dev/null || true)

    if [ -z "$version" ] || [ "$version" = "None" ]; then
        echo "ERROR: Model '$model' is not available in $LOCATION."
        echo "       See what is, with:"
        echo "         az cognitiveservices model list --location $LOCATION --output table"
        exit 1
    fi

    echo "    Version in $LOCATION: $version"
    az cognitiveservices account deployment create \
        --name "$RESOURCE_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --deployment-name "$deployment" \
        --model-name "$model" \
        --model-version "$version" \
        --model-format OpenAI \
        --sku-capacity "$capacity" \
        --sku-name "$SKU_NAME" \
        --output none
    echo "    Created."
}

deploy_model "$CHAT_DEPLOYMENT" "$CHAT_MODEL" "$CHAT_CAPACITY"
deploy_model "$EMBEDDING_DEPLOYMENT" "$EMBEDDING_MODEL" "$EMBEDDING_CAPACITY"

# ---------------------------------------------------------------------------
# Data-plane access
# ---------------------------------------------------------------------------
# Owner and Contributor are CONTROL-plane roles. They let you create this
# resource and do not let you make one inference call against it. With Entra
# ID auth you need a role that grants the data actions. Referenced by GUID
# because display names drift.

say "Data-plane access"

OPENAI_USER="5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"   # Cognitive Services OpenAI User
ROLE_JUST_ASSIGNED=0
ACCOUNT_ID=$(az cognitiveservices account show \
    --name "$RESOURCE_NAME" --resource-group "$RESOURCE_GROUP" \
    --query id --output tsv)

PRINCIPAL_ID=$(az ad signed-in-user show --query id --output tsv 2>/dev/null || true)
PRINCIPAL_TYPE="User"

if [ -z "$PRINCIPAL_ID" ]; then
    # Signed in as a service principal rather than a user.
    SP_APP_ID=$(az account show --query user.name --output tsv 2>/dev/null || true)
    PRINCIPAL_ID=$(az ad sp show --id "$SP_APP_ID" --query id --output tsv 2>/dev/null || true)
    PRINCIPAL_TYPE="ServicePrincipal"
fi

if [ -z "$PRINCIPAL_ID" ]; then
    echo "    WARNING: could not resolve the signed-in principal, so no role was"
    echo "    assigned. If the demos return 403, assign 'Cognitive Services OpenAI User'"
    echo "    ($OPENAI_USER) to yourself on:"
    echo "      $ACCOUNT_ID"
else
    EXISTING=$(az role assignment list \
        --assignee "$PRINCIPAL_ID" --scope "$ACCOUNT_ID" \
        --query "[?contains(roleDefinitionId, '$OPENAI_USER')].id" \
        --output tsv 2>/dev/null || true)

    if [ -n "$EXISTING" ]; then
        skip "Cognitive Services OpenAI User already assigned"
    else
        ROLE_JUST_ASSIGNED=1
        if az role assignment create \
                --role "$OPENAI_USER" \
                --assignee-object-id "$PRINCIPAL_ID" \
                --assignee-principal-type "$PRINCIPAL_TYPE" \
                --scope "$ACCOUNT_ID" \
                --output none 2>/dev/null; then
            echo "    Assigned Cognitive Services OpenAI User at account scope."
        else
            cat <<'ROLEERR'
    WARNING: could not create the role assignment.

    Creating one needs Microsoft.Authorization/roleAssignments/write -- Owner or
    User Access Administrator. Contributor is not enough.

    Ask someone with that right to assign 'Cognitive Services OpenAI User' to
    you on the Azure OpenAI resource. Until then, either set
    AZURE_OPENAI_API_KEY in code/.env or every Azure call returns 403.
ROLEERR
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Write code/.env
# ---------------------------------------------------------------------------

say "Writing $ENV_FILE"

ENDPOINT="https://$RESOURCE_NAME.openai.azure.com/openai/v1/"

if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "$ENV_FILE.bak"
    echo "    Existing .env backed up to .env.bak"
fi

# Start from .env.example so every setting is present and documented, then
# fill in what this script knows.
sed \
    -e "s|^LLM_PROVIDER=.*|LLM_PROVIDER=azure|" \
    -e "s|^AZURE_OPENAI_ENDPOINT=.*|AZURE_OPENAI_ENDPOINT=$ENDPOINT|" \
    -e "s|^AZURE_OPENAI_DEPLOYMENT=.*|AZURE_OPENAI_DEPLOYMENT=$CHAT_DEPLOYMENT|" \
    -e "s|^AZURE_OPENAI_EMBEDDING_DEPLOYMENT=.*|AZURE_OPENAI_EMBEDDING_DEPLOYMENT=$EMBEDDING_DEPLOYMENT|" \
    "$REPO_ROOT/code/.env.example" > "$ENV_FILE.tmp"
{
    echo "# Generated by infra/setup.sh on $(date -u '+%Y-%m-%d %H:%M UTC')"
    echo "# Resource group: $RESOURCE_GROUP"
    echo ""
    cat "$ENV_FILE.tmp"
} > "$ENV_FILE"
rm -f "$ENV_FILE.tmp"

echo "    Endpoint: $ENDPOINT"

# ---------------------------------------------------------------------------
# Verify the data plane actually works
# ---------------------------------------------------------------------------
# Provisioning succeeding says nothing about whether you can CALL it. Make one
# real inference call now, so a 403 surfaces here with an explanation instead
# of in the middle of a demo.

say "Verifying data-plane access"

get_token() {
    az account get-access-token --scope https://cognitiveservices.azure.com/.default \
        --query accessToken --output tsv 2>/dev/null || true
}

TOKEN=$(get_token)
if [ -z "$TOKEN" ]; then
    echo "    WARNING: could not get a token. Skipping verification."
    exit 0
fi

# RBAC is eventually consistent. A fresh assignment has been measured at around
# five minutes before the first successful call, so be patient after assigning
# and brisk otherwise.
if [ "$ROLE_JUST_ASSIGNED" -eq 1 ]; then
    MAX_ATTEMPTS=20          # 20 x 30s = 10 minutes
    echo "    Role was just assigned; this typically takes 5-6 minutes. Waiting up to 10."
else
    MAX_ATTEMPTS=4           # 4 x 30s = 2 minutes
fi

BODY_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE"' EXIT
VERIFY_START=$(date +%s)

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    HTTP_CODE=$(curl -s -o "$BODY_FILE" -w '%{http_code}' --max-time 60 \
        -X POST "${ENDPOINT}responses" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $TOKEN" \
        -d "{\"model\": \"$CHAT_DEPLOYMENT\", \"input\": \"Reply with the single word: ready\"}" \
        2>/dev/null || echo "000")

    case "$HTTP_CODE" in
        2*)
            printf '    Inference call succeeded (HTTP %s after %ss).\n' \
                "$HTTP_CODE" "$(( $(date +%s) - VERIFY_START ))"
            echo ""
            echo "Ready. Next:"
            echo "    cd code && uv sync && uv run python demos/01_first_integration/first_call.py"
            exit 0
            ;;
        401|403)
            if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
                printf '    HTTP %s after %ss, waiting for RBAC to propagate (%s/%s)\n' \
                    "$HTTP_CODE" "$(( $(date +%s) - VERIFY_START ))" "$attempt" "$MAX_ATTEMPTS"
                sleep 30
                TOKEN=$(get_token)
            fi
            ;;
        *)
            echo "    HTTP $HTTP_CODE"
            head -c 400 "$BODY_FILE" && echo
            exit 1
            ;;
    esac
done

echo "    Still HTTP $HTTP_CODE after $MAX_ATTEMPTS attempts. Check the role assignment on:"
echo "      $ACCOUNT_ID"
exit 1
