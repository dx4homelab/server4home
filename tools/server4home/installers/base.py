"""Installer plugin interface.

An Installer applies one entry from the manifest's `install:` list. Different
installers may need different context (some need SSH access to the VM, some
need the cluster's kubeconfig); both are made available so individual plugins
pick what they need.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..manifest import InstallSpec, Manifest
from ..util import SSH


@dataclass
class InstallContext:
    """Runtime context handed to every installer.

    The orchestrator constructs one InstallContext per deploy and reuses it
    across all install entries.
    """

    manifest: Manifest
    ssh: SSH
    kubeconfig: Path | None = None    # set after K3s comes up


class Installer(ABC):
    @abstractmethod
    def apply(self, ctx: InstallContext, entry: InstallSpec) -> None:
        ...

    def requires_kubeconfig(self) -> bool:
        """Override to True for installers that need cluster API access."""
        return True

    def requires_fresh_node(self) -> bool:
        """Override to True for installers that must run on a freshly-created
        VM (not against an already-up cluster).

        `server4home apply <manifest>` skips installers that return True
        here — running them on a live node would either be a no-op or
        actively wrong. The k3s installer is the canonical case: it lives
        in the image, not in helm, and rewriting first-boot config on a
        running node is not what reconciliation should do.
        """
        return False
