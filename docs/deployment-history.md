# Deployment history

> Auto-generated from `deployments/*.json` — **do not edit by hand**.
> Regenerate with `just history` (or `server4home history render`).

## Latest per VM

| Hostname | Target | Last event | Outcome | K3s | Rancher | MetalLB |
|----------|--------|------------|---------|-----|---------|---------|
| k3s-rancher-on-ucore-pve-vm | pve9 | deploy @ 2026-05-23 22:34 UTC | ✅ success | v1.35.4+k3s1 | v2.14.1 | 0.15.3 |

## k3s-rancher-on-ucore-pve-vm

Target: `pve9` · K3s mode: `server`

| When | Kind | Outcome | K3s | MetalLB | Rancher | Manifest | Notes |
|------|------|---------|-----|---------|---------|----------|-------|
| 2026-05-23 22:34 UTC | deploy | ✅ success | v1.35.4+k3s1 | 0.15.3 | v2.14.1 | `78c5834` | manifest dirty at deploy |
| 2026-05-23 22:07 UTC | deploy | ❌ failure @ runner | v1.35.4+k3s1 | — | — | `78c5834` | RuntimeError: Proxmox API token not found. Add to secrets/secrets.yaml: |
