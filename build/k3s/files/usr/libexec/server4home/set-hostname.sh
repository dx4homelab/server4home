#!/usr/bin/env bash
#
# set-hostname.sh — first-boot hostname assignment.
#
# Two modes:
#
# (A) Exact mode (preferred; used by tools/deploy.sh).
#     SMBIOS OEM string (DMI table 11) contains:
#         server4home-hostname-exact=<full-hostname>
#     The script sets that as the hostname verbatim. No suffix.
#
# (B) Prefix mode (legacy/fallback; used by raw `just import-libvirt`).
#     Hostname = <prefix>-<8 hex from /etc/machine-id>, where <prefix> is:
#       1) SMBIOS system-product-name, OR
#       2) /etc/server4home/hostname-prefix file contents, OR
#       3) "server4home"
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

# ---- Mode (A): exact hostname via OEM string -----------------------------
EXACT=""
if command -v dmidecode >/dev/null 2>&1 && [[ -d /sys/firmware/dmi/tables ]]; then
    while IFS= read -r line; do
        line="${line# }"
        case "$line" in
            String*server4home-hostname-exact=*)
                EXACT="${line#*server4home-hostname-exact=}"
                ;;
        esac
    done < <(dmidecode -t 11 2>/dev/null || true)
fi

if [[ -n "$EXACT" ]]; then
    # Light sanitization (DNS-safe), but keep dots so FQDNs work.
    EXACT="$(echo "$EXACT" | tr '[:upper:]' '[:lower:]' \
        | tr -c 'a-z0-9.-' '-' \
        | sed -E 's/^-+|-+$//g; s/-+/-/g' \
        | cut -c1-253)"
    if [[ -n "$EXACT" ]]; then
        log "Exact hostname from SMBIOS: '$EXACT'"
        hostnamectl set-hostname "$EXACT"
        exit 0
    fi
fi

# ---- Mode (B): prefix + machine-id suffix --------------------------------
NAME=""

if [[ -r /sys/class/dmi/id/product_name ]]; then
    raw="$(tr -d '[:space:]' </sys/class/dmi/id/product_name || true)"
    case "$raw" in
        ""|"KVM"|"StandardPC"|"Bochs"|"QEMU"|"Default") ;;
        *) NAME="$raw" ;;
    esac
fi

if [[ -r /etc/server4home/hostname-prefix ]]; then
    file_name="$(tr -d '[:space:]' </etc/server4home/hostname-prefix || true)"
    [[ -n "$file_name" ]] && NAME="$file_name"
fi

[[ -z "$NAME" ]] && NAME="server4home"

NAME="$(echo "$NAME" | tr '[:upper:]' '[:lower:]' \
    | tr -c 'a-z0-9-' '-' \
    | sed -E 's/^-+|-+$//g; s/-+/-/g' \
    | cut -c1-50)"
[[ -z "$NAME" ]] && NAME="server4home"

if [[ -r /etc/machine-id ]]; then
    SUFFIX="$(cut -c1-8 /etc/machine-id)"
else
    SUFFIX="$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi

NEW_HOSTNAME="${NAME}-${SUFFIX}"
log "Setting hostname to '$NEW_HOSTNAME' (prefix=$NAME suffix=$SUFFIX)"
hostnamectl set-hostname "$NEW_HOSTNAME"
