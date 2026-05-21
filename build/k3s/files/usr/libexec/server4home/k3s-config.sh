#!/usr/bin/env bash
#
# k3s-config.sh — first-boot writer for /etc/server4home/k3s.conf from SMBIOS.
#
# tools/deploy reads a manifest's k3s join config (mode / server / token,
# the token resolved from the local secret store) and injects it as SMBIOS
# OEM strings (DMI type 11):
#
#   server4home-k3s-mode=agent
#   server4home-k3s-url=https://k3s-cp-01.lan:6443
#   server4home-k3s-token=K10...
#
# This script runs Before=k3s.service and materializes those into
# /etc/server4home/k3s.conf, which k3s.service reads as an EnvironmentFile.
#
# No strings, or an operator-provided k3s.conf already present => do nothing
# (the VM then starts a fresh single-node server, the default).

set -euo pipefail

log() { printf '[k3s-config] %s\n' "$*"; }

CONF="/etc/server4home/k3s.conf"

if [[ -f "$CONF" ]]; then
    log "$CONF already present; leaving it untouched."
    exit 0
fi

if ! command -v dmidecode >/dev/null 2>&1 || [[ ! -d /sys/firmware/dmi/tables ]]; then
    log "No SMBIOS access; skipping (VM will start as a new k3s server)."
    exit 0
fi

mode=""; url=""; token=""
while IFS= read -r line; do
    case "$line" in
        *server4home-k3s-mode=*)  mode="${line#*server4home-k3s-mode=}" ;;
        *server4home-k3s-url=*)   url="${line#*server4home-k3s-url=}" ;;
        *server4home-k3s-token=*) token="${line#*server4home-k3s-token=}" ;;
    esac
done < <(dmidecode -t 11 2>/dev/null || true)

if [[ -z "$mode" && -z "$url" && -z "$token" ]]; then
    log "No k3s join config in SMBIOS; VM will start as a new k3s server."
    exit 0
fi

install -d -m 0755 /etc/server4home
umask 077
{
    echo "# Written at first boot by k3s-config.sh from SMBIOS OEM strings."
    [[ -n "$mode" ]]  && echo "K3S_MODE=${mode}"
    [[ -n "$url" ]]   && echo "K3S_URL=${url}"
    [[ -n "$token" ]] && echo "K3S_TOKEN=${token}"
} > "$CONF"
chmod 0600 "$CONF"

log "Wrote $CONF (mode=${mode:-server} url=${url:-<none>} token=${token:+<set>})"
