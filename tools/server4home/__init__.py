"""server4home — manifest-driven VM deployment with a plugin architecture.

Importing this package triggers registration of every built-in plugin (targets,
provisioners, installers). Third-party plugins should import the relevant
registry from `server4home.registry` and decorate their classes; the plugin
becomes visible the moment its module is imported anywhere in the process.
"""

# Importing these submodules wires the decorators @{registry}.register(...)
# in each concrete plugin module. Order doesn't matter — the registries are
# just dicts populated at import time.
from . import targets        # noqa: F401  (side-effect: register targets)
from . import provisioners   # noqa: F401  (side-effect: register provisioners)
from . import installers     # noqa: F401  (side-effect: register installers)

__version__ = "0.1.0"
