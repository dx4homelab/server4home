#!/usr/bin/env bash
#
# set-hostname.sh — first-boot hostname assignment.
#
# Builds the hostname from two parts:
#   1. NAME      = SMBIOS system-product-name, set by the deploy script
#                  (`just import-libvirt <vm_name>` or the Proxmox helper).
#                  Falls back to /etc/server4home/hostname-prefix, then
#                  to "server4home".
#   2. SUFFIX    = first 8 hex chars of /etc/machine-id (unique per VM).
#
# Result: "<NAME>-<SUFFIX>" e.g. "server4home-k3s-3f9ab21c"
#
# Idempotent: if hostnamectl --static already reports something other than
# "localhost" or empty, this script does nothing.

set -euo pipefail

log()  { printf '[set-hostname] %s\n' "$*"; }

current="$(hostnamectl --static 2>/dev/null || true)"
case "$current" in
    ""|"localhost"|"localhost.localdomain")
        ;;
    *)
        log "Hostname already set to '$current'; nothing to do."
        exit 0
        ;;
esac

NAME=""

# 1) Prefer SMBIOS product name (injected by virt-install/qm).
if [[ -r /sys/class/dmi/id/product_name ]]; then
    raw="$(tr -d '[:space:]' </sys/class/dmi/id/product_name || true)"
    # Common defaults we want to ignore: "KVM", "Standard PC", etc.
    case "$raw" in
        ""|"KVM"|"StandardPC"|"Bochs"|"QEMU"|"Default") ;;
        *) NAME="$raw" ;;
    esac
fi

# 2) Operator-dropped file overrides SMBIOS in either direction:
#    /etc/server4home/hostname-prefix takes precedence if it exists.
if [[ -r /etc/server4home/hostname-prefix ]]; then
    file_name="$(tr -d '[:space:]' </etc/server4home/hostname-prefix || true)"
    [[ -n "$file_name" ]] && NAME="$file_name"
fi

# 3) Fallback.
[[ -z "$NAME" ]] && NAME="server4home"

# Sanitize to a DNS-safe label (lowercase, alnum + dash, max 50 chars).
NAME="$(echo "$NAME" | tr '[:upper:]' '[:lower:]' \
    | tr -c 'a-z0-9-' '-' \
    | sed -E 's/^-+|-+$//g; s/-+/-/g' \
    | cut -c1-50)"
[[ -z "$NAME" ]] && NAME="server4home"

# Compute UUID suffix from /etc/machine-id (always 32 hex chars on systemd).
if [[ -r /etc/machine-id ]]; then
    SUFFIX="$(cut -c1-8 /etc/machine-id)"
else
    SUFFIX="$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi

NEW_HOSTNAME="${NAME}-${SUFFIX}"
log "Setting hostname to '$NEW_HOSTNAME'"
hostnamectl set-hostname "$NEW_HOSTNAME"
