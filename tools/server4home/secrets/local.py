"""Local secret provider.

Reads a flat YAML map of `name: value` from a gitignored file on the
workstation. Default location: `secrets/secrets.yaml` relative to the
current directory; override with $S4H_SECRETS_FILE.

Example secrets/secrets.yaml:

    "k3s/homelab/agent-token": "K10abc...::server:def..."
    "rancher/admin-password":  "s3cr3t"

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
        self._store: dict[str, str] = {}
        self._loaded = False

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
        # Coerce everything to str; YAML may parse numeric-looking tokens.
        self._store = {str(k): str(v) for k, v in data.items()}
        log.info("Loaded %d secret(s) from %s", len(self._store), self.path)

    def get(self, name: str) -> str:
        self._load()
        # 1) Look up in secrets.yaml first (short strings: tokens, passwords).
        if name in self._store:
            return self._store[name]
        # 2) Fall back to a file under the secret store's root. The name is
        #    treated as a path relative to that root, so a manifest reference
        #    like {secret: "tls/rancher.crt"} reads secrets/tls/rancher.crt
        #    on disk. Path traversal outside the root is rejected.
        root = self.path.parent.resolve()
        candidate = (root / name).resolve()
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
        raise SecretNotFound(
            f"secret '{name}' not found (looked in {self.path} and {candidate}). "
            f"Add it to the YAML store or drop a file at the path shown."
        )
