#!/usr/bin/env bash
#
# setup-rancher-data.sh — first-boot (and idempotent) preparation of an LVM
# data volume for /var/lib/rancher. Detects an empty secondary disk, creates
# PV → VG `rancher` → LV `data` → xfs, and mounts at /var/lib/rancher.
#
# Behavior:
#   - If /var/lib/rancher is already mounted   -> exit 0 (nothing to do).
#   - If VG `rancher` exists                   -> activate + mount its LV.
#   - Otherwise, find the largest *unformatted* non-root disk and use it.
#   - If no candidate disk is found            -> exit 0 (k3s runs on root).
#
# Idempotent: re-running on a configured system is a no-op.

set -euo pipefail

VG_NAME="rancher"
LV_NAME="data"
MOUNT_POINT="/var/lib/rancher"
FS_LABEL="rancher-data"
FSTAB_MARKER="# server4home-rancher-data"

log()  { printf '[rancher-data] %s\n' "$*"; }
fail() { printf '[rancher-data] ERROR: %s\n' "$*" >&2; exit 1; }

# Already mounted? (e.g., fstab took care of it on this boot.)
if mountpoint -q "$MOUNT_POINT"; then
    log "$MOUNT_POINT already mounted; nothing to do."
    exit 0
fi

mkdir -p "$MOUNT_POINT"

# If the VG already exists (subsequent boots, or operator-prepared disk),
# just activate it and mount the LV.
if vgs --noheadings -o vg_name 2>/dev/null | tr -d ' ' | grep -qx "$VG_NAME"; then
    log "VG $VG_NAME exists; activating and mounting."
    vgchange -ay "$VG_NAME" >/dev/null
else
    # Identify the root block device (e.g. /dev/vda) so we never touch it.
    root_part=$(findmnt -no SOURCE /)
    root_part=$(realpath "$root_part")
    root_disk=$(lsblk -no PKNAME "$root_part" 2>/dev/null || true)
    [[ -n "$root_disk" ]] || root_disk="$(basename "$root_part" | sed 's/[0-9]*$//')"

    log "Root disk: /dev/$root_disk; searching for an unformatted data disk."

    candidate=""
    candidate_size=0
    while read -r name; do
        [[ "$name" == "$root_disk" ]] && continue
        dev="/dev/$name"
        [[ -b "$dev" ]] || continue

        # Skip devices that already carry any filesystem, partition table,
        # LVM PV, or other recognized signature.
        if blkid -p "$dev" >/dev/null 2>&1; then
            continue
        fi
        # Also skip if it has child devices (partitions).
        if [[ $(lsblk -n "$dev" | wc -l) -gt 1 ]]; then
            continue
        fi

        size=$(blockdev --getsize64 "$dev" 2>/dev/null || echo 0)
        if (( size > candidate_size )); then
            candidate="$dev"
            candidate_size="$size"
        fi
    done < <(lsblk -dn -o NAME,TYPE | awk '$2=="disk"{print $1}')

    if [[ -z "$candidate" ]]; then
        log "No unformatted secondary disk found. K3s will use the root disk."
        exit 0
    fi

    human_size=$(numfmt --to=iec --suffix=B "$candidate_size")
    log "Using $candidate ($human_size) for VG $VG_NAME."

    pvcreate -ff -y "$candidate"
    vgcreate "$VG_NAME" "$candidate"
    lvcreate -y -l 100%FREE -n "$LV_NAME" "$VG_NAME"
    mkfs.xfs -f -L "$FS_LABEL" "/dev/$VG_NAME/$LV_NAME"
fi

# Mount now (this boot)
mount "/dev/$VG_NAME/$LV_NAME" "$MOUNT_POINT"

# Persist to /etc/fstab if not already there.
if ! grep -q "$FSTAB_MARKER" /etc/fstab 2>/dev/null; then
    uuid=$(blkid -s UUID -o value "/dev/$VG_NAME/$LV_NAME")
    [[ -n "$uuid" ]] || fail "Could not read UUID for /dev/$VG_NAME/$LV_NAME"
    {
        echo ""
        echo "$FSTAB_MARKER"
        echo "UUID=$uuid  $MOUNT_POINT  xfs  defaults,nofail,x-systemd.device-timeout=30  0 0"
    } >> /etc/fstab
    log "Added fstab entry for $MOUNT_POINT (UUID=$uuid)."
    # Tell systemd we changed /etc/fstab so its fstab-generator re-runs and
    # creates the matching var-lib-rancher.mount unit. Without this, every
    # subsequent `mount` / `bootc status` prints the "fstab modified, systemd
    # still uses the old version" hint until the next reboot.
    systemctl daemon-reload || log "WARN: systemctl daemon-reload failed (non-fatal)."
fi

log "Done. $MOUNT_POINT is on /dev/$VG_NAME/$LV_NAME."
