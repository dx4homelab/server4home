"""kubernetes-secret installer.

Creates a Kubernetes Secret (Opaque or kubernetes.io/tls) from manifest
config — typically a pre-staging step for charts that consume secrets by
name (e.g. Rancher's `ingress.tls.source=secret` + `privateCA=true`).

Manifest shape:

    install:
      - name: kubernetes-secret
        config:
          name: tls-rancher-ingress
          namespace: cattle-system
          type: tls                                 # tls | opaque | <full k8s type>
          data:
            tls.crt: { secret: "tls/rancher.crt" }
            tls.key: { secret: "tls/rancher.key" }

Secret references inside `data` have already been resolved by the runner
(see secretref.resolve), so the values arriving here are literal strings.
The namespace is created idempotently before the Secret is applied, so this
installer can run before a Helm chart that would otherwise create it.
"""

from __future__ import annotations

import base64
from typing import Any

import yaml

from ..manifest import InstallSpec
from ..registry import installers
from ..util import log, require_tool, run
from .base import InstallContext, Installer

# Type aliases accepted in `config.type` for convenience.
_TYPE_ALIASES = {
    "tls": "kubernetes.io/tls",
    "opaque": "Opaque",
    "basic-auth": "kubernetes.io/basic-auth",
    "ssh-auth": "kubernetes.io/ssh-auth",
    "dockerconfigjson": "kubernetes.io/dockerconfigjson",
}


@installers.register("kubernetes-secret")
class KubernetesSecretInstaller(Installer):
    """Applies a single Kubernetes Secret via `kubectl apply`."""

    def apply(self, ctx: InstallContext, entry: InstallSpec) -> None:
        require_tool("kubectl", "needed to apply the Secret to the cluster")
        assert ctx.kubeconfig is not None, "kubernetes-secret needs a kubeconfig"

        cfg = entry.config
        name      = self._required(cfg, "name")
        namespace = cfg.get("namespace", "default")
        raw_type  = cfg.get("type", "opaque")
        k8s_type  = _TYPE_ALIASES.get(str(raw_type).lower(), str(raw_type))
        data      = cfg.get("data") or {}
        if not isinstance(data, dict) or not data:
            raise ValueError(
                f"install[kubernetes-secret]: 'data' must be a non-empty mapping "
                f"(got {type(data).__name__})"
            )

        # All data values must be strings at this point (secretref.resolve
        # already turned references into literals).
        encoded: dict[str, str] = {}
        for k, v in data.items():
            if not isinstance(v, str):
                raise ValueError(
                    f"install[kubernetes-secret]: data['{k}'] must be a string, "
                    f"got {type(v).__name__} — check that any secret reference "
                    f"was resolvable."
                )
            encoded[k] = base64.b64encode(v.encode("utf-8")).decode("ascii")

        kubeconfig = str(ctx.kubeconfig)

        # 1) Ensure the namespace exists. `kubectl apply -f -` is idempotent.
        ns_yaml = yaml.safe_dump({
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace},
        }, sort_keys=False)
        log.info("Ensuring namespace '%s' exists", namespace)
        run(
            ["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", "-"],
            input_text=ns_yaml,
            quiet=True,
        )

        # 2) Apply the Secret.
        secret_yaml = yaml.safe_dump({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": k8s_type,
            "data": encoded,
        }, sort_keys=False)
        log.info("Applying secret %s/%s (type=%s, keys=%s)",
                 namespace, name, k8s_type, sorted(encoded))
        run(
            ["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", "-"],
            input_text=secret_yaml,
        )

    @staticmethod
    def _required(cfg: dict[str, Any], key: str) -> str:
        v = cfg.get(key)
        if not v or not isinstance(v, str):
            raise ValueError(
                f"install[kubernetes-secret].config.{key} is required (string)"
            )
        return v
