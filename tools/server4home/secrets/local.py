"""Local secret provider.

Reads a YAML map of `name: value` (and optional per-hostname overlays)
from a gitignored file on the workstation. Default location:
`secrets/secrets.yaml` relative to the current directory; override with
$S4H_SECRETS_FILE.

Example secrets/secrets.yaml:

    # ---- Global / shared across all VMs ----
    "proxmox/api-token":       "PVEAPIToken=root@pam!deploy=..."
    "k3s/homelab/node-token":  "K10abc...::server:def..."

    # ---- Per-hostname overlays (narrowed by manifest.hostname) ----
    k3s-rancher-on-ucore-pve-vm:
      "rancher/admin-password": "<unique-to-prod>"

    k3s-on-virt-manager:
      "rancher/admin-password": "<unique-to-spare>"

Lookup order when the runner has bound a hostname:
  1. data[<hostname>][<name>]          — YAML per-host overlay
  2. data[<name>]                      — YAML global
  3. secrets/<hostname>/<name> on disk — filesystem per-host overlay
  4. secrets/<name>          on disk   — filesystem global (PEMs etc.)

This file must NEVER be committed — see .gitignore. The committed
`secrets/secrets.example.yaml` documents the format.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from ..registry import secret_providers
from ..util import log
from .base import SecretNotFound, SecretProvider

DEFAULT_PATH = "secrets/secrets.yaml"


@secret_providers.register("local")
class LocalSecretProvider(SecretProvider):
    def __init__(self) -> None:
        self.path = Path(os.environ.get("S4H_SECRETS_FILE", DEFAULT_PATH))
        # Globals: top-level scalar entries.
        self._globals: dict[str, str] = {}
        # Per-host overlays: top-level entries whose value is a mapping.
        self._namespaces: dict[str, dict[str, str]] = {}
        self._loaded = False
        self._hostname: str | None = None

    def bind_hostname(self, hostname: str | None) -> None:
        self._hostname = hostname

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.is_file():
            log.warning(
                "local secret store not found at %s — secret references "
                "will fail. Copy secrets/secrets.example.yaml and fill it in.",
                self.path,
            )
            return
        with self.path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{self.path}: expected a top-level mapping")
        # Split: dict-valued top-level entries are per-host overlays;
        # everything else is a global scalar. This is unambiguous given the
        # convention that secret names are paths (string-valued at the leaf).
        for key, value in data.items():
            if isinstance(value, dict):
                self._namespaces[str(key)] = {
                    str(k): str(v) for k, v in value.items()
                }
            else:
                self._globals[str(key)] = str(value)
        log.info(
            "Loaded %d global secret(s) and %d per-host overlay(s) from %s",
            len(self._globals), len(self._namespaces), self.path,
        )

    def get(self, name: str) -> str:
        self._load()
        host = self._hostname

        # 1) YAML per-host overlay
        if host and host in self._namespaces and name in self._namespaces[host]:
            return self._namespaces[host][name]
        # 2) YAML global
        if name in self._globals:
            return self._globals[name]

        # 3-4) Filesystem fallback. The name is treated as a path relative to
        #      the secret store root. Per-host overlay first, then global.
        #      Path traversal outside the root is rejected.
        root = self.path.parent.resolve()
        candidates: list[Path] = []
        if host:
            candidates.append((root / host / name).resolve())
        candidates.append((root / name).resolve())

        for candidate in candidates:
            try:
                candidate.relative_to(root)
            except ValueError:
                raise SecretNotFound(
                    f"secret name '{name}' escapes the secret store root ({root})"
                ) from None
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8")
                except OSError as e:
                    raise SecretNotFound(
                        f"could not read secret file {candidate}: {e}"
                    ) from e

        looked = [str(self.path)] + [str(c) for c in candidates]
        raise SecretNotFound(
            f"secret '{name}' not found (host={host!r}); looked in: "
            + ", ".join(looked)
            + ". Add it to the YAML store (global or per-host) or drop a file at one of the paths shown."
        )
