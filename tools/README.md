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

| Tool          | Why                                                       |
| ---           | ---                                                       |
| `python3`     | ≥ 3.10 — to run the runner                                |
| `virt-install`, `virsh`, `qemu-img` | for `target: local-virt-manager` |
| `helm`, `kubectl` | applied against the new cluster from the workstation |
| `ssh`/`scp`   | to drop K3s config and fetch kubeconfig                   |
| `yq`          | NOT required (we use the Python `pyyaml` library)         |

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

## Plugin architecture

Four extension points, each backed by a `Registry` in
[`server4home/registry.py`](server4home/registry.py):

| Registry          | Lives in                              | Built-ins                             |
| ---               | ---                                   | ---                                   |
| `targets`         | `server4home/targets/`                | `local-virt-manager`, `pve9` (stub)   |
| `mac_provisioners`| `server4home/provisioners/mac.py`     | `default`, `fixed`, `ifra` (stub)     |
| `ip_provisioners` | `server4home/provisioners/ip.py`      | `dhcp`, `static`                      |
| `installers`      | `server4home/installers/`             | `k3s`, `cert-manager`, `rancher-manager` |

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

| Variable          | Used by                          | Default                       |
| ---               | ---                              | ---                           |
| `LIBVIRT_BRIDGE`  | `local-virt-manager` target      | `br0`                         |
| `LIBVIRT_POOL`    | `local-virt-manager` target      | `/var/lib/libvirt/images`     |
| `QCOW2_SRC`       | `local-virt-manager` target      | `output/qcow2/disk.qcow2`     |
| `SSH_USER`        | runner                           | `developer`                   |
| `SSH_KEY`         | runner                           | `~/.ssh/id_ed25519`           |

## Limitations (today)

- `disks:` only honors a `/var/lib/rancher` entry with `type: lvm`. Other
  paths are accepted but skipped with a warning.
- Only `network[0]` is wired (single NIC).
- `target: pve9` raises NotImplementedError — Proxmox provisioning still goes
  through `helpers/proxmox/create-rancher-vm.sh` for now.
- `mac.provisioner: ifra` raises NotImplementedError — lands with the IFRA
  inventory service.
