#!/usr/bin/env bash
#
# create-rancher-vm.sh — provision a Proxmox VM from a server4home qcow2.
#
# Run this ON the Proxmox host (it shells out to `qm` and `pvesm`).
# Expects the qcow2 to already exist at --qcow2 PATH on the Proxmox host
# (scp it over beforehand).
#
# The VM is configured for the requirements of the server4home image:
#   - UEFI (OVMF) firmware, q35 machine
#   - virtio-scsi for the disk (the qcow2 was built with btrfs root)
#   - virtio-net on the chosen bridge so the VM joins your LAN via DHCP
#   - host CPU passthrough for KVM perf and for K3s nested workloads
#
# After boot, the developer user from iso/disk.toml is created with your
# SSH key and passwordless wheel; bootc switch to your registry image is
# the recommended next step (see iso/iso.toml).

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: create-rancher-vm.sh --vmid <id> --name <name> --qcow2 <path> [options]

Required:
  --vmid <id>          Proxmox VM ID (numeric, e.g. 200). Must be unused.
  --name <name>        VM display name (e.g. rancher-cp-01).
  --qcow2 <path>       Path to source qcow2 on this Proxmox host.

Options:
  --memory <MB>         RAM in MB                       (default: 16384)
  --cores <n>           vCPUs                           (default: 4)
  --bridge <iface>      Network bridge                  (default: vmbr0)
  --storage <name>      PVE storage for disks           (default: local-lvm)
  --disk-size <size>    Resize boot disk after import   (default: 64G)
  --data-disk-size <s>  Add a second blank disk for     (default: none)
                        /var/lib/rancher (claimed by
                        the K3s first-boot LVM setup).
                        e.g. 100G. Omit to skip.
  --vlan <id>           VLAN tag for the NIC            (default: none)
  --start               Start the VM after creation     (default: leave stopped)
  --dry-run             Print qm commands without running them
  -h | --help           Show this help

Example:
  create-rancher-vm.sh \
    --vmid 200 --name rancher-cp-01 \
    --qcow2 /var/lib/vz/template/iso/server4home-k3s.qcow2 \
    --memory 16384 --cores 4 --disk-size 80G --start
EOF
}

VMID=""
NAME=""
QCOW2=""
MEMORY=16384
CORES=4
BRIDGE="vmbr0"
STORAGE="local-lvm"
DISK_SIZE="64G"
DATA_DISK_SIZE=""
VLAN=""
START=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --vmid)      VMID="$2"; shift 2 ;;
        --name)      NAME="$2"; shift 2 ;;
        --qcow2)     QCOW2="$2"; shift 2 ;;
        --memory)    MEMORY="$2"; shift 2 ;;
        --cores)     CORES="$2"; shift 2 ;;
        --bridge)    BRIDGE="$2"; shift 2 ;;
        --storage)   STORAGE="$2"; shift 2 ;;
        --disk-size) DISK_SIZE="$2"; shift 2 ;;
        --data-disk-size) DATA_DISK_SIZE="$2"; shift 2 ;;
        --vlan)      VLAN="$2"; shift 2 ;;
        --start)     START=1; shift ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

# Validation
[[ -n "$VMID" ]]  || { echo "ERROR: --vmid is required" >&2; exit 2; }
[[ -n "$NAME" ]]  || { echo "ERROR: --name is required" >&2; exit 2; }
[[ -n "$QCOW2" ]] || { echo "ERROR: --qcow2 is required" >&2; exit 2; }
[[ "$VMID" =~ ^[0-9]+$ ]] || { echo "ERROR: --vmid must be numeric" >&2; exit 2; }
[[ -f "$QCOW2" ]] || { echo "ERROR: qcow2 not found at $QCOW2" >&2; exit 2; }

command -v qm   >/dev/null || { echo "ERROR: 'qm' not found; run this on a Proxmox host." >&2; exit 3; }
command -v pvesm >/dev/null || { echo "ERROR: 'pvesm' not found; run this on a Proxmox host." >&2; exit 3; }

