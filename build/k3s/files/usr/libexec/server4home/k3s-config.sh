#!/usr/bin/env bash
#
# k3s-config.sh — first-boot K3s bootstrap config from SMBIOS.
#
# Two jobs, both before k3s.service starts:
#
# 1. Write /etc/server4home/k3s.conf (env file: K3S_MODE/K3S_URL/K3S_TOKEN)
#    from SMBIOS OEM strings (DMI type 11) injected by tools/deploy:
#        server4home-k3s-mode=agent
#        server4home-k3s-url=https://k3s-cp-01.lan:6443
#        server4home-k3s-token=K10...
#
# 2. Decide the datastore. A *new-cluster server* (server mode, not joining
#    anything) defaults to embedded etcd via a `cluster-init: true` drop-in.
#    Embedded etcd is HA-capable and the datastore choice cannot be changed
#    in place later, so it is the safe default. Opt out with:
#        server4home-k3s-datastore=sqlite
#    Joining servers and agents never get cluster-init.
#
# An operator-provided /etc/server4home/k3s.conf is left untouched; its
# K3S_MODE/K3S_URL are still read to make the datastore decision.

set -euo pipefail

log() { printf '[k3s-config] %s\n' "$*"; }

CONF="/etc/server4home/k3s.conf"
DATASTORE_DROPIN="/etc/rancher/k3s/config.yaml.d/00-server4home-datastore.yaml"

# --- Gather mode/url/token/datastore from SMBIOS ---------------------------
mode=""; url=""; token=""; datastore=""
if command -v dmidecode >/dev/null 2>&1 && [[ -d /sys/firmware/dmi/tables ]]; then
    while IFS= read -r line; do
        case "$line" in
            *server4home-k3s-mode=*)      mode="${line#*server4home-k3s-mode=}" ;;
            *server4home-k3s-url=*)       url="${line#*server4home-k3s-url=}" ;;
            *server4home-k3s-token=*)     token="${line#*server4home-k3s-token=}" ;;
            *server4home-k3s-datastore=*) datastore="${line#*server4home-k3s-datastore=}" ;;
        esac
    done < <(dmidecode -t 11 2>/dev/null || true)
fi

# --- Job 1: /etc/server4home/k3s.conf --------------------------------------
if [[ -f "$CONF" ]]; then
    log "$CONF already present; leaving it untouched."
    # Read mode/url from it so the datastore decision below is consistent.
    # shellcheck disable=SC1090
    source "$CONF" 2>/dev/null || true
    mode="${K3S_MODE:-${mode:-server}}"
    url="${K3S_URL:-${url:-}}"
elif [[ -n "${mode}${url}${token}" ]]; then
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
else
    log "No k3s join config in SMBIOS; starting a new k3s server."
fi

# --- Job 2: datastore / cluster-init ---------------------------------------
mode="${mode:-server}"
if [[ "$mode" == "server" && -z "$url" && "$datastore" != "sqlite" ]]; then
    if [[ -f "$DATASTORE_DROPIN" ]]; then
        log "cluster-init drop-in already present; leaving it."
    else
        install -d -m 0755 /etc/rancher/k3s/config.yaml.d
        {
            echo "# Written by k3s-config.sh: new cluster -> embedded etcd."
            echo "cluster-init: true"
        } > "$DATASTORE_DROPIN"
        chmod 0644 "$DATASTORE_DROPIN"
        log "New cluster: enabled embedded etcd (cluster-init: true)."
    fi
else
    log "No cluster-init (mode=$mode url=${url:-<none>} datastore=${datastore:-etcd})."
fi
