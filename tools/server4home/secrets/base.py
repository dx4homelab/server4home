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

    def bind_hostname(self, hostname: str | None) -> None:
        """Optionally narrow subsequent get() calls to a manifest's hostname.

        Providers MAY use this to implement per-host overlays — a YAML
        section named after the hostname (or a filesystem subdir) that
        shadows the global namespace. The default is a no-op: providers
        without a hostname overlay simply ignore the hint.

        Pass `None` to clear the binding (subsequent get()s see only the
        global namespace again). The runner calls bind_hostname() once
        before resolving each manifest's references.
        """
