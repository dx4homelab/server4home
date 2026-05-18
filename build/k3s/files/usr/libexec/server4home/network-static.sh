#!/usr/bin/env bash
#
# network-static.sh — first-boot static-IP setup driven by SMBIOS OEM strings.
#
# If the VM was created with these OEM strings (DMI table type 11):
#   server4home-static-ip=<addr/cidr>
#   server4home-static-gw=<gateway>
#   server4home-static-dns=<dns>[|<dns>...]   (pipe-separated, optional)
# a NetworkManager keyfile is written to /etc/NetworkManager/system-connections/
# BEFORE NetworkManager.service starts, so the VM comes up with the static IP
# from the very first connection. No SMBIOS strings → DHCP (default).
#
# Idempotent: existing keyfile is not overwritten on re-runs.

set -euo pipefail

log() { printf '[network-static] %s\n' "$*"; }

if [[ ! -d /sys/firmware/dmi/tables ]]; then
    log "No SMBIOS table available; skipping."
    exit 0
fi

if ! command -v dmidecode >/dev/null 2>&1; then
    log "dmidecode not installed; cannot parse OEM strings. Skipping."
    exit 0
fi

raw="$(dmidecode -t 11 2>/dev/null || true)"

# dmidecode -t 11 lines look like: `\tString 1: key=value`.
# We don't care about leading whitespace or the "String N:" prefix.
ip=""; gw=""; dns=""
while IFS= read -r line; do
    case "$line" in
        *server4home-static-ip=*)  ip="${line#*server4home-static-ip=}" ;;
        *server4home-static-gw=*)  gw="${line#*server4home-static-gw=}" ;;
        *server4home-static-dns=*) dns="${line#*server4home-static-dns=}" ;;
    esac
done <<<"$raw"

if [[ -z "$ip" ]]; then
    log "No static IP requested via SMBIOS; keeping DHCP."
    exit 0
fi

# Identify a real ethernet device (skip lo, tailscale, virtual bridges).
iface=""
for dev in /sys/class/net/*; do
    name="$(basename "$dev")"
    case "$name" in
        lo|tailscale*|virbr*|br-*|docker*|veth*|cni*) continue ;;
    esac
    [[ -e "$dev/device" ]] || continue
    iface="$name"
    break
done
[[ -z "$iface" ]] && { log "No suitable ethernet interface found; aborting."; exit 1; }

profile="server4home-static-${iface}"
keyfile="/etc/NetworkManager/system-connections/${profile}.nmconnection"

if [[ -f "$keyfile" ]]; then
    log "Keyfile $keyfile already exists; not overwriting."
    exit 0
fi

# Convert pipe-separated DNS into NM's semicolon-terminated list.
dns_field=""
if [[ -n "$dns" ]]; then
    dns_field="${dns//|/;};"
fi

install -d -m 0755 /etc/NetworkManager/system-connections

umask 077
{
    echo "[connection]"
    echo "id=${profile}"
    echo "type=ethernet"
    echo "interface-name=${iface}"
    echo "autoconnect=true"
    echo "autoconnect-priority=100"
    echo ""
    echo "[ethernet]"
    echo ""
    echo "[ipv4]"
    echo "method=manual"
    echo "addresses=${ip}"
    [[ -n "$gw" ]]      && echo "gateway=${gw}"
    [[ -n "$dns_field" ]] && echo "dns=${dns_field}"
    echo ""
    echo "[ipv6]"
    echo "method=auto"
} > "$keyfile"
chmod 0600 "$keyfile"
chown root:root "$keyfile" 2>/dev/null || true

log "Wrote NM keyfile $keyfile (ip=$ip gw=$gw dns=$dns iface=$iface)."
