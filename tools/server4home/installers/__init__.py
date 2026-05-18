"""Installer plugins (workloads to apply after the cluster is up)."""

from . import k3s              # noqa: F401  (registers "k3s")
from . import cert_manager     # noqa: F401  (registers "cert-manager")
from . import rancher_manager  # noqa: F401  (registers "rancher-manager")