if qm status "$VMID" >/dev/null 2>&1; then
    echo "ERROR: VMID $VMID is already in use. Pick another or destroy it first (qm destroy $VMID)." >&2
    exit 4
fi

# Build the NIC string (with VLAN tag if requested)
NET0="virtio,bridge=${BRIDGE}"
[[ -n "$VLAN" ]] && NET0="${NET0},tag=${VLAN}"

run() {
    if (( DRY_RUN )); then
        printf '  [dry-run] %s\n' "$*"
    else
        printf '  $ %s\n' "$*"
        eval "$@"
    fi
}

echo ">>> Creating VM $VMID ($NAME) on bridge=$BRIDGE storage=$STORAGE"

# SMBIOS values must be base64-encoded for `qm set --smbios1`. The guest reads
# the product name via /sys/class/dmi/id/product_name and uses it as the
# hostname prefix in server4home-hostname.service.
b64() { printf '%s' "$1" | base64 --wrap=0; }
SMBIOS="uuid=$(uuidgen),manufacturer=$(b64 server4home),product=$(b64 "$NAME")"

# 1) Create the VM shell (UEFI + q35 + virtio-scsi + serial console for journalctl)
run qm create "$VMID" \
    --name "$NAME" \
    --memory "$MEMORY" \
    --cores "$CORES" \
    --cpu host \
    --ostype l26 \
    --machine q35 \
    --bios ovmf \
    --efidisk0 "${STORAGE}:0,efitype=4m,pre-enrolled-keys=0,format=raw" \
    --scsihw virtio-scsi-single \
    --net0 "$NET0" \
    --serial0 socket \
    --vga serial0 \
    --smbios1 "$SMBIOS" \
    --agent enabled=1

# 2) Import the qcow2; lands as an unused disk
# NOTE: no `--format qcow2`. On LVM-thin / Ceph / iSCSI etc. the storage only
# accepts raw; passing `--format qcow2` makes qm transfer the bytes, print
# "successfully imported", then silently roll back the LV on finalize so
# the next attach fails with "no such logical volume". Let qm pick the
# storage's default format.
echo ">>> Importing disk from $QCOW2 into $STORAGE (this can take a few minutes)"
run qm importdisk "$VMID" "$QCOW2" "$STORAGE"

# 3) Attach the imported disk as scsi0 and set boot order
#    The imported disk shows up as ${STORAGE}:vm-${VMID}-disk-1 (after efidisk0
#    took disk-0). Reference it as `unused0` for a clean handoff.
run qm set "$VMID" --scsi0 "${STORAGE}:vm-${VMID}-disk-1,discard=on,iothread=1,ssd=1"
run qm set "$VMID" --boot order=scsi0

# 4) Resize the boot disk to the requested size
echo ">>> Resizing scsi0 to $DISK_SIZE"
run qm resize "$VMID" scsi0 "$DISK_SIZE"

# 5) Optional second (data) disk for /var/lib/rancher
#    The K3s image's first-boot service detects an unformatted disk, creates
#    PV/VG/LV/xfs on it, and mounts /var/lib/rancher before k3s starts.
if [[ -n "$DATA_DISK_SIZE" ]]; then
    echo ">>> Adding blank data disk scsi1 ($DATA_DISK_SIZE) for /var/lib/rancher"
    run qm set "$VMID" --scsi1 "${STORAGE}:${DATA_DISK_SIZE%G},discard=on,iothread=1,ssd=1"
fi

# 6) Optional auto-start
if (( START )); then
    echo ">>> Starting VM $VMID"
    run qm start "$VMID"
fi

echo ""
echo "Done. Inspect with:  qm config $VMID"
echo "Console:             qm terminal $VMID   (Ctrl-O q  to detach)"
if (( ! START )); then
    echo "Start:               qm start $VMID"
fi
echo ""
echo "Once it boots, it will request DHCP on $BRIDGE. SSH in as 'developer'"
echo "(SSH key from iso/disk.toml). Then optionally:"
echo "  sudo bootc switch ghcr.io/dx4homelab/server4home-k3s:stable"
echo "  sudo reboot"
