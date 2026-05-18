"""K3s installer plugin.

The K3s image already ships K3s and starts it on first boot. This installer
applies post-boot configuration from the manifest's `install:` entry — today,
that means translating `args:` such as `--disable=traefik` into a
`/etc/rancher/k3s/config.yaml.d/10-deploy.yaml` drop-in and restarting k3s.

Does NOT need the cluster kubeconfig (works over SSH directly).
"""

from __future__ import annotations

import time
from textwrap import dedent

from ..manifest import InstallSpec, Manifest
from ..registry import installers
from ..util import log
from .base import InstallContext, Installer


@installers.register("k3s")
class K3sInstaller(Installer):
    def requires_kubeconfig(self) -> bool:
        return False

    def apply(self, ctx: InstallContext, entry: InstallSpec) -> None:
        disabled = self._extract_disabled(entry.args)
        if not disabled:
            log.info("install[k3s]: no recognized args to apply")
            return

        config_yaml = self._render_config(disabled)
        log.info("install[k3s]: dropping config.yaml.d/10-deploy.yaml")
        ctx.ssh.put_text(
            config_yaml,
            "/etc/rancher/k3s/config.yaml.d/10-deploy.yaml",
            mode="0644",
        )

        log.info("install[k3s]: restarting k3s.service")
        ctx.ssh.run("systemctl restart k3s", sudo=True)
        # Give it a beat before the rancher installer hits the API.
        time.sleep(5)
        self._wait_ready(ctx)

    @staticmethod
    def _extract_disabled(args: list[str]) -> list[str]:
        out: list[str] = []
        for a in args:
            if a.startswith("--disable="):
                out.append(a[len("--disable="):])
            elif a.startswith("--disable "):
                out.append(a[len("--disable "):])
        return out

    @staticmethod
    def _render_config(disabled: list[str]) -> str:
        body = "disable:\n" + "\n".join(f"  - {d}" for d in disabled) + "\n"
        return dedent("""\
            # Managed by server4home/installers/k3s.py — do not edit.
            # Translated from manifest install[k3s].args.
        """) + body

    @staticmethod
    def _wait_ready(ctx: InstallContext) -> None:
        import subprocess
        log.info("Waiting for K3s API to be Ready again (polling silently)")
        ssh = ctx.ssh
        cmd = ssh._base + [f"{ssh.user}@{ssh.host}",
                           "sudo k3s kubectl get --raw=/readyz"]
        for _ in range(60):
            rc = subprocess.run(cmd, capture_output=True).returncode
            if rc == 0:
                log.info("K3s API Ready")
                return
            time.sleep(5)
        raise TimeoutError("K3s did not become Ready within 5 minutes")
