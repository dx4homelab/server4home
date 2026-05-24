"""Secret-reference resolution.

Anywhere in a manifest's free-form config, a value of the form

    { "secret": "<name>" }

is a *secret reference*. `resolve()` walks an arbitrary dict/list structure
and replaces every reference with the value returned by a SecretProvider.

Keeping references (not literals) in the manifest is what lets the manifest
stay in git: the literal token only ever exists on the workstation's
gitignored secret store (or, later, INFRA — the homelab Infrastructure
service that owns inventory, MAC reservation, and the pfSense API bridge).
"""

from __future__ import annotations

from typing import Any

from .secrets.base import SecretProvider


def is_secret_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value.keys()) == {"secret"}
        and isinstance(value["secret"], str)
    )


def resolve(obj: Any, provider: SecretProvider) -> Any:
    """Return a copy of `obj` with every secret reference resolved."""
    if is_secret_ref(obj):
        return provider.get(obj["secret"])
    if isinstance(obj, dict):
        return {k: resolve(v, provider) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve(v, provider) for v in obj]
    return obj


def has_secret_refs(obj: Any) -> bool:
    """True if `obj` contains at least one unresolved secret reference."""
    if is_secret_ref(obj):
        return True
    if isinstance(obj, dict):
        return any(has_secret_refs(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_secret_refs(v) for v in obj)
    return False
