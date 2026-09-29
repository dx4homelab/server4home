#!/usr/bin/env bash
# Verify `systemctl preset-all` does not change which units the image enables.
#
# What this catches
# -----------------
# Anaconda (every ISO install) runs `systemctl preset-all`, which discards the
# enablement baked into the image and re-derives it from preset files. When the
# presets disagree with the image, an ISO-installed host silently differs from
# the image — the first ProArt install came up with zincati restart-looping and
# podman.socket off. build/files/usr/lib/systemd/system-preset/10-server4home.preset
# restates the image's choices; this test fails when a base-image update adds a
# unit whose preset disagrees, so the preset file is fixed before hardware is.
#
# Usage:
#   tools/scripts/smoke-test-presets.sh localhost/server4home:stable

set -euo pipefail

IMAGE="${1:?usage: $0 <image-ref>}"
RUNTIME="${CONTAINER_RUNTIME:-podman}"

echo "==> preset drift test: ${IMAGE}"

"${RUNTIME}" run --rm -i --entrypoint /bin/bash "${IMAGE}" -s <<'INCONTAINER'
set -euo pipefail

enabled() { systemctl list-unit-files --state=enabled --no-legend | awk '{print $1}' | sort; }

before="$(enabled)"
systemctl preset-all >/dev/null 2>&1 || true
after="$(enabled)"

added="$(comm -13 <(echo "${before}") <(echo "${after}"))"
removed="$(comm -23 <(echo "${before}") <(echo "${after}"))"

if [ -n "${added}${removed}" ]; then
  [ -z "${added}" ] || printf '  preset-all would ENABLE:  %s\n' ${added}
  [ -z "${removed}" ] || printf '  preset-all would DISABLE: %s\n' ${removed}
  echo "FAIL: ISO installs would diverge from the image — add these to 10-server4home.preset" >&2
  exit 1
fi
echo "PASS: preset-all leaves unit enablement unchanged ($(echo "${before}" | wc -l) enabled units)"
INCONTAINER
