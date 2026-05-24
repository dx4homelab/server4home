"""High-level orchestration: take a Manifest, produce a running, configured VM."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import history, secretref
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

    # The recorder captures phase, vm/image facts, and installer outcomes,
    # then writes a single JSON file to deployments/ on __exit__ (success
    # OR failure). See server4home/history.py.
    with history.record(manifest, kind="deploy") as rec:
        # Identify the image early so even an early failure has a record.
        rec.set_image(
            ref=f"localhost/{manifest.image_ref()}:stable",
            k3s_version=_extract_k3s_version(manifest),
        )

        # 1) Create the VM via the chosen target plugin.
        target_cls = targets.get(manifest.target)
        target = target_cls()
        result = target.create(manifest, wipe_data=wipe_data)
        log.info("VM created: name=%s mac=%s", result.vm_name, result.mac)
        rec.set_vm(name=result.vm_name, mac=result.mac)

        # 2) Discover its IP.
        ip = target.discover_ip(manifest, result.mac)
        log.info("VM IP: %s", ip)
        rec.set_vm(ip=ip)

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
            rec.installer_start(entry.name, entry.version)
            installers.get(entry.name)().apply(ctx, entry)
            rec.installer_ok()

        if mode == "agent":
            # Agents have no kubeconfig and nothing cluster-wide to install.
            if post_kc:
                log.warning("Ignoring kubeconfig-dependent installers on an agent "
                            "node: %s", [e.name for e in post_kc])
                for entry in post_kc:
                    rec.installer_start(entry.name, entry.version)
                    rec.installer_skipped()
            log.info("Done.")
            log.info("VM:   %s @ %s  (joined cluster as agent)", manifest.hostname, ip)
            return None

        # Server: fetch kubeconfig and apply the rest.
        ctx.kubeconfig = _fetch_kubeconfig(ssh, manifest.hostname, ip, kubeconfig_dir)
        for entry in post_kc:
            log.info("Applying installer: %s", entry.name)
            rec.installer_start(entry.name, entry.version)
            installers.get(entry.name)().apply(ctx, entry)
            rec.installer_ok()

        log.info("Done.")
        log.info("VM:         %s @ %s", manifest.hostname, ip)
        log.info("kubeconfig: %s", ctx.kubeconfig)
        log.info("Try:        KUBECONFIG=%s kubectl get nodes", ctx.kubeconfig)
        return ctx.kubeconfig


def destroy(manifest: Manifest) -> None:
    target_cls = targets.get(manifest.target)
    target = target_cls()
    target.destroy(manifest)


def apply(manifest: Manifest, *,
          kubeconfig: str | Path | None = None,
          kubeconfig_dir: str | Path = "./kubeconfigs",
          only: list[str] | None = None,
          skip: list[str] | None = None) -> Path:
    """Reconcile installers against an EXISTING cluster.

    Unlike ``deploy``, this does NOT call ``target.create()`` or touch the VM
    via SSH — it's a cluster-side operation that relies on the kubeconfig
    fetched by the original deploy. Use it to bump helm-chart versions
    (rancher-manager, metallb, cert-manager) after editing a manifest:

        $ $EDITOR instances/foo.yaml      # version: v2.15.0
        $ server4home apply instances/foo.yaml

    Installers that report ``requires_fresh_node() == True`` are skipped
    (today: just k3s, since the K3s binary lives in the bootc image, not
    in a helm chart).

    Returns the kubeconfig path used.
    """
    # Resolve secret references in install entries (same as deploy).
    _resolve_secrets(manifest)

    # Find the kubeconfig — explicit override, then convention, then error.
    kc = Path(kubeconfig) if kubeconfig else Path(kubeconfig_dir) / f"{manifest.hostname}.kubeconfig"
    if not kc.is_file():
        raise FileNotFoundError(
            f"kubeconfig not found at {kc}. `apply` reconciles installers "
            f"against an existing cluster — run `server4home deploy "
            f"{manifest.source_path}` first, or pass --kubeconfig <path> "
            f"if the kubeconfig lives elsewhere."
        )
    log.info("Using kubeconfig: %s", kc)

    # SSH is not used by apply today (every helm installer is cluster-side).
    # Construct an InstallContext with the kubeconfig but no SSH — installers
    # whose apply() touches ctx.ssh will fail loudly. That's the right
    # signal (those installers don't belong in `apply`).
    ctx = InstallContext(manifest=manifest, ssh=None, kubeconfig=kc)  # type: ignore[arg-type]

    only_set = set(only) if only else None
    skip_set = set(skip) if skip else set()

    with history.record(manifest, kind="apply") as rec:
        rec.set_image(
            ref=f"localhost/{manifest.image_ref()}:stable",
            k3s_version=_extract_k3s_version(manifest),
        )
        for entry in manifest.installer_entries():
            inst = installers.get(entry.name)()
            if only_set is not None and entry.name not in only_set:
                log.info("Skipping installer (not in --only): %s", entry.name)
                rec.installer_start(entry.name, entry.version)
                rec.installer_skipped()
                continue
            if entry.name in skip_set:
                log.info("Skipping installer (in --skip): %s", entry.name)
                rec.installer_start(entry.name, entry.version)
                rec.installer_skipped()
                continue
            if inst.requires_fresh_node():
                log.info("Skipping installer (requires fresh node): %s", entry.name)
                rec.installer_start(entry.name, entry.version)
                rec.installer_skipped()
                continue
            log.info("Reconciling installer: %s", entry.name)
            rec.installer_start(entry.name, entry.version)
            inst.apply(ctx, entry)
            rec.installer_ok()

    log.info("Done. kubeconfig=%s", kc)
    return kc


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _resolve_secrets(manifest: Manifest) -> None:
    """Replace every { secret: <name> } reference in install configs in place."""
    needs = any(secretref.has_secret_refs(e.config) for e in manifest.install)
    if not needs:
        return
    provider = secret_providers.get(manifest.secret_provider)()
    # Bind the manifest's hostname so providers that support per-host
    # overlays (today: `local`) prefer values scoped to this VM before
    # falling back to the global namespace.
    provider.bind_hostname(manifest.hostname)
    log.info(
        "Resolving secret references via '%s' provider (hostname=%s)",
        manifest.secret_provider, manifest.hostname,
    )
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


def _extract_k3s_version(manifest: Manifest) -> str | None:
    """Best-effort: the K3s install entry's `version:` (drives image tag)."""
    k3s = manifest.k3s_install()
    return k3s.version if k3s else None


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
