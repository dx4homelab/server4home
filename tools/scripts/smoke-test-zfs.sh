#!/usr/bin/env bash
# Verify a built image carries a ZFS kmod that matches its own kernel.
#
# What this catches
# -----------------
# uCore's ZFS module is built out-of-tree by ublue-os/akmods against one specific
# kernel build. When Fedora bumps the kernel faster than OpenZFS supports it, an
# image can ship a kernel with no matching zfs.ko. Nothing in the container build
# fails: the image builds green, pushes green, and the breakage only appears when
# the storage server reboots and the pool will not import.
#
# We cannot `modprobe zfs` in CI (the runner kernel is not the image kernel), so
# the checks are static:
#   1. identify the kernel the image actually boots — the one module tree that
#      contains a vmlinuz. Images legitimately carry extra module directories
#      with no kernel in them, so "how many trees exist" is not the question.
#   2. the installed kernel-core package agrees with that tree (catches skew)
#   3. a zfs kmod object exists under that kernel's tree
#   4. depmod registered zfs, so modprobe would resolve it at boot
#   5. the zfs userland is installed and its version agrees with the kmod
#
# Usage:
#   tools/scripts/smoke-test-zfs.sh <image-ref>
#   tools/scripts/smoke-test-zfs.sh localhost/server4home:stable
#
# Only the base image needs testing — the K3s flavor is built FROM it and
# inherits the same kernel and modules.

set -euo pipefail

IMAGE="${1:?usage: $0 <image-ref>}"
RUNTIME="${CONTAINER_RUNTIME:-podman}"

echo "==> ZFS smoke test: ${IMAGE}"

# Script is piped over stdin (bash -s) so nothing needs shell-escaping here.
"${RUNTIME}" run --rm -i --entrypoint /bin/bash "${IMAGE}" -s <<'INCONTAINER'
set -euo pipefail
shopt -s nullglob

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. the kernel this image boots = the module tree that ships a vmlinuz.
#    Extra module directories without a kernel are normal and irrelevant.
mapfile -t booted < <(find /usr/lib/modules -mindepth 2 -maxdepth 2 -name vmlinuz -printf '%h\n' 2>/dev/null)
[ "${#booted[@]}" -ne 0 ] || fail "no /usr/lib/modules/*/vmlinuz — image ships no bootable kernel"
[ "${#booted[@]}" -eq 1 ] || fail "expected 1 bootable kernel, found ${#booted[@]}: ${booted[*]}"
kver="$(basename "${booted[0]}")"
echo "  boot kernel:  ${kver}"

# 2. the installed kernel-core package must match that tree
pkg_kver="$(rpm -q --qf '%{VERSION}-%{RELEASE}.%{ARCH}' kernel-core 2>/dev/null || true)"
[ -n "${pkg_kver}" ] || fail "kernel-core package not installed"
[ "${pkg_kver}" = "${kver}" ] || fail "kernel skew — kernel-core is ${pkg_kver} but the bootable tree is ${kver}"
echo "  kernel-core:  ${pkg_kver}"

# 3. a zfs kmod object exists for that kernel (may be .ko, .ko.xz or .ko.zst)
mapfile -t zfs_ko < <(find "/usr/lib/modules/${kver}" -name 'zfs.ko*' -print 2>/dev/null)
[ "${#zfs_ko[@]}" -gt 0 ] || fail "no zfs.ko* under /usr/lib/modules/${kver} — akmods produced no module for the kernel this image boots"
echo "  zfs module:   ${zfs_ko[0]}"

# 4. depmod registered it, so modprobe resolves at boot
dep="/usr/lib/modules/${kver}/modules.dep"
[ -f "${dep}" ] || fail "missing ${dep}"
grep -q 'zfs\.ko' "${dep}" || fail "zfs absent from ${dep} — depmod did not register it; modprobe would fail at boot"
echo "  modules.dep:  zfs registered"

# 5. userland present and version-consistent with the kmod
user_ver="$(rpm -q --qf '%{VERSION}' zfs 2>/dev/null || true)"
[ -n "${user_ver}" ] || fail "zfs userland package not installed"
command -v zpool >/dev/null 2>&1 || fail "zpool binary missing"
kmod_ver="$(modinfo -F version "${zfs_ko[0]}" 2>/dev/null || true)"
echo "  zfs userland: ${user_ver}"
echo "  zfs kmod:     ${kmod_ver:-<modinfo unavailable>}"
if [ -n "${kmod_ver}" ] && [ "${kmod_ver%%-*}" != "${user_ver}" ]; then
  fail "version mismatch — userland ${user_ver} vs kmod ${kmod_ver}"
fi

echo "PASS: ZFS ${user_ver} present and consistent for kernel ${kver}"
INCONTAINER
