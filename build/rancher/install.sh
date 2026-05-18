#!/usr/bin/env bash
# Install Helm into /usr/bin at image build time. cert-manager and Rancher
# themselves are installed on the running cluster at first boot
# (build/rancher/files/usr/libexec/server4home/rancher-bootstrap.sh).

set -euo pipefail

: "${HELM_VERSION:?HELM_VERSION must be set, e.g. v3.21.0}"

arch="linux-amd64"
url="https://get.helm.sh/helm-${HELM_VERSION}-${arch}.tar.gz"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

curl --fail --silent --show-error --location "$url" -o "$tmpdir/helm.tar.gz"
tar -xzf "$tmpdir/helm.tar.gz" -C "$tmpdir"
install -m 0755 "$tmpdir/${arch}/helm" /usr/bin/helm

helm version --short
echo "Helm ${HELM_VERSION} installed."
