"""Target plugin interface.

A Target is responsible for creating the VM (and tearing it down) on a
specific hypervisor / management surface. It does *not* configure what runs
inside the VM — installers do that after the VM is reachable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..manifest import Manifest


@dataclass
class CreateResult:
    """What the Target returns once it has created a VM."""

    vm_name: str
    # MAC the hypervisor ended up using (may differ from manifest if "default").
    mac: str | None = None
    # IP the runner should expect to find the VM at. None = "discover by other
    # means after boot" (e.g. ARP scan against the bridge for DHCP).
    expected_ip: str | None = None


class IdentityMismatchError(RuntimeError):
    """Raised when a preserved data disk's recorded identity does not match
    the manifest being deployed.

    The expected recovery is for the operator to either fix the manifest
    (use the same hostname the data disk was created for) or to pass
    ``--wipe-data`` to explicitly drop the preserved state.
    """


class Target(ABC):
    """Plugin contract for a deployment target."""

    @abstractmethod
    def create(self, manifest: Manifest, *,
               wipe_data: bool = False) -> CreateResult:
        """Provision the VM. Idempotent re-create allowed.

        When ``wipe_data=True``, the target must drop any preserved per-VM
        data (data disk, sidecars) before creating fresh storage. Default
        ``False`` preserves data across re-deploys.
        """

    @abstractmethod
    def discover_ip(self, manifest: Manifest, mac: str | None) -> str:
        """Find the running VM's IP. Called after `create()` returns."""

    def destroy(self, manifest: Manifest) -> None:
        """Optional cleanup. Default: not implemented."""
        raise NotImplementedError(f"{type(self).__name__} does not implement destroy()")
