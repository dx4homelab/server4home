"""Target plugins (deployment destinations).

Each module imported below registers exactly one concrete Target subclass
with `server4home.registry.targets`. Adding a new target = drop a module
here and import it.
"""

from . import local_virt_manager  # noqa: F401  (registers "local-virt-manager")
from . import pve9                # noqa: F401  (registers "pve9")
