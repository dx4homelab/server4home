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


class Target(ABC):
    """Plugin contract for a deployment target."""

    @abstractmethod
    def create(self, manifest: Manifest) -> CreateResult:
        """Provision the VM. Idempotent re-create allowed."""

    @abstractmethod
    def discover_ip(self, manifest: Manifest, mac: str | None) -> str:
        """Find the running VM's IP. Called after `create()` returns."""

    def destroy(self, manifest: Manifest) -> None:
        """Optional cleanup. Default: not implemented."""
        raise NotImplementedError(f"{type(self).__name__} does not implement destroy()")
