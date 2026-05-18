"""Proxmox PVE 9 target (stubbed).

The bash helper at `helpers/proxmox/create-rancher-vm.sh` works today as a
manual path; this plugin will eventually shell out to it (or to the Proxmox
REST API) once the manifest-driven flow has settled. For now, calling it
raises a clear error so we don't pretend.
"""

from __future__ import annotations

from ..manifest import Manifest
from ..registry import targets
from .base import CreateResult, Target


@targets.register("pve9")
class Pve9(Target):
    def create(self, manifest: Manifest) -> CreateResult:
        raise NotImplementedError(
            "target='pve9' is not implemented in this iteration. "
            "Use helpers/proxmox/create-rancher-vm.sh manually, "
            "or switch the manifest to target='local-virt-manager' for now."
        )

    def discover_ip(self, manifest: Manifest, mac: str | None) -> str:
        raise NotImplementedError
