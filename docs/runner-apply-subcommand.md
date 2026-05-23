# Add `server4home apply <manifest>` — installer-only path against an existing VM

**Status:** must-have backlog item. Land before the next Rancher minor bump
so that upgrades don't need hand-typed `helm upgrade` invocations.

The runner today is **create-only**: [`deploy_cmd`](../tools/server4home/cli.py)
always calls `target.create()`, then runs every installer. There's no way to
say "the VM already exists, just reconcile the helm charts." That gap is
fine while everything is greenfield, but it forces operators to:

- run `helm upgrade --install` by hand for [rancher](../tools/server4home/installers/rancher_manager.py) and [metallb](../tools/server4home/installers/metallb.py) — duplicating logic the installers already encode (chart name, repo URL, `--reuse-values`, namespace),
- keep the manifest's `version:` field truthful out of band, and
- carry the kubeconfig path / context in shell rather than letting the runner pick it up.

`apply` closes the gap. See [k3s-rancher-vms.md §upgrades](k3s-rancher-vms.md) for the operator-side
workflow this enables.

---

## Proposed CLI surface

```bash
server4home apply <manifest>                  # run all installers against the existing cluster
server4home apply <manifest> --only rancher-manager,metallb
server4home apply <manifest> --skip k3s       # never reconcile the bootc-managed installer
server4home apply <manifest> --kubeconfig <path>   # override; default = kubeconfigs/<hostname>.kubeconfig
server4home apply <manifest> --dry-run        # print helm commands without running
```

Just recipe sibling to `deploy`:

```just
# Reconcile installers against an existing VM (no VM (re)create).
# Use when bumping rancher-manager / metallb / cert-manager chart versions.
[group('Deploy')]
apply manifest: _python-env
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/server4home apply {{ manifest }}
```

---

## What `apply` does, step by step

1. Load + validate the manifest (same as `deploy`).
2. Resolve `{ secret: ... }` references (same as `deploy`).
3. **Skip `target.create()`** and **skip `target.discover_ip()` if a kubeconfig already exists** —
   `apply` is a cluster-side operation, not a VM-side one. Only fall back to
   SSH/IP discovery if a pre-kubeconfig installer (e.g. a fresh `kubernetes-secret`
   pre-stage on a new namespace) needs SSH access to the node — which today none do.
4. Resolve the kubeconfig path: `--kubeconfig` flag wins, then `kubeconfigs/<hostname>.kubeconfig`,
   then error with a hint to run `deploy` first.
5. Run every installer's `apply(ctx, entry)` in manifest order. The installers
   are already idempotent via `helm upgrade --install` — no change needed there.
6. Emit a deployment-history event tagged `kind: "apply"` (vs `kind: "deploy"`),
   so the JSON ledger distinguishes greenfield from reconciliation.

---

## Why we skip `k3s` by default in `apply`

K3s lives in the image, not in helm. Reconciling it via `apply` would mean
either:

- a no-op (the installer would see `k3s.service` already active and exit), or
- a regression (the installer rewrites `/etc/server4home/k3s.conf` on a
  running node, which is wrong — that file is first-boot config).

The K3s installer should grow a `requires_fresh_node()` predicate (analogous
to today's `requires_kubeconfig()`) that `apply` reads to decide whether to
run it. Default: `True` (skip on `apply`). `--only k3s` can still force it
for explicit reconfigurations, with a warning.

---

## What this does NOT do

`apply` is **not** a generic "deploy minus create." It explicitly avoids:

- Running the VM provisioner. If you need a new VM, that's `deploy`.
- Reconciling SMBIOS-injected first-boot config. Those land via image
  rebuilds + `bootc upgrade`, not via the runner.
- Per-installer rollback. `helm rollback` is the right tool; documented in
  [k3s-rancher-vms.md](k3s-rancher-vms.md) rather than wrapped.

---

## Acceptance criteria

A future Rancher 2.14.1 → 2.15.x upgrade is driven entirely by:

```bash
# 1) Edit manifest version
$EDITOR instances/k3s-rancher-on-ucore-pve-vm.yaml   # version: v2.15.0
# 2) One command. Done.
just apply instances/k3s-rancher-on-ucore-pve-vm.yaml
# 3) Commit the manifest + the new deployment-history JSON
git add instances/ deployments/ docs/deployment-history.md
git commit -m "rancher: bump to v2.15.0"
```

No raw `helm` invocations from the operator. No kubeconfig path passed by
hand. The history ledger records the bump.

---

## Implementation sketch (so the next session is a copy-paste)

Touch points:

- `tools/server4home/cli.py` — add `apply_cmd`, mirrors `deploy_cmd`'s args.
- `tools/server4home/runner.py` — extract a private `_apply_installers(manifest, kubeconfig, *, only, skip)` helper that today's `deploy()` calls in its installer loop. Both `deploy` and `apply` route through it.
- `tools/server4home/installers/base.py` — add `requires_fresh_node()` defaulting to `False`. K3s overrides to `True`.
- `tools/server4home/installers/k3s.py` — keep the body unchanged; `apply` is the one that skips it via the new predicate.
- `Justfile` — `apply` recipe alongside `deploy`.
- `docs/k3s-rancher-vms.md` — replace the "manual helm upgrade" snippet in the upgrades section with `just apply <manifest>`.

Tests worth writing alongside:

- `apply` against a missing kubeconfig errors with the deploy hint.
- `apply --only rancher-manager` calls only `rancher_manager.apply()`.
- `apply` with `--dry-run` produces the helm command lines that `deploy`
  would have run, byte-for-byte.

---

## When to land this

The first time you bump a helm-chart `version:` in a manifest. Whichever
comes first: Rancher minor, MetalLB 0.16.x once the chart bug clears,
cert-manager when it shows up.
