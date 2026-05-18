"""MAC address provisioners.

Three plugins ship in v1:
  - `default`: let the hypervisor pick.
  - `fixed`:   use the literal MAC from `mac.fixed`.
  - `ifra`:   reserve via the IFRA inventory API. Stubbed; raises until the
              service exists. The post-boot ifra-register.sh continues to
              register the hypervisor-assigned MAC in the meantime.
"""

from __future__ import annotations

from ..manifest import MacSpec, Manifest
from ..registry import mac_provisioners
from .base import MacProvisioner


@mac_provisioners.register("default")
class DefaultMac(MacProvisioner):
    """Let the hypervisor choose. Returns None to signal `no preference`."""

    def resolve(self, manifest: Manifest, spec: MacSpec) -> str | None:
        return None


@mac_provisioners.register("fixed")
class FixedMac(MacProvisioner):
    """Use the literal MAC from the manifest."""

    def resolve(self, manifest: Manifest, spec: MacSpec) -> str | None:
        assert spec.fixed, "schema validation should have caught missing mac.fixed"
        return spec.fixed


@mac_provisioners.register("ifra")
class IfraMac(MacProvisioner):
    """Reserve a MAC from the IFRA inventory before VM create.

    Not implemented yet. Update this class once the IFRA HTTP service exists;
    no other code changes are needed.
    """

    def resolve(self, manifest: Manifest, spec: MacSpec) -> str | None:
        raise NotImplementedError(
            "mac.provisioner='ifra' is not yet implemented. "
            "Use 'default' or 'fixed' for now; IFRA support will land "
            "alongside the inventory service."
        )
