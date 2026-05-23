"""Pydantic models for the instance YAML schema.

Mirrors the prototype at instances/k3s-on-virt-manager.yaml. Anything not
needed by today's plugins lives as a free-form dict so plugins can read their
own keys without schema churn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


# --------------------------------------------------------------------------- #
# Provisioner sub-models
# --------------------------------------------------------------------------- #
class MacSpec(BaseModel):
    """How to allocate the VM's MAC address."""

    model_config = ConfigDict(extra="allow")

    provisioner: Literal["default", "fixed", "ifra"] = "default"
    fixed: str | None = None  # used when provisioner == "fixed"

    @model_validator(mode="after")
    def _check(self) -> "MacSpec":
        if self.provisioner == "fixed" and not self.fixed:
            raise ValueError("mac.provisioner=fixed requires mac.fixed to be set")
        return self


class IpSpec(BaseModel):
    """How to assign the VM's primary-NIC IP."""

    model_config = ConfigDict(extra="allow")

    provisioner: Literal["dhcp", "static"] = "dhcp"
    static: str | None = None     # CIDR notation, e.g. 192.168.120.50/16
    gateway: str | None = None    # only meaningful for static
    dns: str | None = None        # comma- or pipe-separated for multiple

    @model_validator(mode="after")
    def _check(self) -> "IpSpec":
        if self.provisioner == "static" and not self.static:
            raise ValueError("ip.provisioner=static requires ip.static to be set (CIDR)")
        return self


# --------------------------------------------------------------------------- #
# Network / Disk / Install
# --------------------------------------------------------------------------- #
class NetworkSpec(BaseModel):
    """A single NIC. v1 wires only the first entry into the VM."""

    model_config = ConfigDict(extra="allow")

    name: str = "default"
    type: Literal["bridge"] = "bridge"
    mac: MacSpec = Field(default_factory=MacSpec)
    ip: IpSpec = Field(default_factory=IpSpec)


class DiskSpec(BaseModel):
    """A data disk attached to the VM.

    v1: only `path: /var/lib/rancher` with `type: lvm` is honored. Other paths
    are accepted but skipped (with a warning) so the manifest can grow.
    """

    model_config = ConfigDict(extra="allow")

    path: str
    size: str
    type: Literal["lvm"] = "lvm"


class InstallSpec(BaseModel):
    """A workload to apply to the cluster after K3s is up.

    `name` selects the installer plugin (e.g. "k3s", "rancher-manager",
    "cert-manager"). The rest of the keys (version, args, config, …) are
    plugin-specific and stored as a free-form dict.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    version: str | None = None
    args: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    memory: int = 16384     # MiB
    vcpus: int = 4


class ProxmoxConfig(BaseModel):
    """PVE-target-specific knobs. Only honored when `target: pve9`.

    Keeping these in a typed sub-model (rather than free-form extras) means
    Pydantic catches typos (`vmdid: 70020`) — a silent fallthrough to
    /cluster/nextid would otherwise be very confusing on the next redeploy.
    """

    model_config = ConfigDict(extra="forbid")

    # Pin a specific VMID for this VM (e.g. for grouped numbering schemes
    # like 70020 = k8s/k3s control planes). When omitted: the target looks
    # up an existing VM with the same name first, then falls back to
    # Proxmox's /cluster/nextid auto-allocator.
    vmid: int | None = Field(default=None, ge=100, le=999_999_999)


# --------------------------------------------------------------------------- #
# Top-level
# --------------------------------------------------------------------------- #
class Manifest(BaseModel):
    """The full instance manifest."""

    model_config = ConfigDict(extra="allow")

    base: str = "k3s-base"
    hostname: str
    target: str   # validated against the registry, not a Literal
    secret_provider: str = "local"   # which secret-provider plugin to use
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    disks: list[DiskSpec] = Field(default_factory=list)
    network: list[NetworkSpec] = Field(default_factory=list)
    install: list[InstallSpec] = Field(default_factory=list)
    proxmox: ProxmoxConfig | None = None   # only honored when target == "pve9"

    # Resolved file path (set after loading); not part of the YAML itself.
    source_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        p = Path(path)
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        try:
            m = cls.model_validate(data)
        except ValidationError as e:
            raise ValueError(f"manifest validation failed: {e}") from e
        m.source_path = p
        return m

    @property
    def primary_network(self) -> NetworkSpec:
        if not self.network:
            return NetworkSpec()
        return self.network[0]

    def installer_entries(self) -> list[InstallSpec]:
        return list(self.install)

    def image_ref(self) -> str:
        """Map manifest `base:` to the local container image tag."""
        mapping = {
            "k3s-base": "server4home-k3s",
        }
        return mapping.get(self.base, self.base)

    def k3s_install(self) -> InstallSpec | None:
        """The `install:` entry named 'k3s', if any."""
        for entry in self.install:
            if entry.name == "k3s":
                return entry
        return None

    def k3s_mode(self) -> str:
        """'server' (default) or 'agent', from the k3s install entry's config."""
        k3s = self.k3s_install()
        if k3s is None:
            return "server"
        mode = k3s.config.get("mode", "server")
        if mode not in ("server", "agent"):
            raise ValueError(f"install[k3s].config.mode must be server|agent, got '{mode}'")
        return mode

    def k3s_datastore(self) -> str:
        """'etcd' (default) or 'sqlite', from the k3s install entry's config.

        A new-cluster server defaults to embedded etcd — HA-capable, and the
        datastore choice can't be changed in place later. 'sqlite' is the
        deliberate single-node opt-out.
        """
        k3s = self.k3s_install()
        ds = "etcd" if k3s is None else k3s.config.get("datastore", "etcd")
        if ds not in ("etcd", "sqlite"):
            raise ValueError(f"install[k3s].config.datastore must be etcd|sqlite, got '{ds}'")
        return ds

    def k3s_join(self) -> dict[str, str]:
        """Join parameters for the k3s entry: {mode, server, token} (resolved).

        Returns only the keys that are set. `server`/`token` are present when
        joining an existing cluster. Secret references must already have been
        resolved (see secretref.resolve).
        """
        k3s = self.k3s_install()
        if k3s is None:
            return {}
        cfg = k3s.config
        out: dict[str, str] = {"mode": self.k3s_mode()}
        if cfg.get("server"):
            out["server"] = str(cfg["server"])
        if cfg.get("token"):
            out["token"] = str(cfg["token"])
        return out
