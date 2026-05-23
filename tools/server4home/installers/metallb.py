"""MetalLB installer.

Helm-installs MetalLB into `metallb-system`, then applies a default
`IPAddressPool` and `L2Advertisement` so K8s `Service`s of type
`LoadBalancer` (notably K3s's bundled Traefik) get an external IP.

Pairs with the K3s install entry having `--disable=servicelb` (MetalLB
replaces K3s's built-in service-loadbalancer). Traefik stays enabled —
MetalLB hands it the configured VIP.

Manifest:

    install:
      - name: metallb
        version: 0.16.0
        config:
          vip: 192.168.130.50          # single-VIP shortcut → /32 pool
          # or, for a range:
          # pool:
          #   addresses:
          #     - 192.168.130.50-192.168.130.59

L2 mode only (homelab default). BGP mode would need a BGP-capable upstream
router and extra `BGPPeer` / `BGPAdvertisement` config — outside the v1
scope of this plugin.
"""

from __future__ import annotations

import time
from typing import Any

import yaml

from ..manifest import InstallSpec
from ..registry import installers
from ..util import CommandError, Helm, log, require_tool, run
from .base import InstallContext, Installer

# 0.16.0 (released 2026-05-20) has a chart bug: the frr-k8s subchart
# unconditionally references .Values.prometheus.serviceMonitor.enabled,
# which is nil in the default values, so `helm install` errors at template
# render before anything is applied. Pin to the prior stable until upstream
# ships a fix. Bumpable per-manifest via `version:`.
DEFAULT_VERSION = "0.15.3"
REPO_NAME = "metallb"
REPO_URL = "https://metallb.github.io/metallb"
CHART = "metallb/metallb"
NAMESPACE = "metallb-system"


@installers.register("metallb")
class MetalLBInstaller(Installer):
    def apply(self, ctx: InstallContext, entry: InstallSpec) -> None:
        require_tool("kubectl")
        assert ctx.kubeconfig is not None, "metallb needs a kubeconfig"

        version = (entry.version or DEFAULT_VERSION).lstrip("v")
        addresses = self._addresses(entry.config)

        log.info("Installing MetalLB %s (pool addresses: %s)", version, addresses)
        helm = Helm(ctx.kubeconfig)
        helm.repo_add(REPO_NAME, REPO_URL)
        helm.repo_update(REPO_NAME)
        helm.upgrade_install(
            release="metallb",
            chart=CHART,
            namespace=NAMESPACE,
            version=version,
            timeout="10m",
        )

        kubeconfig = str(ctx.kubeconfig)
        pool_yaml = yaml.safe_dump({
            "apiVersion": "metallb.io/v1beta1",
            "kind": "IPAddressPool",
            "metadata": {"name": "default", "namespace": NAMESPACE},
            "spec": {"addresses": addresses},
        }, sort_keys=False)
        l2_yaml = yaml.safe_dump({
            "apiVersion": "metallb.io/v1beta1",
            "kind": "L2Advertisement",
            "metadata": {"name": "default", "namespace": NAMESPACE},
            "spec": {"ipAddressPools": ["default"]},
        }, sort_keys=False)

        # MetalLB's validation webhook needs a moment after the controller
        # pod reports Ready before it actually serves; retry on webhook
        # errors specifically.
        log.info("Applying IPAddressPool (addresses=%s)", addresses)
        self._apply_with_retry(kubeconfig, pool_yaml)
        log.info("Applying L2Advertisement")
        self._apply_with_retry(kubeconfig, l2_yaml)

    @staticmethod
    def _addresses(cfg: dict[str, Any]) -> list[str]:
        vip = cfg.get("vip")
        if vip:
            return [f"{vip}/32"]
        pool = cfg.get("pool") or {}
        addrs = pool.get("addresses")
        if addrs:
            return list(addrs)
        raise ValueError(
            "install[metallb].config requires either 'vip' (single VIP) "
            "or 'pool.addresses' (list of CIDRs / ranges)."
        )

    @staticmethod
    def _apply_with_retry(kubeconfig: str, manifest_yaml: str,
                          attempts: int = 12, delay: float = 5.0) -> None:
        last_err: str = ""
        for i in range(attempts):
            try:
                run(["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", "-"],
                    input_text=manifest_yaml, quiet=(i > 0))
                return
            except CommandError as e:
                last_err = e.stderr or str(e)
                webhook_unready = (
                    "webhook" in last_err.lower()
                    or "no endpoints available" in last_err.lower()
                    or "connection refused" in last_err.lower()
                )
                if not webhook_unready or i == attempts - 1:
                    raise
                if i == 0:
                    log.info("MetalLB webhook not yet serving; retrying every "
                             "%.0fs (up to %d times)", delay, attempts)
                time.sleep(delay)
