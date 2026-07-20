#!/usr/bin/bash

set -eoux pipefail

###############################################################################
# Main Build Script
###############################################################################
# This script follows the @ublue-os/bluefin pattern for build scripts.
# It uses set -eoux pipefail for strict error handling and debugging.
###############################################################################

# Source helper functions
# shellcheck source=/dev/null
source /ctx/build/copr-helpers.sh

# Enable nullglob for all glob operations to prevent failures on empty matches
shopt -s nullglob

echo "::group:: Copy Bluefin Config from Common"

# Copy just files from @projectbluefin/common (includes 00-entry.just which imports 60-custom.just)
mkdir -p /usr/share/ublue-os/just/
shopt -s nullglob
cp -r /ctx/oci/common/bluefin/usr/share/ublue-os/just/* /usr/share/ublue-os/just/
shopt -u nullglob

echo "::endgroup::"

echo "::group:: Copy Custom Files"

# Copy Brewfiles to standard location
mkdir -p /usr/share/ublue-os/homebrew/
cp /ctx/custom/brew/*.Brewfile /usr/share/ublue-os/homebrew/

# Consolidate Just Files
find /ctx/custom/ujust -iname '*.just' -exec printf "\n\n" \; -exec cat {} \; >> /usr/share/ublue-os/just/60-custom.just

# Copy Flatpak preinstall files
mkdir -p /etc/flatpak/preinstall.d/
cp /ctx/custom/flatpaks/*.preinstall /etc/flatpak/preinstall.d/

echo "::endgroup::"

echo "::group:: Install Packages"

# Install packages using dnf5
# Example: dnf5 install -y tmux

# Example using COPR with isolated pattern:
# copr_install_isolated "ublue-os/staging" package-name

echo "::endgroup::"

echo "::group:: Container Image Signature Verification"

# Bake cosign signature verification for this image's own registry namespace so
# that `bootc upgrade`/`bootc switch` refuse an unsigned or tampered image out of
# the box — no per-VM /etc configuration required. Covers the base image
# (ghcr.io/dx4homelab/server4home) and the K3s flavor (…/server4home-k3s), which
# CI signs with the same key. Because the K3s flavor is built FROM this base, it
# inherits all of this automatically.

# Key goes in /usr/lib (immutable, part of the image) rather than /etc, which is
# overlay-managed and reset on deploy for Fedora CoreOS bases.
install -D -m 0644 /ctx/cosign.pub /usr/lib/pki/containers/server4home.pub

# Tell containers/image to look for cosign's sigstore attachments — the
# sha256-<digest>.sig tags — in this registry. Without this the signatures are
# never fetched and verification fails closed.
install -d -m 0755 /etc/containers/registries.d
cat > /etc/containers/registries.d/dx4homelab.yaml <<'EOF'
docker:
  ghcr.io/dx4homelab:
    use-sigstore-attachments: true
EOF

# Merge sigstoreSigned rules into the base policy, preserving its default:reject,
# the ublue-os rules, and the docker catch-all. Scoped to the server4home family
# repos specifically (not the whole org) so sibling images signed with other keys
# — e.g. ghcr.io/dx4homelab/bluefin-dx — keep falling through the catch-all and a
# rebase to them is not blocked. Add a repo here if a new flavor is published.
python3 - <<'PY'
import json, os

path = "/etc/containers/policy.json"
policy = json.load(open(path)) if os.path.exists(path) else {
    "default": [{"type": "reject"}],
    "transports": {"docker": {"": [{"type": "insecureAcceptAnything"}]}},
}
rule = [{
    "type": "sigstoreSigned",
    "keyPath": "/usr/lib/pki/containers/server4home.pub",
    "signedIdentity": {"type": "matchRepository"},
}]
docker = policy.setdefault("transports", {}).setdefault("docker", {})
for repo in ("ghcr.io/dx4homelab/server4home", "ghcr.io/dx4homelab/server4home-k3s"):
    docker[repo] = rule
with open(path, "w") as f:
    json.dump(policy, f, indent=4)
    f.write("\n")
PY

echo "::endgroup::"

echo "::group:: System Configuration"

# Enable/disable systemd services
systemctl enable podman.socket
# Example: systemctl mask unwanted-service

echo "::endgroup::"

# Restore default glob behavior
shopt -u nullglob

echo "Custom build complete!"
