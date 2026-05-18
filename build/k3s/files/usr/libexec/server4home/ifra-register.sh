#!/usr/bin/env bash
#
# ifra-register.sh — best-effort first-boot registration with the IFRA
# (homelab inventory) API.
#
# Sends this host's primary-NIC MAC and hostname to the configured endpoint.
# On error the script logs and exits 0 — registration is advisory, never a
# blocker for boot or for K3s. On success, a sentinel file is written so we
# do not re-register on every boot.
#
# Override the endpoint with /etc/server4home/ifra.conf, e.g.:
#   IFRA_URL=https://ifra.local.example.com/api/ifra/mac-addresses/reserve-mac-address
#   IFRA_INSECURE=1   # skip TLS verify (self-signed homelab cert)

set -euo pipefail

log()  { printf '[ifra-register] %s\n' "$*"; }

SENTINEL="/var/lib/server4home/.ifra-registered"
IFRA_URL="https://ifra.local.homelabsolutions.nen/api/ifra/mac-addresses/reserve-mac-address"
IFRA_INSECURE=0

# Operator overrides, if any.
[[ -r /etc/server4home/ifra.conf ]] && source /etc/server4home/ifra.conf

if [[ -f "$SENTINEL" ]]; then
    log "Already registered (sentinel $SENTINEL present); nothing to do."
    exit 0
fi

# Find the primary NIC — the one carrying the default route.
iface="$(ip -4 route show default 2>/dev/null | awk '/^default/{print $5; exit}')"
if [[ -z "$iface" ]]; then
    log "No default route yet; cannot determine primary NIC. Skipping."
    exit 0
fi

mac_path="/sys/class/net/${iface}/address"
if [[ ! -r "$mac_path" ]]; then
    log "Cannot read MAC for $iface; skipping."
    exit 0
fi

mac="$(cat "$mac_path")"
hostname="$(hostnamectl --static 2>/dev/null || hostname)"

log "POST $IFRA_URL  mac=$mac hostname=$hostname iface=$iface"

curl_args=(
    --silent --show-error
    --fail
    --max-time 10
    --connect-timeout 5
    --retry 2 --retry-delay 2
    --header 'Content-Type: application/json'
    --request POST
    --data "{\"mac\":\"$mac\",\"hostname\":\"$hostname\",\"interface\":\"$iface\"}"
    "$IFRA_URL"
)
[[ "$IFRA_INSECURE" == "1" ]] && curl_args=(--insecure "${curl_args[@]}")

if response=$(curl "${curl_args[@]}" 2>&1); then
    log "Registration succeeded. Response: $response"
    install -d -m 0755 "$(dirname "$SENTINEL")"
    {
        echo "registered_at=$(date -Is)"
        echo "mac=$mac"
        echo "hostname=$hostname"
        echo "url=$IFRA_URL"
    } > "$SENTINEL"
else
    rc=$?
    log "Registration failed (curl exit $rc). Keeping defaults; will retry on next boot."
fi

exit 0
