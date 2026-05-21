"""Secret provider plugins.

A secret provider resolves a named secret (e.g. a K3s join token) to its
value at deploy time, so manifests can reference secrets by name and never
carry the literal value into git.
"""

from . import local  # noqa: F401  (registers "local")
