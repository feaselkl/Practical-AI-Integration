# Shared helpers for the infra scripts. Sourced, not executed.
#
# Kept to the things that must behave identically everywhere. The scripts stay
# otherwise self-contained so you can read any one of them top to bottom.

# ensure_providers <namespace>...
#
# Azure resource providers are registered per SUBSCRIPTION, and a fresh
# subscription has most of them off. Creating a resource under an unregistered
# namespace fails with:
#
#   (MissingSubscriptionRegistration) The subscription is not registered to use
#   namespace 'Microsoft.CognitiveServices'
#
# which says nothing about how to fix it. Register up front instead.
#
# Checks before registering: registration is idempotent but `--wait` on an
# already-registered namespace still costs a round trip, and there are several.
ensure_providers() {
    local ns state
    for ns in "$@"; do
        state=$(az provider show --namespace "$ns" --query registrationState --output tsv 2>/dev/null || echo "Unknown")

        case "$state" in
            Registered)
                printf '    %-34s registered\n' "$ns"
                continue
                ;;
            Registering)
                printf '    %-34s registering, waiting...\n' "$ns"
                ;;
            *)
                printf '    %-34s %s, registering (can take a few minutes)\n' "$ns" "$state"
                ;;
        esac

        if ! az provider register --namespace "$ns" --wait --only-show-errors; then
            cat >&2 <<ERREOF

ERROR: Could not register the '$ns' provider on this subscription.

Almost always a permissions problem, and a confusing one: creating resources
needs Contributor on the RESOURCE GROUP, but registering a provider needs it on
the SUBSCRIPTION. You can have enough rights to run the rest of this script and
still not enough to get past this line.

Either ask someone with subscription-level access to run:

    az provider register --namespace $ns --subscription "$(az account show --query id -o tsv 2>/dev/null)"

or register it in the Azure portal under:

    Subscriptions -> your subscription -> Settings -> Resource providers

Then re-run this script. It is safe to re-run.
ERREOF
            exit 1
        fi
        printf '    %-34s registered\n' "$ns"
    done
}
