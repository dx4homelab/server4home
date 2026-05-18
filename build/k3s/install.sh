#!/usr/bin/env bash
# Install K3s into the image (binary + selinux policy + systemd unit + symlinks).
# Mode (server/agent) is decided at runtime by /etc/server4home/k3s.conf.

set -euo pipefail

: "${K3S_VERSION:?K3S_VERSION must be set, e.g. v1.35.4+k3s1}"

# Note: base is Fedora CoreOS, for which upstream k3s-selinux does not ship.
# K3s runs without it as long as --selinux is not enabled (it isn't here).
# A warning will appear in journal at first start; safe to ignore.

# qemu-guest-agent — lets the libvirt host query the VM's IP via
# `virsh domifaddr --source agent`, which is the most reliable IP discovery
# path for bridge-attached guests.
dnf install -y qemu-guest-agent
dnf clean all

# K3s binary. URL-encode the '+' that appears in K3s version tags.
url_version="${K3S_VERSION//+/%2B}"
curl --fail --silent --show-error --location \
    "https://github.com/k3s-io/k3s/releases/download/${url_version}/k3s" \
    --output /usr/bin/k3s
chmod 0755 /usr/bin/k3s

# K3s embeds kubectl/crictl/ctr; expose them as symlinks.
ln -sf /usr/bin/k3s /usr/bin/kubectl
ln -sf /usr/bin/k3s /usr/bin/crictl
ln -sf /usr/bin/k3s /usr/bin/ctr

# Note: the enable-symlink is shipped in
# /usr/lib/systemd/system/multi-user.target.wants/k3s.service (via the COPY of
# build/k3s/files/). On Fedora CoreOS, /etc is overlay-managed on deploy, so a
# `systemctl enable` here would not persist; the /usr symlink does.

install -d -m 0755 /etc/server4home

echo "K3s ${K3S_VERSION} installed."
