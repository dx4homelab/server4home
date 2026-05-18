"""cert-manager installer (Helm).

Standalone — usable from a manifest as an explicit `install:` entry — but
also auto-invoked by the rancher-manager installer since Rancher depends on
it.
"""

from __future__ import annotations

from ..manifest import InstallSpec, Manifest
from ..registry import installers
from ..util import Helm, log
from .base import InstallContext, Installer

DEFAULT_VERSION = "v1.18.6"
REPO_NAME = "jetstack"
REPO_URL = "https://charts.jetstack.io"
CHART = "jetstack/cert-manager"


@installers.register("cert-manager")
class CertManagerInstaller(Installer):
    def apply(self, ctx: InstallContext, entry: InstallSpec) -> None:
        assert ctx.kubeconfig is not None
        helm = Helm(ctx.kubeconfig)
        version = entry.version or DEFAULT_VERSION
        log.info("Installing cert-manager %s", version)
        helm.repo_add(REPO_NAME, REPO_URL)
        helm.repo_update(REPO_NAME)
        helm.upgrade_install(
            release="cert-manager",
            chart=CHART,
            namespace="cert-manager",
            version=version,
            set_flags={"installCRDs": "true"},
            timeout="10m",
        )
