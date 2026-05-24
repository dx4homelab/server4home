#!/usr/bin/env bash
#
# bootc-switch.sh — first-boot OS-image switch.
#
# A freshly-deployed server4home VM boots a qcow2 baked from a local
# image (e.g. `localhost/server4home-k3s:stable`). That ref doesn't exist
# at runtime — the bootc-fetch-apply-updates.timer ticks against it and
# silently finds nothing. So new VMs never pick up CI-published security
# / feature updates from GHCR.
#
# This unit closes the gap: on first boot, read the desired image ref
# from SMBIOS (set by the runner via the manifest's `upgrade2image:`
# field), compare to the currently-booted ref, and `bootc switch`
# + `bootc upgrade --apply` if they differ. The `--apply` triggers a
# reboot into the new image, so when k3s.service finally starts the
# system is on GHCR (and signed-image enforcement works).
#
# Fail-soft policy: every error path exits 0. Boot must never depend on
# an external registry being reachable. The unit's `SuccessExitStatus=`
# already accepts non-zero, but we keep the script honest too: if
# anything goes wrong, leave the sentinel UNWRITTEN so the next boot
# retries.

set -uo pipefail

log()  { printf '[bootc-switch] %s\n' "$*"; }
fail() { log "WARN: $*"; exit 0; }

SENTINEL="/var/lib/server4home/.bootc-switched"

# Already switched on a previous boot?
if [[ -f "$SENTINEL" ]]; then
    log "Sentinel $SENTINEL present; nothing to do."
    exit 0
fi

# Read the target image ref from SMBIOS OEM strings (DMI type 11). The
# runner injects this as `server4home-image-ref=<ref>` alongside the
# hostname / static-IP / k3s-mode strings it already sets. Absence of
# the field means "no opinion" — exit 0 quietly.
if ! command -v dmidecode >/dev/null 2>&1; then
    fail "dmidecode missing; cannot read SMBIOS"
fi

target_ref="$(dmidecode -t 11 2>/dev/null \
    | awk -F= '/server4home-image-ref/{print $2; exit}' \
    | tr -d '[:space:]')"

if [[ -z "$target_ref" ]]; then
    log "No server4home-image-ref in SMBIOS; leaving image untouched."
    exit 0
fi

# Localhost target → nothing to fetch. Used by image-build / local-test
# flows where the developer doesn't want the VM swapping to GHCR behind
# their back.
if [[ "$target_ref" == localhost/* ]]; then
    log "Target is $target_ref (localhost) — short-circuit; no switch."
    install -d -m 0755 "$(dirname "$SENTINEL")"
    echo "skipped_at=$(date -Is)"  > "$SENTINEL"
    echo "reason=localhost-target" >> "$SENTINEL"
    exit 0
fi

# What's currently booted?
if ! current_json="$(bootc status --json 2>/dev/null)"; then
    fail "bootc status failed; not on a bootc system?"
fi
current_ref="$(printf '%s' "$current_json" \
    | python3 -c 'import json,sys;d=json.load(sys.stdin);print(((d.get("status") or {}).get("booted") or {}).get("image",{}).get("image",{}).get("image",""))' \
    2>/dev/null)"

if [[ -z "$current_ref" ]]; then
    fail "Could not parse current bootc image ref"
fi

log "Current image: $current_ref"
log "Target image:  $target_ref"

# Already on the target ref? (E.g. someone redeployed a VM that's
# already on GHCR.) Mark the sentinel and exit so the timer takes over.
if [[ "$current_ref" == "$target_ref" ]]; then
    log "Already on target; recording sentinel and exiting."
    install -d -m 0755 "$(dirname "$SENTINEL")"
    {
        echo "skipped_at=$(date -Is)"
        echo "reason=already-on-target"
        echo "ref=$target_ref"
    } > "$SENTINEL"
    exit 0
fi

# Do the switch. `bootc switch` updates /etc/ostree/remotes.d & the
# stored ref but doesn't reboot; `bootc upgrade --apply` pulls and
# reboots immediately.
log "Switching: $current_ref -> $target_ref"
if ! bootc switch "$target_ref"; then
    fail "bootc switch failed; leaving image untouched"
fi

# Record the sentinel BEFORE rebooting so the post-reboot run doesn't
# infinite-loop. The reboot from `bootc upgrade --apply` is the last
# thing that happens.
install -d -m 0755 "$(dirname "$SENTINEL")"
{
    echo "switched_at=$(date -Is)"
    echo "from=$current_ref"
    echo "to=$target_ref"
} > "$SENTINEL"

log "Applying upgrade (reboot incoming)"
exec bootc upgrade --apply
