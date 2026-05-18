"""Rancher Manager installer (Helm).

Auto-installs cert-manager first if it isn't already present in the cluster
(unless the manifest explicitly disables this via `config.cert-manager-auto:
false`). The manifest's `config:` block is written verbatim as the Helm
values file — anything the upstream chart supports just works.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from ..manifest import InstallSpec, Manifest
from ..registry import installers
from ..util import Helm, log, run
from .base import InstallContext, Installer
from .cert_manager import CertManagerInstaller

DEFAULT_VERSION = "2.14.1"
REPO_NAME = "rancher-stable"
REPO_URL = "https://releases.rancher.com/server-charts/stable"
CHART = "rancher-stable/rancher"


@installers.register("rancher-manager")
class RancherManagerInstaller(Installer):
    def apply(self, ctx: InstallContext, entry: InstallSpec) -> None:
        assert ctx.kubeconfig is not None

        # Resolve version. Rancher's chart uses unprefixed semver, so strip a
        # leading "v" if the manifest carries one.
        version = (entry.version or DEFAULT_VERSION).lstrip("v")

        # Pull out the optional auto-dep toggle before writing the values file,
        # so it isn't accidentally passed to the chart as a real value.
        config = dict(entry.config)
        auto_cert_manager = config.pop("cert-manager-auto", True)
        if auto_cert_manager and not self._cert_manager_installed(ctx.kubeconfig):
            log.info("rancher-manager: cert-manager not present — installing it first")
            CertManagerInstaller().apply(
                ctx,
                InstallSpec(name="cert-manager"),
            )

        # Materialize the values file.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".values.yaml", delete=False, encoding="utf-8",
        ) as fh:
            yaml.safe_dump(config, fh, sort_keys=False)
            values_path = Path(fh.name)
        log.info("Rancher values written to %s", values_path)

        helm = Helm(ctx.kubeconfig)
        helm.repo_add(REPO_NAME, REPO_URL)
        helm.repo_update(REPO_NAME)
        helm.upgrade_install(
            release="rancher",
            chart=CHART,
            namespace="cattle-system",
            version=version,
            values_file=values_path,
            timeout="20m",
        )

    @staticmethod
    def _cert_manager_installed(kubeconfig: Path) -> bool:
        rc = run(
            [
                "kubectl", "--kubeconfig", str(kubeconfig),
                "get", "deployment", "cert-manager",
                "-n", "cert-manager",
            ],
            check=False, capture=True,
        ).returncode
        return rc == 0
