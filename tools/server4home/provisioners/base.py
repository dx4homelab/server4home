"""Provisioner plugin interfaces.

A MAC provisioner resolves a MAC address (or `None` to let the hypervisor
choose). An IP provisioner produces SMBIOS OEM string fragments that the
target appends to its `--sysinfo` argument; the VM's first-boot
network-static.sh consumes them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..manifest import IpSpec, MacSpec, Manifest


class MacProvisioner(ABC):
    """Decides what MAC the VM should be created with."""

    @abstractmethod
    def resolve(self, manifest: Manifest, spec: MacSpec) -> str | None:
        """Return a MAC string, or None to let the hypervisor pick one."""


@dataclass
class IpResult:
    """OEM-string fragments produced by an IP provisioner.

    The target wires these into virt-install (or qm) verbatim, prefixed with a
    comma. A DHCP-by-default provisioner returns an empty list and the target
    skips the entire static-IP path.
    """

    oem_strings: list[str]    # e.g. ["server4home-static-ip=...", "server4home-static-gw=..."]


class IpProvisioner(ABC):
    @abstractmethod
    def resolve(self, manifest: Manifest, spec: IpSpec) -> IpResult:
        ...
