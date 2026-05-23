"""High-level orchestration: take a Manifest, produce a running, configured VM."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import secretref
from .installers.base import InstallContext
from .manifest import Manifest
from .registry import installers, secret_providers, targets
from .util import SSH, log


def deploy(manifest: Manifest, *, ssh_user: str | None = None,
           ssh_key: str | None = None,
           kubeconfig_dir: str | Path = "./kubeconfigs",
           wipe_data: bool = False) -> Path | None:
    """Run the full deploy pipeline.

    Returns the path to the fetched kubeconfig, or None for an agent-only
    deploy (agents have no cluster API of their own).

    ``wipe_data=True`` tells the target to drop any preserved per-VM data
    (the LVM data disk + identity meta sidecar) before creating the VM —
    necessary when the cluster identity has changed and you can't reuse the
    previous etcd/certs.
    """
    ssh_user = ssh_user or os.environ.get("SSH_USER", "developer")
    ssh_key = ssh_key or os.environ.get("SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519"))

    # 0) Resolve secret references in install entries before anything else —
    #    the target needs real values (e.g. K3s token) to inject via SMBIOS.
    _resolve_secrets(manifest)

    mode = manifest.k3s_mode()
    log.info("K3s mode: %s", mode)

    # 1) Create the VM via the chosen target plugin.
    target_cls = targets.get(manifest.target)
    target = target_cls()
    result = target.create(manifest, wipe_data=wipe_data)
    log.info("VM created: name=%s mac=%s", result.vm_name, result.mac)

    # 2) Discover its IP.
    ip = target.discover_ip(manifest, result.mac)
    log.info("VM IP: %s", ip)

    # 3) SSH + InstallContext.
    ssh = SSH(host=ip, user=ssh_user, key=ssh_key)
    ssh.wait_reachable()
    ctx = InstallContext(manifest=manifest, ssh=ssh)

    # 4) Wait for K3s. A server exposes /readyz; an agent has no local API,
    #    so we just wait for k3s.service to be active.
    if mode == "agent":
        _wait_for_k3s_agent(ssh)
    else:
        _wait_for_k3s_server(ssh)

    # 5) Installers. Split by whether they need the cluster kubeconfig.
    entries = manifest.installer_entries()
    pre_kc = [e for e in entries
              if not installers.get(e.name)().requires_kubeconfig()]
    post_kc = [e for e in entries
               if installers.get(e.name)().requires_kubeconfig()]

    for entry in pre_kc:
        log.info("Applying installer (pre-kubeconfig): %s", entry.name)
        installers.get(entry.name)().apply(ctx, entry)

    if mode == "agent":
        # Agents have no kubeconfig and nothing cluster-wide to install.
        if post_kc:
            log.warning("Ignoring kubeconfig-dependent installers on an agent "
                        "node: %s", [e.name for e in post_kc])
        log.info("Done.")
        log.info("VM:   %s @ %s  (joined cluster as agent)", manifest.hostname, ip)
        return None

    # Server: fetch kubeconfig and apply the rest.
    ctx.kubeconfig = _fetch_kubeconfig(ssh, manifest.hostname, ip, kubeconfig_dir)
    for entry in post_kc:
        log.info("Applying installer: %s", entry.name)
        installers.get(entry.name)().apply(ctx, entry)

    log.info("Done.")
    log.info("VM:         %s @ %s", manifest.hostname, ip)
    log.info("kubeconfig: %s", ctx.kubeconfig)
    log.info("Try:        KUBECONFIG=%s kubectl get nodes", ctx.kubeconfig)
    return ctx.kubeconfig


def destroy(manifest: Manifest) -> None:
    target_cls = targets.get(manifest.target)
    target = target_cls()
    target.destroy(manifest)


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _resolve_secrets(manifest: Manifest) -> None:
    """Replace every { secret: <name> } reference in install configs in place."""
    needs = any(secretref.has_secret_refs(e.config) for e in manifest.install)
    if not needs:
        return
    provider = secret_providers.get(manifest.secret_provider)()
    log.info("Resolving secret references via '%s' provider", manifest.secret_provider)
    for entry in manifest.install:
        entry.config = secretref.resolve(entry.config, provider)


def _ssh_rc(ssh: SSH, remote_cmd: str) -> int:
    """Run a remote command silently; return its exit code."""
    cmd = ssh._base + [f"{ssh.user}@{ssh.host}", remote_cmd]
    return subprocess.run(cmd, capture_output=True).returncode


def _wait_for_k3s_server(ssh: SSH, attempts: int = 60, delay: int = 5) -> None:
    log.info("Waiting for K3s API to report Ready (polling silently)")
    for _ in range(attempts):
        if _ssh_rc(ssh, "sudo k3s kubectl get --raw=/readyz") == 0:
            log.info("K3s API is Ready")
            return
        time.sleep(delay)
    raise TimeoutError("K3s API did not become Ready within 5 minutes")


def _wait_for_k3s_agent(ssh: SSH, attempts: int = 60, delay: int = 5) -> None:
    log.info("Waiting for k3s agent service to be active (polling silently)")
    for _ in range(attempts):
        if _ssh_rc(ssh, "systemctl is-active --quiet k3s") == 0:
            log.info("k3s agent service is active")
            return
        time.sleep(delay)
    raise TimeoutError("k3s agent service did not become active within 5 minutes")


def _fetch_kubeconfig(ssh: SSH, hostname: str, ip: str,
                      dest_dir: str | Path) -> Path:
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{hostname}.kubeconfig"
    log.info("Fetching kubeconfig -> %s", out)
    content = ssh.stdout("cat /etc/rancher/k3s/k3s.yaml", sudo=True)
    content = content.replace(
        "server: https://127.0.0.1:6443",
        f"server: https://{ip}:6443",
    )
    out.write_text(content)
    out.chmod(0o600)
    return out
