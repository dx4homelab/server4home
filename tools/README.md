# server4home runner

A Python CLI that turns an instance YAML manifest (`instances/<name>.yaml`)
into a running, configured server4home VM with K3s and optionally a stack of
helm-installed apps on top (Rancher Manager, cert-manager, …).

## Quickstart

```bash
# From the repo root:
just deploy instances/k3s-on-virt-manager.yaml
```

The Justfile bootstraps a project virtualenv at `./.venv/` on first use and
installs this package in editable mode (`pip install -e tools/`). Subsequent
runs reuse the venv.

If you prefer to drive the CLI directly:

```bash
. .venv/bin/activate
server4home --help
server4home validate instances/k3s-on-virt-manager.yaml
server4home deploy   instances/k3s-on-virt-manager.yaml
server4home destroy  instances/k3s-on-virt-manager.yaml
server4home list-plugins
```

## Workstation prerequisites

| Tool                                | Why                                                  |
| ----------------------------------- | ---------------------------------------------------- |
| `python3`                           | ≥ 3.10 — to run the runner                           |
| `virt-install`, `virsh`, `qemu-img` | for `target: local-virt-manager`                     |
| `helm`, `kubectl`                   | applied against the new cluster from the workstation |
| `ssh`/`scp`                         | to drop K3s config and fetch kubeconfig              |
| `yq`                                | NOT required (we use the Python `pyyaml` library)    |

## What the manifest looks like

See [`instances/k3s-on-virt-manager.yaml`](../instances/k3s-on-virt-manager.yaml)
for the canonical example.

```yaml
base: k3s-base
hostname: rancher-cp-01
target: local-virt-manager

resources:
  memory: 16384
  vcpus: 4

disks:
  - path: /var/lib/rancher
    size: 100G
    type: lvm

network:
  - name: default
    type: bridge
    mac:
      provisioner: default        # default | fixed | ifra
    ip:
      provisioner: dhcp           # dhcp | static
      # static: 192.168.120.50/16
      # gateway: 192.168.1.1
      # dns: 192.168.1.1

install:
  - name: k3s
    args:
      - --disable=traefik
      - --disable=servicelb

  - name: rancher-manager
    version: v2.14.1
    config:
      hostname: rancher.example.lan
      replicas: 1
      bootstrapPassword: admin
```

### Joining an existing cluster

To make the VM an **agent** that joins an existing K3s/Rancher cluster
instead of starting its own, give the `k3s` install entry a `config:` block.
No `rancher-manager` entry — Rancher already runs on the cluster:

```yaml
install:
  - name: k3s
    config:
      mode: agent
      server: https://k3s-cp-01.lan:6443
      token: { secret: "k3s/homelab/agent-token" }
```

