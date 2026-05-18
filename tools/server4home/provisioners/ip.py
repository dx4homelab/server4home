"""IP address provisioners.

Two plugins ship in v1:
  - `dhcp`:   default. Returns an empty IpResult; the target skips SMBIOS
              OEM-string injection and the VM uses NetworkManager's DHCP.
  - `static`: emits OEM-string fragments consumed by the VM image's
              first-boot `network-static.sh` to write a NM keyfile before
              NetworkManager starts.
"""

from __future__ import annotations

from ..manifest import IpSpec, Manifest
from ..registry import ip_provisioners
from .base import IpProvisioner, IpResult


@ip_provisioners.register("dhcp")
class DhcpIp(IpProvisioner):
    def resolve(self, manifest: Manifest, spec: IpSpec) -> IpResult:
        return IpResult(oem_strings=[])


@ip_provisioners.register("static")
class StaticIp(IpProvisioner):
    def resolve(self, manifest: Manifest, spec: IpSpec) -> IpResult:
        assert spec.static, "schema validation should have caught missing ip.static"
        out: list[str] = [f"server4home-static-ip={spec.static}"]
        if spec.gateway:
            out.append(f"server4home-static-gw={spec.gateway}")
        if spec.dns:
            # The guest script accepts both pipe- and comma-separated; normalize
            # to pipe so we don't collide with virt-install's --sysinfo CSV.
            normalized = spec.dns.replace(",", "|")
            out.append(f"server4home-static-dns={normalized}")
        return IpResult(oem_strings=out)
