"""Secret provider interface.

A SecretProvider maps an opaque secret name to its value. The `local`
provider reads from a gitignored file on the workstation; a future `ifra`
provider will fetch from the homelab inventory API — same interface, so
manifests never change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SecretNotFound(KeyError):
    """Raised when a referenced secret cannot be resolved."""


class SecretProvider(ABC):
    @abstractmethod
    def get(self, name: str) -> str:
        """Return the secret value for `name`, or raise SecretNotFound."""
