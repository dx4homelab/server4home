#!/usr/bin/env bash
#
# rancher-bootstrap.sh — first-boot helm install of cert-manager + Rancher
# Manager on the freshly-started K3s cluster.
#
# Skips when:
#   - This is an agent node (K3S_MODE=agent in /etc/server4home/k3s.conf).
#   - Rancher is already deployed in the cattle-system namespace.
#   - A sentinel at /var/lib/server4home/.rancher-bootstrap-done is present.
#
# Behavior on failure:
#   - Exits non-zero so systemctl marks the unit failed and journalctl shows
#     what went wrong. Re-running `systemctl start rancher-bootstrap.service`
#     resumes from where it failed (idempotent thanks to helm upgrade --install).
#
# Overrides are read from /etc/server4home/rancher.conf at runtime.

set -euo pipefail

log()  { printf '[rancher-bootstrap] %s\n' "$*"; }
fail() { printf '[rancher-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

SENTINEL="/var/lib/server4home/.rancher-bootstrap-done"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# ---- Agent guard ---------------------------------------------------------
K3S_MODE="server"
[[ -r /etc/server4home/k3s.conf ]] && source /etc/server4home/k3s.conf
if [[ "$K3S_MODE" == "agent" ]]; then
    log "K3S_MODE=agent; Rancher is a cluster-wide app — skip on workers."
    exit 0
fi

# ---- Already done? -------------------------------------------------------
if [[ -f "$SENTINEL" ]]; then
    log "Sentinel present; Rancher already bootstrapped. Nothing to do."
    exit 0
fi

# ---- Config & defaults ---------------------------------------------------
# These are bake-time defaults; override in /etc/server4home/rancher.conf.
RANCHER_HOSTNAME=""
RANCHER_BOOTSTRAP_PASSWORD="admin"
RANCHER_CHART_VERSION="2.14.1"
CERT_MANAGER_VERSION="v1.18.6"
RANCHER_REPLICAS="1"
[[ -r /etc/server4home/rancher.conf ]] && source /etc/server4home/rancher.conf

if [[ -z "$RANCHER_HOSTNAME" ]]; then
    RANCHER_HOSTNAME="$(hostnamectl --static 2>/dev/null || hostname)"
fi
[[ -z "$RANCHER_HOSTNAME" ]] && fail "Could not determine RANCHER_HOSTNAME (set in /etc/server4home/rancher.conf)"

# ---- Wait for K3s API ----------------------------------------------------
log "Waiting for K3s API to be reachable..."
for _ in $(seq 1 60); do
    if kubectl get --raw='/readyz' >/dev/null 2>&1; then
        break
    fi
    sleep 5
done
kubectl get --raw='/readyz' >/dev/null 2>&1 || fail "K3s API not ready after 5 min"

log "Waiting for at least one node to be Ready..."
for _ in $(seq 1 60); do
    if kubectl get nodes \
        -o jsonpath='{.items[*].status.conditions[?(@.type=="Ready")].status}' \
        2>/dev/null | grep -q True; then
        break
    fi
    sleep 5
done

# ---- Skip if Rancher already deployed (e.g. operator-installed earlier) --
if kubectl get -n cattle-system deployment rancher >/dev/null 2>&1; then
    log "Rancher deployment already exists in cattle-system. Marking done."
    install -d "$(dirname "$SENTINEL")"
    : > "$SENTINEL"
    exit 0
fi

# ---- Install cert-manager ------------------------------------------------
log "Installing cert-manager ${CERT_MANAGER_VERSION}"
helm repo add jetstack https://charts.jetstack.io --force-update
helm repo update

helm upgrade --install cert-manager jetstack/cert-manager \
    --namespace cert-manager --create-namespace \
    --version "$CERT_MANAGER_VERSION" \
    --set installCRDs=true \
    --wait --timeout 10m

# ---- Install Rancher -----------------------------------------------------
log "Installing Rancher ${RANCHER_CHART_VERSION} (hostname=$RANCHER_HOSTNAME)"
helm repo add rancher-stable https://releases.rancher.com/server-charts/stable --force-update
helm repo update

helm upgrade --install rancher rancher-stable/rancher \
    --namespace cattle-system --create-namespace \
    --version "$RANCHER_CHART_VERSION" \
    --set "hostname=$RANCHER_HOSTNAME" \
    --set "bootstrapPassword=$RANCHER_BOOTSTRAP_PASSWORD" \
    --set "replicas=$RANCHER_REPLICAS" \
    --wait --timeout 20m

# ---- Sentinel + summary --------------------------------------------------
install -d "$(dirname "$SENTINEL")"
{
    echo "bootstrapped_at=$(date -Is)"
    echo "rancher_version=$RANCHER_CHART_VERSION"
    echo "cert_manager_version=$CERT_MANAGER_VERSION"
    echo "hostname=$RANCHER_HOSTNAME"
} > "$SENTINEL"

cat <<EOF
[rancher-bootstrap] Done.

  Rancher UI:        https://${RANCHER_HOSTNAME}/
  Bootstrap password: ${RANCHER_BOOTSTRAP_PASSWORD}
  Bootstrap secret:   kubectl get secret --namespace cattle-system bootstrap-secret -o go-template='{{ .data.bootstrapPassword | base64decode }}'

First UI login will prompt you to set a permanent admin password.
EOF