The runner resolves the token, injects mode/server/token as SMBIOS OEM
strings, and the VM's first-boot `k3s-config.sh` writes
`/etc/server4home/k3s.conf` before `k3s.service` starts — so the node comes
up already joined. Agent deploys skip the kubeconfig fetch and any
kubeconfig-dependent installers (there's no local cluster API on an agent).

## Secrets

Manifests never carry secret values. A secret is referenced by name:

```yaml
install:
  - name: k3s
    config:
      mode: agent
      server: https://k3s-cp-01.lan:6443
      token: { secret: "k3s/homelab/agent-token" }   # <-- reference, not value
```

At deploy time the runner resolves every `{ secret: <name> }` reference via a
**secret provider** plugin (default: `local`). The `local` provider reads a
flat `name: value` map from `secrets/secrets.yaml` — which is gitignored.
Copy the committed template to start:

```bash
cp secrets/secrets.example.yaml secrets/secrets.yaml
$EDITOR secrets/secrets.yaml      # fill in real values
```

Override the store path with `$S4H_SECRETS_FILE`, or the provider with the
manifest's top-level `secret_provider:` key. A future `ifra` provider will
fetch from the homelab inventory API — the manifest stays identical.

### Pre-staging Kubernetes secrets (TLS, registry creds, …)

The `kubernetes-secret` installer creates a Kubernetes Secret on the cluster
before any chart that needs it runs. Combined with `{ secret: ... }`
references in the manifest, it's the path to automated TLS ingress for
Rancher (or anything else) using your homelab CA:

```yaml
install:
  - name: kubernetes-secret
    config:
      name: tls-rancher-ingress
      namespace: cattle-system
      type: tls                                    # → kubernetes.io/tls
      data:
        tls.crt: { secret: "tls/rancher.crt" }
        tls.key: { secret: "tls/rancher.key" }

  - name: kubernetes-secret                        # Rancher's privateCA
    config:
      name: tls-ca
      namespace: cattle-system
      type: opaque
      data:
        cacerts.pem: { secret: "tls/ca.crt" }

  - name: rancher-manager
    version: v2.14.1
    config:
      hostname: rancher.lan.example.com
      ingress:
        tls:
          source: secret                           # uses tls-rancher-ingress
      privateCA: true                              # uses tls-ca
```

Installers run in manifest order, so the secrets exist before the Helm chart
applies. The installer creates the namespace idempotently first, so this
works even before `helm install --create-namespace` runs.

The PEM contents come from the local secret store. For multi-line values,
use a YAML literal block in `secrets/secrets.yaml`:

```yaml
"tls/rancher.crt": |
  -----BEGIN CERTIFICATE-----
  MIID…
  -----END CERTIFICATE-----
"tls/rancher.key": |
  -----BEGIN PRIVATE KEY-----
  MIIE…
  -----END PRIVATE KEY-----
```

Accepted `type` values: `opaque`, `tls`, `basic-auth`, `ssh-auth`,
`dockerconfigjson`, or any full `kubernetes.io/*` type string verbatim.

## Plugin architecture

Five extension points, each backed by a `Registry` in
[`server4home/registry.py`](server4home/registry.py):

| Registry           | Lives in                          | Built-ins                                                     |
| ------------------ | --------------------------------- | ------------------------------------------------------------- |
| `targets`          | `server4home/targets/`            | `local-virt-manager`, `pve9` (stub)                           |
| `mac_provisioners` | `server4home/provisioners/mac.py` | `default`, `fixed`, `ifra` (stub)                             |
| `ip_provisioners`  | `server4home/provisioners/ip.py`  | `dhcp`, `static`                                              |
| `installers`       | `server4home/installers/`         | `k3s`, `cert-manager`, `rancher-manager`, `kubernetes-secret` |
| `secret_providers` | `server4home/secrets/`            | `local`                                                       |

### Adding a new plugin

1. Pick the right subpackage (`targets/`, `provisioners/`, `installers/`).
2. Write a class that subclasses the appropriate ABC (`Target`,
   `MacProvisioner`, `IpProvisioner`, `Installer`).
3. Decorate it: `@<registry>.register("<key>")`.
4. Import your module from that subpackage's `__init__.py` so the decorator
   runs at package import time.
5. Reference your plugin from a manifest by its key.

Example — a new installer that helm-installs Longhorn:

```python
# server4home/installers/longhorn.py
from ..registry import installers
from ..util import Helm
from .base import InstallContext, Installer

@installers.register("longhorn")
class LonghornInstaller(Installer):
    def apply(self, ctx, entry):
        helm = Helm(ctx.kubeconfig)
        helm.repo_add("longhorn", "https://charts.longhorn.io")
        helm.repo_update("longhorn")
        helm.upgrade_install(
            release="longhorn", chart="longhorn/longhorn",
            namespace="longhorn-system",
            version=entry.version,
        )
```

Then add `from . import longhorn` to `server4home/installers/__init__.py`.
The manifest can now contain:

```yaml
install:
  - name: longhorn
    version: 1.7.2
```

## Environment overrides

| Variable         | Used by                     | Default                   |
| ---------------- | --------------------------- | ------------------------- |
| `LIBVIRT_BRIDGE` | `local-virt-manager` target | `br0`                     |
| `LIBVIRT_POOL`   | `local-virt-manager` target | `/var/lib/libvirt/images` |
| `QCOW2_SRC`      | `local-virt-manager` target | `output/qcow2/disk.qcow2` |
| `SSH_USER`       | runner                      | `developer`               |
| `SSH_KEY`        | runner                      | `~/.ssh/id_ed25519`       |

## Limitations (today)

- `disks:` only honors a `/var/lib/rancher` entry with `type: lvm`. Other
  paths are accepted but skipped with a warning.
- Only `network[0]` is wired (single NIC).
- `target: pve9` raises NotImplementedError — Proxmox provisioning still goes
  through `helpers/proxmox/create-rancher-vm.sh` for now.
- `mac.provisioner: ifra` raises NotImplementedError — lands with the IFRA
  inventory service.
