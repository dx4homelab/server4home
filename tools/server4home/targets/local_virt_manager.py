"""Local libvirt / virt-manager target.

Creates a VM via `virt-install --import` from the freshly-built qcow2,
attaching an optional data disk for /var/lib/rancher (driven by the manifest
`disks:` list). MAC and IP are resolved through the provisioner plugins; both
flow into the VM via SMBIOS (`system.product` for the hostname; `oemStrings`
for static IP and the hostname-exact marker).
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from ..manifest import Manifest
from ..registry import ip_provisioners, mac_provisioners, targets
from ..util import SSH, CommandError, log, require_tool, run
from .base import CreateResult, Target

LIBVIRT_POOL = Path(os.environ.get("LIBVIRT_POOL", "/var/lib/libvirt/images"))
LIBVIRT_BRIDGE = os.environ.get("LIBVIRT_BRIDGE", "br0")
QCOW2_SRC = Path(os.environ.get("QCOW2_SRC", "output/qcow2/disk.qcow2"))


@targets.register("local-virt-manager")
class LocalVirtManager(Target):
    """Deploy onto the workstation's own libvirt."""

    def __init__(self) -> None:
        for tool in ("virt-install", "virsh", "qemu-img"):
            require_tool(tool)

    # ------------------------------------------------------------------ #
    # create()
    # ------------------------------------------------------------------ #
    def create(self, manifest: Manifest) -> CreateResult:
        if not QCOW2_SRC.is_file():
            raise RuntimeError(
                f"qcow2 source not found at {QCOW2_SRC}. "
                "Run `just rebuild-vm-k3s` first."
            )

        vm = manifest.hostname
        dst = LIBVIRT_POOL / f"{vm}.qcow2"
        data_dst = LIBVIRT_POOL / f"{vm}-data.qcow2"

        run(["sudo", "systemctl", "enable", "--now", "libvirtd.socket"])

        # Tear down a previous domain with this name (idempotent re-deploy).
        if self._domain_exists(vm):
            log.info("Domain '%s' exists; destroying and undefining", vm)
            run(["sudo", "virsh", "destroy", vm], check=False)
            try:
                run(["sudo", "virsh", "undefine", vm, "--nvram"])
            except CommandError:
                run(["sudo", "virsh", "undefine", vm])

        # Copy boot disk into the pool.
        log.info("Copying qcow2 to libvirt pool: %s", dst)
        run(["sudo", "cp", "-f", str(QCOW2_SRC), str(dst)])
        run(["sudo", "chown", "qemu:qemu", str(dst)])

        # Optional data disk for /var/lib/rancher.
        disk_args: list[str] = ["--disk", f"path={dst},format=qcow2,bus=virtio"]
        data_size = self._data_disk_size_for_rancher(manifest)
        if data_size:
            if data_dst.exists():
                log.info("Reusing existing data disk: %s", data_dst)
            else:
                log.info("Creating data disk %s (%s)", data_dst, data_size)
                run(["sudo", "qemu-img", "create", "-f", "qcow2",
                     str(data_dst), data_size])
                run(["sudo", "chown", "qemu:qemu", str(data_dst)])
            disk_args += ["--disk", f"path={data_dst},format=qcow2,bus=virtio"]

        # Resolve MAC + IP via provisioners.
        net = manifest.primary_network
        mac_plugin = mac_provisioners.get(net.mac.provisioner)()
        ip_plugin = ip_provisioners.get(net.ip.provisioner)()
        mac = mac_plugin.resolve(manifest, net.mac)
        ip_result = ip_plugin.resolve(manifest, net.ip)

        # Build the OEM-string list. Order doesn't matter; entryN indices are
        # just slots. The guest first-boot services scan all of them by key.
        oem: list[str] = [
            # exact-hostname marker so set-hostname.sh skips the UUID suffix:
            f"server4home-hostname-exact={vm}",
        ]
        oem += ip_result.oem_strings
        # K3s join config (mode/server/token) for k3s-config.sh. Secret refs
        # were already resolved by the runner, so `token` here is a literal.
        join = manifest.k3s_join()
        if join.get("mode"):
            oem.append(f"server4home-k3s-mode={join['mode']}")
        if join.get("server"):
            oem.append(f"server4home-k3s-url={join['server']}")
        if join.get("token"):
            oem.append(f"server4home-k3s-token={join['token']}")
        # etcd is the default in k3s-config.sh; only signal the sqlite opt-out.
        if manifest.k3s_datastore() == "sqlite":
            oem.append("server4home-k3s-datastore=sqlite")

        sysinfo_parts = [
            "smbios",
            "system.manufacturer=server4home",
            f"system.product={vm}",
        ]
        for i, entry in enumerate(oem):
            sysinfo_parts.append(f"oemStrings.entry{i}={entry}")
        sysinfo = ",".join(sysinfo_parts)

        # Build the --network argument.
        net_arg = f"bridge={LIBVIRT_BRIDGE},model=virtio"
        if mac:
            net_arg += f",mac={mac}"

        # virt-install. --channel wires up qemu-guest-agent so we can
        # `virsh domifaddr --source agent` to discover the VM's IP without
        # ARP polling.
        cmd = [
            "sudo", "virt-install",
            "--name", vm,
            "--memory", str(manifest.resources.memory),
            "--vcpus", str(manifest.resources.vcpus),
            *disk_args,
            "--import",
            "--os-variant", "fedora-unknown",
            "--network", net_arg,
            "--sysinfo", sysinfo,
            "--channel", "unix,target.type=virtio,target.name=org.qemu.guest_agent.0",
            "--boot", "uefi",
            "--graphics", "spice",
            "--noautoconsole",
        ]
        log.info("Creating libvirt domain '%s' on bridge=%s", vm, LIBVIRT_BRIDGE)
        run(cmd)

        return CreateResult(vm_name=vm, mac=mac)

    # ------------------------------------------------------------------ #
    # discover_ip()
    # ------------------------------------------------------------------ #
    def discover_ip(self, manifest: Manifest, mac: str | None) -> str:
        """Resolve the VM's primary IPv4 address.

        Priority:
          (1) Manifest-supplied static IP — trust it.
          (2) qemu-guest-agent via `virsh domifaddr --source agent`. Most
              reliable for bridge-attached VMs (image ships qga).
          (3) mDNS / DNS lookup by hostname.
          (4) arp-scan + ARP neighbor poll (fallback for older images
              without the agent or workstations without arp-scan).
        """
        vm = manifest.hostname

        if manifest.primary_network.ip.provisioner == "static":
            static = manifest.primary_network.ip.static or ""
            return static.split("/")[0]  # strip /CIDR

        # (2) qemu-guest-agent — try repeatedly while the agent comes up.
        log.info("Waiting for qemu-guest-agent to report a primary IPv4")
        for _ in range(60):
            ip = self._domifaddr_agent(vm)
            if ip:
                log.info("Guest-agent IP: %s", ip)
                return ip
            time.sleep(2)

        log.warning("Guest-agent didn't report an IP in 2 min; falling back to DNS/ARP")

        # (3) DNS / mDNS.
        for candidate in (vm, f"{vm}.local", f"{vm}.lan"):
            try:
                parts = run(["getent", "hosts", candidate],
                            capture=True, quiet=True).stdout.split()
                if parts:
                    log.info("Resolved %s -> %s via DNS/mDNS", candidate, parts[0])
                    return parts[0]
            except CommandError:
                pass

        # (4) ARP polling.
        if mac is None:
            mac = self._libvirt_mac_for(vm)
        log.info("Polling ARP for MAC %s (will take up to 5 min on a cold bridge)", mac)
        if shutil.which("arp-scan"):
            run(["sudo", "arp-scan", "--localnet", "--retry=2"],
                check=False, capture=True, quiet=True)
        for _ in range(60):
            ip = self._arp_lookup(mac)
            if ip:
                return ip
            time.sleep(5)

        raise RuntimeError(
            f"could not discover IP for {vm} (mac={mac}). "
            f"Workarounds: (a) `brew install arp-scan`, "
            f"(b) set `ip.provisioner: static` in the manifest, "
            f"(c) ensure the K3s image includes qemu-guest-agent (rebuild)."
        )

    def destroy(self, manifest: Manifest) -> None:
        vm = manifest.hostname
        if not self._domain_exists(vm):
            log.info("Domain '%s' does not exist; nothing to destroy", vm)
            return
        run(["sudo", "virsh", "destroy", vm], check=False)
        try:
            run(["sudo", "virsh", "undefine", vm, "--nvram", "--remove-all-storage"])
        except CommandError:
            run(["sudo", "virsh", "undefine", vm, "--remove-all-storage"])

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _domain_exists(name: str) -> bool:
        return run(["sudo", "virsh", "dominfo", name],
                   check=False, capture=True).returncode == 0

    @staticmethod
    def _libvirt_mac_for(vm: str) -> str:
        out = run(["sudo", "virsh", "domiflist", vm], capture=True).stdout
        # Header rows then table; MAC is the last column.
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("-") or line.startswith("Interface"):
                continue
            mac = line.split()[-1]
            if re.fullmatch(r"[0-9a-fA-F:]{17}", mac):
                return mac
        raise RuntimeError(f"could not find MAC for domain {vm}")

    @staticmethod
    def _arp_lookup(mac: str) -> str | None:
        out = run(["ip", "neigh"], capture=True, check=False, quiet=True).stdout
        for line in out.splitlines():
            if mac.lower() in line.lower():
                return line.split()[0]
        return None

    @staticmethod
    def _domifaddr_agent(vm: str) -> str | None:
        """Ask qemu-guest-agent for the VM's IPv4 (via `virsh domifaddr`)."""
        p = run(
            ["sudo", "virsh", "domifaddr", vm, "--source", "agent"],
            check=False, capture=True, quiet=True,
        )
        if p.returncode != 0:
            return None
        for line in p.stdout.splitlines():
            line = line.strip()
            # Lines look like:
            #   enp1s0     ...        ipv4   192.168.201.122/16
            if "ipv4" not in line:
                continue
            for token in line.split():
                if "." in token and "/" in token:
                    addr = token.split("/")[0]
                    if not addr.startswith("127."):
                        return addr
        return None

    @staticmethod
    def _data_disk_size_for_rancher(manifest: Manifest) -> str | None:
        for d in manifest.disks:
            if d.path == "/var/lib/rancher":
                if d.type != "lvm":
                    raise ValueError(
                        f"disks[/var/lib/rancher]: only type=lvm is supported "
                        f"in v1 (got '{d.type}')"
                    )
                return d.size
            log.warning("disks[%s] is not /var/lib/rancher; ignored in v1.", d.path)
        return None
