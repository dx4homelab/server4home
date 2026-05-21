"""Plugin registries.

Four registries exist (one per extension point): `targets`, `mac_provisioners`,
`ip_provisioners`, `installers`. Concrete plugin classes register themselves
with the matching registry via a decorator at import time:

    from server4home.registry import targets

    @targets.register("local-virt-manager")
    class LocalVirtManager(Target):
        ...

Adding a new plugin is purely additive: drop a module under the right
subpackage, decorate the class, and import the module from that subpackage's
__init__.py. The registries themselves never need editing.
"""

from __future__ import annotations

from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, type[T]] = {}

    def register(self, key: str) -> Callable[[type[T]], type[T]]:
        def decorator(cls: type[T]) -> type[T]:
            if key in self._items:
                raise ValueError(
                    f"{self.kind} '{key}' is already registered "
                    f"(by {self._items[key].__module__}.{self._items[key].__name__})"
                )
            self._items[key] = cls
            return cls

        return decorator

    def get(self, key: str) -> type[T]:
        try:
            return self._items[key]
        except KeyError:
            raise KeyError(
                f"no {self.kind} plugin registered for '{key}'. "
                f"Available: {sorted(self._items)}"
            ) from None

    def keys(self) -> list[str]:
        return sorted(self._items)

    def __contains__(self, key: str) -> bool:
        return key in self._items


# Module-level registries. Importing this module gives any plugin a stable
# place to register against (decorator targets) even before the typed base
# classes are loaded.
targets: "Registry" = Registry("target")
mac_provisioners: "Registry" = Registry("mac-provisioner")
ip_provisioners: "Registry" = Registry("ip-provisioner")
installers: "Registry" = Registry("installer")
secret_providers: "Registry" = Registry("secret-provider")
