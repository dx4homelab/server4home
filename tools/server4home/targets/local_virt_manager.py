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

        # Build the --sysinfo argument.
        sysinfo_parts = [
            "smbios",
            "system.manufacturer=server4home",
            f"system.product={vm}",
            # exact-hostname marker so set-hostname.sh skips the UUID suffix:
            f"oemStrings.entry0=server4home-hostname-exact={vm}",
        ]
        for i, entry in enumerate(ip_result.oem_strings, start=1):
            sysinfo_parts.append(f"oemStrings.entry{i}={entry}")
        sysinfo = ",".join(sysinfo_parts)

        # Build the --network argument.
        net_arg = f"bridge={LIBVIRT_BRIDGE},model=virtio"
        if mac:
            net_arg += f",mac={mac}"

        # virt-install
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
        vm = manifest.hostname

        # If the manifest specified a static IP, trust it.
        if manifest.primary_network.ip.provisioner == "static":
            static = manifest.primary_network.ip.static or ""
            # Strip /CIDR suffix if present.
            return static.split("/")[0]

        # Otherwise: find the MAC libvirt actually assigned, then ARP for it.
        if mac is None:
            mac = self._libvirt_mac_for(vm)

        log.info("Polling ARP for MAC %s (DHCP-assigned IP)", mac)
        for _ in range(60):
            ip = self._arp_lookup(mac)
            if ip:
                return ip
            time.sleep(5)

        # Last-ditch: DNS / mDNS by hostname.
        try:
            return run(["getent", "hosts", vm], capture=True).stdout.split()[0]
        except Exception:
            pass
        raise RuntimeError(
            f"could not discover IP for {vm} (mac={mac}). "
            f"Try `sudo virsh domifaddr {vm}` once the VM is up."
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
        out = run(["ip", "neigh"], capture=True, check=False).stdout
        for line in out.splitlines():
            if mac.lower() in line.lower():
                return line.split()[0]
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
