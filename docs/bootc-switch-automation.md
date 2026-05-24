# Automate the `localhost/` → `ghcr.io/...` bootc switch on first boot

**Status (2026-05-24): landed.** This document is kept as the design record;
the implementation matches it. The pieces:

- [build/k3s/files/usr/libexec/server4home/bootc-switch.sh](../build/k3s/files/usr/libexec/server4home/bootc-switch.sh) — the first-boot worker
- [build/k3s/files/usr/lib/systemd/system/server4home-bootc-switch.service](../build/k3s/files/usr/lib/systemd/system/server4home-bootc-switch.service) — oneshot, before k3s.service
- `server4home-image-ref=<ref>` injected as a SMBIOS OEM string by both the [pve9](../tools/server4home/targets/pve9.py) and [local-virt-manager](../tools/server4home/targets/local_virt_manager.py) targets when `manifest.upgrade2image` is set

**Will take effect on:** the next image build. Today's already-running VMs
were switched manually via the documented `sudo bootc switch ... && sudo
bootc upgrade --apply` one-liner; that step now happens automatically on
fresh VMs after a rebuild.

A freshly-deployed VM boots the local qcow2 image and reports its
container ref as `localhost/server4home-k3s:stable`. The baked-in
`bootc-fetch-apply-updates.timer` ticks every ~8h, looks up that ref,
finds nothing (the local registry doesn't exist at runtime), and is
effectively a no-op. The VM is pinned to whatever qcow2 it was built
from, missing every published security/feature update.

Today's workaround is a manual one-liner after first boot:

```bash
ssh developer@<vm-ip> \
  "sudo bootc switch ghcr.io/dx4homelab/server4home-k3s:stable && \
   sudo bootc upgrade --apply"
```

It works but it's easy to forget — and the VM that's pinned to the build
qcow2 won't tell you it's stale, it'll just silently miss updates. This
doc captures the design for moving that step inside the image itself, so
new VMs land on the GHCR-published image automatically.

---

## Proposed design: SMBIOS-driven first-boot switch

A new first-boot oneshot, `server4home-bootc-switch.service`, alongside
the existing `set-hostname` / `network-static` / `k3s-config` services:

1. Reads target image ref from SMBIOS OEM string
   `server4home-image-ref=ghcr.io/dx4homelab/server4home-k3s:stable`
   (set by the runner at VM creation time — same layer as hostname / static IP).
2. Reads the current booted ref from `bootc status --json`.
3. If equal → exit 0 (idempotent re-boot, image already on GHCR).
4. If different → `bootc switch <ref>` + `bootc upgrade --apply`. The
   `--apply` triggers a reboot into the new image.
5. Writes `/etc/server4home/bootc-switched` so the unit only runs once
   per image ref (a future ref change re-triggers).
6. Ordering: `After=network-online.target`, `Before=k3s.service` — the
   switch happens before K3s starts, so when K3s comes up it's already
   on the GHCR image. The deploy window gains one reboot (~30s) but the
   VM ends in a consistent state.

The defaulting layer mirrors the existing `Manifest.image_ref()` helper:

| `base:` (manifest) | Default SMBIOS image ref                        |
|--------------------|-------------------------------------------------|
| `k3s-base`         | `ghcr.io/dx4homelab/server4home-k3s:stable`     |

Override at the manifest level for pinned digests or alternate registries:

```yaml
# Switch to this image on next boot.
upgrade2image: ghcr.io/dx4homelab/server4home-k3s@sha256:abc...
```

The opt-out for local-image testing is "don't set the field" (or set it
to a `localhost/...` value, which the unit short-circuits — no point
switching to yourself). Keeping the field a single string rather than a
nested `{ ref, apply_on_first_boot }` block keeps the manifest line as
readable as the manual `bootc switch` command it replaces.

---

## Why SMBIOS instead of a static path in the image

The image is built once and serves many VMs. Hardcoding
`ghcr.io/dx4homelab/...` in the unit file works for the published image
but breaks the moment someone forks the repo to their own GHCR org —
they'd have to rebuild the image just to change one URL. SMBIOS keeps
the deploy-time decision (which registry?) at the runner layer where it
already lives next to hostname/IP/k3s-mode.

This also dovetails with the [[deployment-history-design]] — the JSON
event ledger captures `image.ref` and `image.digest` post-switch, so the
history file always records the **GHCR digest the VM actually ran**, not
the localhost qcow2 it was provisioned from.

---

## Why not just do it from the runner via SSH

Considered and rejected. The runner already SSHes in for the K3s readiness
poll and the kubeconfig fetch — adding `sudo bootc switch + upgrade
--apply` to that hop would work, but:

- The reboot inside the deploy window means the runner has to re-poll for
  SSH + K3s readiness afterwards. Doable, but it doubles the wait-for-SSH
  loops the runner has to manage.
- The fix only applies to fresh `deploy`s. If a VM is provisioned out-of-
  band (someone hand-imports the qcow2 via the [helper script](../helpers/proxmox/create-rancher-vm.sh)),
  it never gets switched. Baking the switch into the image makes the helper
  script's "After it boots, run `sudo bootc switch`…" footnote unnecessary.
- Decoupling fits the existing pattern — every first-boot config decision
  flows through SMBIOS (`server4home-hostname-exact`, `server4home-static-ip`,
  `K3S_MODE`/`K3S_URL`/`K3S_TOKEN`). One more is the natural extension.

---

## Implementation sketch

Touch points (so the next session is a copy-paste):

- **`build/k3s/files/usr/libexec/server4home/bootc-switch.sh`** — new script.
  Read SMBIOS via `dmidecode -t 11`, compare to `bootc status --json`,
  call `bootc switch` + `bootc upgrade --apply` if needed. Write
  `/etc/server4home/bootc-switched` to mark.
- **`build/k3s/files/usr/lib/systemd/system/server4home-bootc-switch.service`** —
  oneshot, `After=network-online.target`, `Before=k3s.service`,
  `ConditionPathExists=!/etc/server4home/bootc-switched`. Enabled at
  build time via a `/usr/lib/systemd/system/multi-user.target.wants/`
  symlink (the pattern the K3s install.sh already uses).
- **`tools/server4home/manifest.py`** — the `upgrade2image: str | None`
  field is already landed (stub: accepted, not yet consumed). Implementation
  task here is to read it from `manifest.upgrade2image` in the targets and
  inject. A `Manifest.resolved_upgrade2image()` helper can defaults to the
  GHCR ref derived from `base:` (mirroring `Manifest.image_ref()` for the
  qcow2 source).
- **`tools/server4home/targets/pve9.py`** and **`local_virt_manager.py`** —
  inject `server4home-image-ref=<ref>` as a SMBIOS OEM string in
  `_oem_args_for()` / equivalent, alongside the existing hostname/IP
  injections.
- **`docs/k3s-rancher-vms.md`** — replace the manual `bootc switch`
  instruction with a one-line "happens automatically on first boot."
  Keep the manual command in a "if you need to switch registries later"
  callout.

---

## Acceptance criteria

A fresh deploy reports the GHCR image without any manual step:

```bash
just deploy instances/k3s-rancher-on-ucore-pve-vm.yaml
# ... runner finishes, VM is up + K3s ready ...
ssh developer@192.168.130.20 'sudo bootc status' | grep 'Booted image'
# >>> Booted image: ghcr.io/dx4homelab/server4home-k3s:stable
```

And the deployment-history JSON has the correct ref:

```json
"image": {
  "ref": "ghcr.io/dx4homelab/server4home-k3s:stable",
  "digest": "sha256:..."
}
```

`bootc-fetch-apply-updates.timer` ticks every 8h after that and the VM
self-updates as new digests land. No "switch to ghcr.io" line anywhere
in operator-facing docs.

---

## Edge cases worth thinking through before implementation

- **Air-gapped homelabs.** Some users won't have outbound internet from
  their VMs. The unit should fail-soft: if the `bootc switch` errors
  with a network failure, log it, leave the marker file *unwritten*, and
  let `k3s.service` proceed on the local image. Retry on the next boot.
  Don't ever brick the VM over a transient registry hiccup.
- **Cosign verification.** [The CI workflow](../.github/workflows/build.yml)
  already signs every push. Layering on `bootc switch --enforce-container-sigs`
  in the script (or via `/etc/containers/policy.json`) is the natural follow-up
  — bumping signature enforcement is then a one-line image change, not a
  runner change.
- **Rollback path.** If the first GHCR image is broken, the user is now
  one reboot away from a non-bootable VM. Mitigate by leaving the prior
  rollback target intact (bootc does this automatically) and documenting
  `sudo bootc rollback && sudo reboot` in the VM's MOTD or a `--help`
  output the operator can find.
- **`apply_on_first_boot: false` for local image testing.** A developer
  iterating on the image with `just rebuild-vm-k3s` does NOT want their
  qcow2 silently swapped for GHCR on first boot. The opt-out has to land
  alongside the feature, not after.

---

## When to land this

Whichever comes first:

- Second time someone forgets the manual `bootc switch` step and asks
  why their VM isn't picking up image updates.
- First time a published-image security update needs to land on every
  homelab simultaneously and the manual ssh-and-switch is the bottleneck.
