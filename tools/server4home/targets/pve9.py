"""Proxmox VE 9 target — REST API + one SSH hop for `qm importdisk`.

Provisions a VM on a Proxmox host via the REST API:
    https://<pve-host>:8006/api2/json/...

Most operations are pure API calls (create VM, set config, start, query
guest-agent IPs). The one unavoidable SSH hop is `qm importdisk`: the API's
own disk-import endpoint expects raw-format pre-uploaded blobs and is
genuinely too awkward to use for a qcow2. Standard pattern, even in
proxmoxer/Ansible.

Authentication: a Proxmox API token stored as the `proxmox/api-token`
secret. Token string format is the one Proxmox shows you at creation, e.g.

    server4home@pve!deploy=8c4a3f02-1234-5678-9abc-def012345678

Configuration via environment variables (defaults shown):

    PVE_HOST           pve9.local.homelabsolutions.net
    PVE_PORT           8006
    PVE_NODE           pve9
    PVE_STORAGE        local-lvm
    PVE_BRIDGE         vmbr0
    PVE_SSH_USER       root            # for the qm importdisk SSH hop
    PVE_VERIFY_TLS     0               # 0/false = accept self-signed
    PVE_API_TIMEOUT    60              # seconds, per-request

The Manifest's `disks[/var/lib/rancher]` + `network[0]` + K3s join config
all flow through the same plugins as local-virt-manager; only the VM
provisioning surface differs.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from ..manifest import Manifest
from ..registry import ip_provisioners, mac_provisioners, secret_providers, targets
from ..util import CommandError, log, require_tool, run
from .base import CreateResult, IdentityMismatchError, Target

QCOW2_SRC = Path(os.environ.get("QCOW2_SRC", "output/qcow2/disk.qcow2"))

# Marker line written into a VM's `description` field by `create()` so that a
# later `create()` against the same VMID can detect identity mismatches
# (e.g. someone renamed `hostname:` in the manifest but kept the same pinned
# `proxmox.vmid:`). On PVE, destroying a VM destroys its data disks along
# with it — there's no "preserve data disk across VM destroy" the way libvirt
# files in /var/lib/libvirt/images survive. So re-deploy on PVE is destructive
# by definition, and the identity check is the gate that forces an
# explicit acknowledgment (--wipe-data) before that destruction happens.
IDENTITY_MARKER = "# server4home:data-identity"


def _read_identity_from_description(desc: str | None) -> str | None:
    """Pull the recorded server4home hostname out of a VM description.

    The marker has the form: `# server4home:data-identity hostname=<name>`.
    Returns None if absent. Robust to user-added notes around it.
    """
    if not desc:
        return None
    for line in desc.splitlines():
        line = line.strip()
        if not line.startswith(IDENTITY_MARKER):
            continue
        rest = line[len(IDENTITY_MARKER):].strip()
        for pair in rest.split():
            k, _, v = pair.partition("=")
            if k == "hostname":
                return v
    return None


def _update_description_with_identity(prior: str | None, hostname: str) -> str:
    """Return a new description that preserves any user-authored lines but
    replaces (or appends) the server4home identity marker.

    Idempotent — running it twice with the same args yields the same output.
    """
    user_lines = [
        ln for ln in (prior or "").splitlines()
        if not ln.strip().startswith(IDENTITY_MARKER)
    ]
    marker = f"{IDENTITY_MARKER} hostname={hostname}"
    user_lines.append(marker)
    return "\n".join(line.rstrip() for line in user_lines).strip() + "\n"


@targets.register("pve9")
class Pve9(Target):
    """Deploy onto a Proxmox VE 9 host via REST API."""

    def __init__(self) -> None:
        require_tool("ssh")
        require_tool("scp")

        self.host = os.environ.get("PVE_HOST", "pve9.local.homelabsolutions.net")
        self.port = int(os.environ.get("PVE_PORT", "8006"))
        self.node = os.environ.get("PVE_NODE", "pve9")
        self.storage = os.environ.get("PVE_STORAGE", "local-lvm")
        self.bridge = os.environ.get("PVE_BRIDGE", "vmbr0")
        self.ssh_user = os.environ.get("PVE_SSH_USER", "root")
        self.verify_tls = os.environ.get("PVE_VERIFY_TLS", "0").lower() in (
            "1", "true", "yes", "y",
        )
        self.api_timeout = float(os.environ.get("PVE_API_TIMEOUT", "60"))
        self.api_base = f"https://{self.host}:{self.port}/api2/json"

        # The PVE API token may live in a per-hostname overlay in
        # secrets.yaml, so we cannot resolve it here — the manifest (and
        # therefore the hostname to bind on the secret provider) isn't
        # known yet. Defer to _ensure_client(manifest), called from
        # create() and destroy() before any API call.
        self.token: str | None = None
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------ #
    # create()
    # ------------------------------------------------------------------ #
    def create(self, manifest: Manifest, *,
               wipe_data: bool = False) -> CreateResult:
        if not QCOW2_SRC.is_file():
            raise RuntimeError(
                f"qcow2 source not found at {QCOW2_SRC}. "
                "Run `just rebuild-vm-k3s` first."
            )

        self._ensure_client(manifest)
        vm = manifest.hostname

        # 1) Resolve VMID — prefer manifest's `proxmox.vmid`, fall back to
        # Proxmox's /cluster/nextid for a free integer.
        vmid = self._resolve_vmid(manifest)
        log.info("PVE target: vmid=%d node=%s host=%s", vmid, self.node, self.host)

        # 1b) Pre-flight identity check. If a VM already exists at this VMID,
        # consult its description for a server4home identity marker and decide
        # whether re-deploy is safe. PVE destroys disks along with VMs, so
        # re-deploy is destructive by definition — the check forces explicit
        # --wipe-data acknowledgement.
        existing_config = self._get_vm_config_or_none(vmid)
        if existing_config is not None:
            self._handle_existing_vm(manifest, vmid, existing_config, wipe_data)

        # 2) Build the SMBIOS args. Two distinct slots in QEMU/Proxmox:
        #    - smbios1:    DMI type 1 (manufacturer/product/etc.) — first-class qm setting,
        #                  settable via the API.
        #    - args/-smbios type=11 (OEM strings, DMI type 11): not directly settable via
        #                  any qm field, and the raw `args` field is guarded by Proxmox so
        #                  that **only literal root@pam over the local CLI** can set it.
        #                  API tokens with Administrator role get 500: "only root can set
        #                  'args' config". We therefore inject `args` via SSH-as-root
        #                  (`qm set --args ...`) right after creating the VM shell.
        smbios1 = self._smbios1_for(vm)
        oem_args = self._oem_args_for(manifest)

        # 3) Allocate the VM shell — without `args`. We set that field over SSH below.
        memory = manifest.resources.memory
        cores = manifest.resources.vcpus
        net0 = self._build_net_arg(manifest)
        log.info("Creating VM %s (vmid=%d) — POST /nodes/%s/qemu", vm, vmid, self.node)
        self._wait_for_task(self._api(
            "POST", f"/nodes/{self.node}/qemu", data={
                "vmid": vmid,
                "name": vm,
                "memory": memory,
                "cores": cores,
                "cpu": "host",
                "ostype": "l26",
                "machine": "q35",
                "bios": "ovmf",
                "scsihw": "virtio-scsi-single",
                "agent": "enabled=1",
                "net0": net0,
                "efidisk0": f"{self.storage}:0,efitype=4m,pre-enrolled-keys=0,format=raw",
                "smbios1": smbios1,
                "serial0": "socket",
                "vga": "serial0",
            },
        ))

        # 3b) Set the QEMU `args` field via SSH-as-root (API tokens can't).
        # Carries the SMBIOS OEM strings into the running QEMU process, which
        # the first-boot services in the image (set-hostname / k3s-config /
        # network-static) read via `dmidecode -t 11`.
        if oem_args:
            import shlex
            log.info("Setting `args` on vmid=%d via SSH (root-only field)", vmid)
            run(["ssh", f"{self.ssh_user}@{self.host}",
                 f"qm set {vmid} --args {shlex.quote(oem_args)}"])

        # 4) scp + qm importdisk via SSH (the API path for raw disk import
        # is genuinely awkward; this is what proxmoxer/Ansible also do).
        remote_qcow2 = f"/var/lib/vz/template/iso/server4home-{vm}.qcow2"
        log.info("scp qcow2 → %s:%s", self.host, remote_qcow2)
        run(["scp", "-q",
             str(QCOW2_SRC),
             f"{self.ssh_user}@{self.host}:{remote_qcow2}"])
        # NOTE: do NOT pass `--format qcow2` here. The destination storage
        # picks its own format (raw for LVM/LVM-thin/Ceph/iSCSI; qcow2 for
        # directory-backed). With `--format qcow2` against LVM-thin, qm
        # transfers the bytes, prints "successfully imported", but the LV
        # is rolled back on finalize because LVM doesn't hold qcow2 files.
        # The next PUT /config then fails with "no such logical volume".
        log.info("qm importdisk %d (over ssh; storage default format)", vmid)
        run(["ssh", f"{self.ssh_user}@{self.host}",
             f"qm importdisk {vmid} {remote_qcow2} {self.storage}"])

        # 5) Convert the imported disk (parked as unusedN by qm importdisk)
        # into scsi0. Two separate PUTs by design:
        #
        #   a) Attach the LV to scsi0 by its real volume ID — NOT by the
        #      `unusedN` key (the API parser rejects that string as a volume
        #      ID with 400: "unable to parse volume ID 'unused0'"; the qm
        #      CLI doesn't either, despite occasional forum claims).
        #   b) Then, in a separate request, drop the now-redundant unusedN
        #      entry. Critically, this MUST be a separate call: combining
        #      it with the scsi0 set makes Proxmox process the delete first,
        #      which destroys the LV (500: "no such logical volume") before
        #      the scsi0 assignment can take a reference. Once scsi0 holds
        #      the LV, deleting the unused entry is harmless.
        config = self._api("GET", f"/nodes/{self.node}/qemu/{vmid}/config")
        unused_key = self._find_imported_unused_key(config)
        if not unused_key:
            raise RuntimeError(
                f"qm importdisk completed but no unused disk found on vmid={vmid}; "
                f"current config: {config}"
            )
        unused_value = str(config.get(unused_key, ""))
        vol_id = unused_value.split(",")[0].strip()
        if not vol_id or ":" not in vol_id:
            raise RuntimeError(
                f"{unused_key} has unexpected value {unused_value!r}; "
                f"expected '<storage>:<volume>' as the leading token"
            )

        log.info("Attaching scsi0 → %s (resolved from %s)", vol_id, unused_key)
        self._api("PUT", f"/nodes/{self.node}/qemu/{vmid}/config", data={
            "scsi0": f"{vol_id},discard=on,iothread=1,ssd=1",
            "boot": "order=scsi0",
        })

        # Verify the attach took, then drop the stale unused entry.
        config = self._api("GET", f"/nodes/{self.node}/qemu/{vmid}/config")
        if not config.get("scsi0"):
            raise RuntimeError(
                f"scsi0 should be set to {vol_id} but config shows: {config}"
            )
        if config.get(unused_key):
            log.info("Removing stale %s entry (LV is now referenced by scsi0)",
                     unused_key)
            self._api("PUT", f"/nodes/{self.node}/qemu/{vmid}/config", data={
                "delete": unused_key,
            })

        # 6) Resize boot disk to manifest size (default 64G).
        boot_size = self._boot_disk_size(manifest)
        log.info("Resizing scsi0 → %s", boot_size)
        self._wait_for_task(self._api(
            "PUT", f"/nodes/{self.node}/qemu/{vmid}/resize",
            data={"disk": "scsi0", "size": boot_size},
        ))

        # 7) Optional data disk (LVM under /var/lib/rancher).
        data_size = self._data_disk_size_for_rancher(manifest)
        if data_size:
            size_gb = self._size_to_gb(data_size)
            log.info("Attaching blank data disk scsi1 (%dG)", size_gb)
            self._api("PUT", f"/nodes/{self.node}/qemu/{vmid}/config", data={
                "scsi1": f"{self.storage}:{size_gb},discard=on,iothread=1,ssd=1",
            })

        # 8) Start.
        log.info("Starting VM %d", vmid)
        self._wait_for_task(self._api(
            "POST", f"/nodes/{self.node}/qemu/{vmid}/status/start", data={},
        ))

        # 9) Read the MAC the API actually settled on (may differ if user
        # set mac.provisioner=fixed; net0 string includes it).
        config = self._api("GET", f"/nodes/{self.node}/qemu/{vmid}/config")
        mac = self._mac_from_net0(config.get("net0", "")) or None

        # 10) Stamp the identity marker into the VM's description so future
        # re-deploys can detect manifest renames.
        self._write_identity_to_vm(vmid, manifest.hostname,
                                   prior_description=None)

        return CreateResult(vm_name=vm, mac=mac)

    # ------------------------------------------------------------------ #
    # discover_ip()
    # ------------------------------------------------------------------ #
    def discover_ip(self, manifest: Manifest, mac: str | None) -> str:
        if manifest.primary_network.ip.provisioner == "static":
            return (manifest.primary_network.ip.static or "").split("/")[0]

        self._ensure_client(manifest)
        vmid = self._resolve_vmid(manifest)
        log.info("Polling qemu-guest-agent for primary IPv4 via API")
        for _ in range(60):
            try:
                data = self._api(
                    "GET",
                    f"/nodes/{self.node}/qemu/{vmid}/agent/network-get-interfaces",
                )
            except httpx.HTTPStatusError:
                time.sleep(2)
                continue
            for iface in (data.get("result") or []) if data else []:
                if iface.get("name") == "lo":
                    continue
                for addr in iface.get("ip-addresses") or []:
                    if (
                        addr.get("ip-address-type") == "ipv4"
                        and not addr.get("ip-address", "").startswith(("127.", "169.254."))
                    ):
                        log.info("Guest-agent IP: %s", addr["ip-address"])
                        return addr["ip-address"]
            time.sleep(2)

        raise RuntimeError(
            f"could not discover IP for {manifest.hostname} via Proxmox guest-agent "
            f"(vmid={vmid}). Check `qm guest cmd {vmid} network-get-interfaces` on PVE."
        )

    # ------------------------------------------------------------------ #
    # destroy()
    # ------------------------------------------------------------------ #
    def destroy(self, manifest: Manifest) -> None:
        self._ensure_client(manifest)
        try:
            vmid = self._lookup_vmid_by_name(manifest.hostname)
        except RuntimeError:
            log.info("VM '%s' not found on %s; nothing to destroy", manifest.hostname, self.node)
            return
        self._destroy_vm_by_vmid(vmid)

    def _destroy_vm_by_vmid(self, vmid: int) -> None:
        """Stop + delete a VM by VMID (no lookup-by-name). Idempotent: a
        missing VM is a successful no-op.

        Used by both ``destroy()`` (after lookup-by-name) and by the
        ``--wipe-data`` path in ``create()`` (where the VM may have a
        different name than the new manifest's hostname).
        """
        # `qm stop` on an already-stopped VM is a no-op task; ignore the
        # "not running" error from the API. Same for the DELETE on a
        # missing VM (in case the VM disappeared between lookup and now).
        try:
            log.info("Stopping VM %d", vmid)
            self._wait_for_task(self._api(
                "POST", f"/nodes/{self.node}/qemu/{vmid}/status/stop", data={},
            ))
        except Exception as e:
            log.info("VM %d stop: %s (continuing)", vmid, e)
        log.info("Deleting VM %d (and all its disks)", vmid)
        self._wait_for_task(self._api(
            "DELETE", f"/nodes/{self.node}/qemu/{vmid}",
            params={"purge": 1, "destroy-unreferenced-disks": 1},
        ))

    # ------------------------------------------------------------------ #
    # Identity check helpers
    # ------------------------------------------------------------------ #
    def _get_vm_config_or_none(self, vmid: int) -> dict | None:
        """Return the VM's config dict if it exists, else None.

        Used by `create()` to check whether the target vmid is already
        occupied — the actual config tells us both 'does it exist' and
        'what identity does it claim'.
        """
        try:
            return self._api("GET", f"/nodes/{self.node}/qemu/{vmid}/config")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 500 and "does not exist" in e.response.text:
                return None
            # 404 / other client errors: VM is not there.
            if 400 <= e.response.status_code < 500:
                return None
            raise

    def _handle_existing_vm(self, manifest: Manifest, vmid: int,
                            existing_config: dict, wipe_data: bool) -> None:
        """Pre-flight: decide what to do when a VM already exists at vmid.

        Three cases:
          1. Recorded identity matches manifest.hostname  → require --wipe-data
             (any re-create on PVE wipes the data disk; user must opt in).
          2. Recorded identity DIFFERS from manifest.hostname → likely a rename
             mistake. Refuse without --wipe-data; the message names both
             hostnames so the operator can decide.
          3. No recorded identity (VM predates this feature, or was created
             outside server4home) → refuse without --wipe-data — we can't
             verify anything.
        """
        desc = existing_config.get("description")
        recorded = _read_identity_from_description(desc)
        existing_name = existing_config.get("name") or "<unnamed>"

        if recorded is None:
            if not wipe_data:
                raise IdentityMismatchError(
                    f"VM {vmid} ('{existing_name}') exists on node "
                    f"'{self.node}' but has no server4home identity marker "
                    f"in its description — runner cannot verify safe reuse. "
                    f"Pass --wipe-data to destroy it and rebuild, or destroy "
                    f"it manually first (`qm destroy {vmid} --purge 1`)."
                )
            log.warning("VM %d exists with no identity marker; --wipe-data "
                        "set → destroying", vmid)
        elif recorded != manifest.hostname:
            if not wipe_data:
                raise IdentityMismatchError(
                    f"VM {vmid} was created for hostname '{recorded}'; "
                    f"current manifest hostname is '{manifest.hostname}'. "
                    f"This usually means the manifest's `hostname:` was "
                    f"renamed but `proxmox.vmid:` was kept. Either:\n"
                    f"  - fix the manifest hostname back to '{recorded}', or\n"
                    f"  - pass --wipe-data (= `just deploy-fresh ...`) to "
                    f"destroy VM {vmid} and rebuild under the new name."
                )
            log.warning("Identity rename detected: '%s' -> '%s'; --wipe-data "
                        "set → destroying VM %d", recorded, manifest.hostname,
                        vmid)
        else:
            # Same hostname, same vmid → idempotent re-deploy. On PVE this
            # would still wipe the data disk (no preservation across VM
            # destroy), so we treat it the same as a wipe and force the
            # operator to acknowledge.
            if not wipe_data:
                raise IdentityMismatchError(
                    f"VM {vmid} already exists for hostname "
                    f"'{manifest.hostname}'. On Proxmox, re-creating it "
                    f"destroys the data disk (and the K3s/etcd state on it). "
                    f"Choose:\n"
                    f"  - reconcile installers only (recommended for chart "
                    f"bumps, no VM touch):\n"
                    f"      just apply {manifest.source_path or '<manifest>'}\n"
                    f"  - rebuild from scratch (destroys the cluster):\n"
                    f"      just deploy-fresh {manifest.source_path or '<manifest>'}"
                )
            log.warning("Re-deploy of same identity '%s' with --wipe-data → "
                        "destroying VM %d", manifest.hostname, vmid)

        # All paths that reach here have wipe_data=True (or the no-identity
        # path was downgraded to a warning). Destroy the existing VM.
        self._destroy_vm_by_vmid(vmid)

    def _write_identity_to_vm(self, vmid: int, hostname: str,
                              *, prior_description: str | None) -> None:
        """Write/update the identity marker in the VM's description."""
        new_desc = _update_description_with_identity(prior_description, hostname)
        self._api("PUT", f"/nodes/{self.node}/qemu/{vmid}/config", data={
            "description": new_desc,
        })

    # ------------------------------------------------------------------ #
    # API client + helpers
    # ------------------------------------------------------------------ #
    def _api(self, method: str, path: str, *,
             data: dict | None = None,
             params: dict | None = None) -> Any:
        url = self.api_base + path
        log.info("$ pve api: %s %s", method, path)
        r = self._client.request(method, url, data=data, params=params)
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"PVE API {method} {path} → {r.status_code}: {r.text}",
                request=r.request, response=r,
            )
        body = r.json()
        return body.get("data")

    def _wait_for_task(self, upid: Any, *,
                       attempts: int = 180, delay: float = 2.0) -> None:
        """Many PVE mutations return a UPID; poll until the task ends.

        For endpoints that return immediate data (e.g. GETs), `upid` will
        be a non-UPID value; we treat anything not starting with 'UPID:'
        as already-complete and return immediately.
        """
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return
        for _ in range(attempts):
            r = self._client.get(f"{self.api_base}/nodes/{self.node}/tasks/{upid}/status")
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"task status {upid}: {r.status_code}: {r.text}",
                    request=r.request, response=r,
                )

            status = (r.json().get("data") or {})
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise RuntimeError(
                        f"PVE task failed: {upid} exitstatus={status.get('exitstatus')}"
                    )
                return
            time.sleep(delay)
        raise TimeoutError(f"PVE task {upid} did not complete in {attempts*delay:.0f}s")

    def _ensure_client(self, manifest: Manifest) -> None:
        """Lazily resolve the PVE token (with hostname overlay) + build the
        httpx client. Idempotent — subsequent calls are no-ops.

        Token loading is deferred (vs. __init__) so that the manifest's
        hostname is known and can be bound on the secret provider — that
        lets `proxmox/api-token` live under a per-hostname YAML overlay.
        """
        if self._client is not None:
            return
        self.token = self._load_token(manifest)
        self._client = httpx.Client(
            verify=self.verify_tls,
            timeout=self.api_timeout,
            headers={"Authorization": f"PVEAPIToken={self.token}"},
        )

    def _load_token(self, manifest: Manifest) -> str:
        provider = secret_providers.get(manifest.secret_provider)()
        provider.bind_hostname(manifest.hostname)
        try:
            return provider.get("proxmox/api-token")
        except Exception as e:
            raise RuntimeError(
                "Proxmox API token not found. Add to secrets/secrets.yaml:\n"
                '    "proxmox/api-token": "<user>@<realm>!<id>=<secret>"\n'
                f"(underlying error: {e})"
            ) from e

    def _resolve_vmid(self, manifest: Manifest) -> int:
        """Pick a VMID. Order:
            1. manifest.proxmox.vmid (typed, validated, the documented path)
            2. Existing VM with same name (idempotent re-deploy)
            3. /cluster/nextid (auto-allocate)

        Pinning a VMID is the right thing to do when the operator uses a
        grouped numbering scheme (e.g. 7xxxx for k3s control-plane VMs).
        Auto-allocate is fine for ephemeral experiments.
        """
        if manifest.proxmox is not None and manifest.proxmox.vmid is not None:
            return manifest.proxmox.vmid
        try:
            return self._lookup_vmid_by_name(manifest.hostname)
        except RuntimeError:
            pass
        nextid = self._api("GET", "/cluster/nextid")
        return int(nextid)

    def _lookup_vmid_by_name(self, name: str) -> int:
        resources = self._api("GET", "/cluster/resources", params={"type": "vm"})
        for r in resources or []:
            if r.get("name") == name and r.get("node") == self.node:
                return int(r["vmid"])
        raise RuntimeError(f"no VM named '{name}' on node '{self.node}'")

    # ------------------------------------------------------------------ #
    # SMBIOS / OEM string assembly
    # ------------------------------------------------------------------ #
    def _smbios1_for(self, vm: str) -> str:
        """qm `--smbios1`: base64 fields, vm name in product, deterministic UUID."""
        def b64(s: str) -> str:
            return base64.b64encode(s.encode()).decode()
        return ",".join([
            f"uuid={uuid.uuid4()}",
            f"manufacturer={b64('server4home')}",
            f"product={b64(vm)}",
        ])

    def _oem_args_for(self, manifest: Manifest) -> str:
        """OEM strings (DMI type 11) via QEMU's `-smbios type=11,value=...`.

        Each OEM string becomes one `value=<string>` token. Multiple values
        live under the same `-smbios type=11`.
        """
        vm = manifest.hostname
        oem: list[str] = [
            f"server4home-hostname-exact={vm}",
        ]

        # IP plugin (static IP fragments).
        net = manifest.primary_network
        ip_plugin = ip_provisioners.get(net.ip.provisioner)()
        ip_result = ip_plugin.resolve(manifest, net.ip)
        oem.extend(ip_result.oem_strings)

        # K3s join config (mode/server/token; resolved literals from secrets).
        join = manifest.k3s_join()
        if join.get("mode"):
            oem.append(f"server4home-k3s-mode={join['mode']}")
        if join.get("server"):
            oem.append(f"server4home-k3s-url={join['server']}")
        if join.get("token"):
            oem.append(f"server4home-k3s-token={join['token']}")
        if manifest.k3s_datastore() == "sqlite":
            oem.append("server4home-k3s-datastore=sqlite")

        # First-boot bootc-switch target (consumed by bootc-switch.sh).
        # When set, the VM swaps from the qcow2-baked `localhost/...` ref
        # to the GHCR-published image and reboots before k3s.service starts.
        if manifest.upgrade2image:
            oem.append(f"server4home-image-ref={manifest.upgrade2image}")

        # Encode as: `-smbios type=11,value=A,value=B,value=C`
        values = ",".join(f"value={s}" for s in oem)
        return f"-smbios type=11,{values}"

    def _build_net_arg(self, manifest: Manifest) -> str:
        net = manifest.primary_network
        mac_plugin = mac_provisioners.get(net.mac.provisioner)()
        mac = mac_plugin.resolve(manifest, net.mac)
        out = f"virtio,bridge={self.bridge}"
        if mac:
            out += f",macaddr={mac}"
        return out

    # ------------------------------------------------------------------ #
    # Disk helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_imported_unused_key(config: dict) -> str | None:
        """qm importdisk parks the new disk under unused0 / unused1 / etc.

        Return the *key* (e.g. 'unused0') so the caller can do
        `scsi0=unused0` and let Proxmox atomically convert the entry,
        preserving the underlying LV.
        """
        for key in sorted(config):
            if key.startswith("unused"):
                return key
        return None

    @staticmethod
    def _boot_disk_size(manifest: Manifest) -> str:
        # Default 64G; allow override via manifest.resources.boot_disk_size
        extras = (manifest.resources.model_extra or {}) if hasattr(manifest.resources, "model_extra") else {}
        return str(extras.get("boot_disk_size", "64G")) if isinstance(extras, dict) else "64G"

    @staticmethod
    def _data_disk_size_for_rancher(manifest: Manifest) -> str | None:
        for d in manifest.disks:
            if d.path == "/var/lib/rancher":
                if d.type != "lvm":
                    raise ValueError(
                        f"disks[/var/lib/rancher]: only type=lvm supported (got '{d.type}')"
                    )
                return d.size
            log.warning("disks[%s] is not /var/lib/rancher; ignored in v1.", d.path)
        return None

    @staticmethod
    def _size_to_gb(size: str) -> int:
        """Normalize manifest sizes ('60G', '100GB', '107374182400') to int GB."""
        s = size.strip().upper().rstrip("B")
        if s.endswith("G"):
            return int(float(s[:-1]))
        if s.endswith("T"):
            return int(float(s[:-1]) * 1024)
        if s.endswith("M"):
            return max(1, int(float(s[:-1]) / 1024))
        # Plain integer = bytes
        return max(1, int(int(s) / (1024**3)))

    @staticmethod
    def _mac_from_net0(net0: str) -> str | None:
        # Examples: 'virtio=BC:24:11:..,bridge=vmbr0', 'virtio,bridge=vmbr0,macaddr=BC:..'
        for token in net0.split(","):
            m = re.match(r"^(?:virtio|e1000|rtl8139)=([0-9A-Fa-f:]{17})$", token)
            if m:
                return m.group(1)
            m = re.match(r"^macaddr=([0-9A-Fa-f:]{17})$", token)
            if m:
                return m.group(1)
        return None
