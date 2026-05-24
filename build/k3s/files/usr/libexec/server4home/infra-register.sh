#!/usr/bin/env bash
#
# infra-register.sh — best-effort first-boot registration with the homelab
# INFRA (Infrastructure) API.
#
# INFRA is the planned homelab infrastructure service: resource inventory,
# MAC address reservation, and a programmatic bridge to the pfSense firewall
# (https://firewall.home.andreevs.net/api/v2/documentation). Until that
# service is deployed, this script is a no-op that fails gracefully — boot
# and K3s never depend on it.
#
# Sends this host's primary-NIC MAC and hostname to the configured endpoint.
# On error the script logs and exits 0 — registration is advisory, never a
# blocker for boot or for K3s. On success, a sentinel file is written so we
# do not re-register on every boot.
#
# Override the endpoint with /etc/server4home/infra.conf, e.g.:
#   INFRA_URL=https://infra.local.example.com/api/infra/mac-addresses/reserve-mac-address
#   INFRA_INSECURE=1   # skip TLS verify (self-signed homelab cert)

set -euo pipefail

log()  { printf '[infra-register] %s\n' "$*"; }

SENTINEL="/var/lib/server4home/.infra-registered"
INFRA_URL="https://infra.local.homelabsolutions.net/api/infra/mac-addresses/reserve-mac-address"
INFRA_INSECURE=0

# Operator overrides, if any.
[[ -r /etc/server4home/infra.conf ]] && source /etc/server4home/infra.conf

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

log "POST $INFRA_URL  mac=$mac hostname=$hostname iface=$iface"

curl_args=(
    --silent --show-error
    --fail
    --max-time 10
    --connect-timeout 5
    --retry 2 --retry-delay 2
    --header 'Content-Type: application/json'
    --request POST
    --data "{\"mac\":\"$mac\",\"hostname\":\"$hostname\",\"interface\":\"$iface\"}"
    "$INFRA_URL"
)
[[ "$INFRA_INSECURE" == "1" ]] && curl_args=(--insecure "${curl_args[@]}")

if response=$(curl "${curl_args[@]}" 2>&1); then
    log "Registration succeeded. Response: $response"
    install -d -m 0755 "$(dirname "$SENTINEL")"
    {
        echo "registered_at=$(date -Is)"
        echo "mac=$mac"
        echo "hostname=$hostname"
        echo "url=$INFRA_URL"
    } > "$SENTINEL"
else
    rc=$?
    log "Registration failed (curl exit $rc). Keeping defaults; will retry on next boot."
fi

exit 0
