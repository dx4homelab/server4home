#!/usr/bin/env bash
# Verify a built image carries an NVIDIA kmod that matches its own kernel, and
# that it is the OPEN kernel module flavor.
#
# What this catches
# -----------------
# Same failure class as smoke-test-zfs.sh: uCore's nvidia.ko is built
# out-of-tree by ublue-os/akmods against one specific kernel. A kernel bump
# ahead of the akmods build still produces a green image, and the breakage only
# appears when the GPU box reboots and nvidia-smi finds no driver.
#
# The open-module check matters for Blackwell (RTX 50xx), which the proprietary
# module does not support at all: the proprietary nvidia.ko reports
# license "NVIDIA", the open one "Dual MIT/GPL".
#
# Checks (static — the runner kernel is not the image kernel):
#   1. exactly one bootable kernel tree (the one with a vmlinuz)
#   2. nvidia.ko* exists under that tree and depmod registered it
#   3. the module is the open flavor
#   4. userland (nvidia-smi) and nvidia-container-toolkit (nvidia-ctk) exist,
#      and the kmod and driver package versions agree
#
# Usage:
#   tools/scripts/smoke-test-nvidia.sh localhost/server4home-nvidia:stable

set -euo pipefail

IMAGE="${1:?usage: $0 <image-ref>}"
RUNTIME="${CONTAINER_RUNTIME:-podman}"

echo "==> NVIDIA smoke test: ${IMAGE}"

"${RUNTIME}" run --rm -i --entrypoint /bin/bash "${IMAGE}" -s <<'INCONTAINER'
set -euo pipefail
shopt -s nullglob

fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. the kernel this image boots
mapfile -t booted < <(find /usr/lib/modules -mindepth 2 -maxdepth 2 -name vmlinuz -printf '%h\n' 2>/dev/null)
[ "${#booted[@]}" -eq 1 ] || fail "expected 1 bootable kernel, found ${#booted[@]}: ${booted[*]}"
kver="$(basename "${booted[0]}")"
echo "  boot kernel:  ${kver}"

# 2. nvidia kmod for that kernel, registered with depmod
mapfile -t ko < <(find "/usr/lib/modules/${kver}" -name 'nvidia.ko*' -print 2>/dev/null)
[ "${#ko[@]}" -gt 0 ] || fail "no nvidia.ko* under /usr/lib/modules/${kver}"
echo "  nvidia kmod:  ${ko[0]}"
grep -qE '(^|/)nvidia\.ko' "/usr/lib/modules/${kver}/modules.dep" ||
  fail "nvidia absent from modules.dep — modprobe would fail at boot"

# 3. open kernel module flavor
license="$(modinfo -F license "${ko[0]}")"
echo "  license:      ${license}"
[ "${license}" = "Dual MIT/GPL" ] ||
  fail "nvidia.ko is not the open kernel module (license '${license}'); Blackwell needs the open module"

# 4. userland + container toolkit, versions consistent
command -v nvidia-smi >/dev/null 2>&1 || fail "nvidia-smi missing"
command -v nvidia-ctk >/dev/null 2>&1 || fail "nvidia-ctk (nvidia-container-toolkit) missing"
kmod_ver="$(modinfo -F version "${ko[0]}")"
drv_ver="$(rpm -q --qf '%{VERSION}' nvidia-driver-cuda 2>/dev/null || true)"
echo "  kmod version: ${kmod_ver}"
echo "  driver pkg:   ${drv_ver:-<nvidia-driver-cuda not installed>}"
[ -z "${drv_ver}" ] || [ "${drv_ver}" = "${kmod_ver}" ] ||
  fail "version mismatch — driver userland ${drv_ver} vs kmod ${kmod_ver}"

echo "PASS: NVIDIA open module ${kmod_ver} present and consistent for kernel ${kver}"
INCONTAINER
