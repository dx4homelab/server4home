"""High-level orchestration: take a Manifest, produce a running, configured VM."""

from __future__ import annotations

import os
from pathlib import Path

from .installers.base import InstallContext
from .manifest import Manifest
from .registry import installers, targets
from .util import SSH, log, run


def deploy(manifest: Manifest, *, ssh_user: str | None = None,
           ssh_key: str | None = None,
           kubeconfig_dir: str | Path = "./kubeconfigs") -> Path:
    """Run the full deploy pipeline. Returns the path to the fetched kubeconfig."""

    ssh_user = ssh_user or os.environ.get("SSH_USER", "developer")
    ssh_key = ssh_key or os.environ.get("SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519"))

    # 1) Create the VM via the chosen target plugin.
    target_cls = targets.get(manifest.target)
    target = target_cls()
    result = target.create(manifest)
    log.info("VM created: name=%s mac=%s", result.vm_name, result.mac)

    # 2) Discover its IP.
    ip = target.discover_ip(manifest, result.mac)
    log.info("VM IP: %s", ip)

    # 3) Build SSH + InstallContext.
    ssh = SSH(host=ip, user=ssh_user, key=ssh_key)
    ssh.wait_reachable()

    ctx = InstallContext(manifest=manifest, ssh=ssh)

    # 4) Wait for K3s API readiness before applying installers.
    _wait_for_k3s(ssh)

    # 5) Apply all installers that don't need a kubeconfig (k3s args), then
    #    fetch the kubeconfig and apply the rest.
    entries = manifest.installer_entries()
    pre_kubeconfig = [e for e in entries
                      if not installers.get(e.name)().requires_kubeconfig()]
    post_kubeconfig = [e for e in entries
                       if installers.get(e.name)().requires_kubeconfig()]

    for entry in pre_kubeconfig:
        log.info("Applying installer (pre-kubeconfig): %s", entry.name)
        installers.get(entry.name)().apply(ctx, entry)

    if post_kubeconfig:
        ctx.kubeconfig = _fetch_kubeconfig(ssh, manifest.hostname, ip, kubeconfig_dir)
        for entry in post_kubeconfig:
            log.info("Applying installer: %s", entry.name)
            installers.get(entry.name)().apply(ctx, entry)
    else:
        # No kubeconfig-dependent installs, but the user often wants the file
        # anyway; fetch it as a convenience.
        ctx.kubeconfig = _fetch_kubeconfig(ssh, manifest.hostname, ip, kubeconfig_dir)

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
def _wait_for_k3s(ssh: SSH, attempts: int = 60, delay: int = 5) -> None:
    import time as _time
    log.info("Waiting for K3s API to report Ready (polling silently)")
    for _ in range(attempts):
        # Use Python's subprocess directly to bypass the per-call logger.
        import subprocess as _sp
        cmd = ssh._base + [f"{ssh.user}@{ssh.host}",
                           "sudo k3s kubectl get --raw=/readyz"]
        rc = _sp.run(cmd, capture_output=True).returncode
        if rc == 0:
            log.info("K3s API is Ready")
            return
        _time.sleep(delay)
    raise TimeoutError("K3s API did not become Ready within 5 minutes")


def _fetch_kubeconfig(ssh: SSH, hostname: str, ip: str,
                      dest_dir: str | Path) -> Path:
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{hostname}.kubeconfig"
    log.info("Fetching kubeconfig -> %s", out)
    content = ssh.stdout("cat /etc/rancher/k3s/k3s.yaml", sudo=True)
    # K3s writes server: https://127.0.0.1:6443 — rewrite for off-host use.
    content = content.replace(
        "server: https://127.0.0.1:6443",
        f"server: https://{ip}:6443",
    )
    out.write_text(content)
    out.chmod(0o600)
    return out
