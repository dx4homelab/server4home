"""Click-based CLI entrypoint for `server4home`."""

from __future__ import annotations

import sys

import click

from . import runner
from .manifest import Manifest
from .registry import installers, ip_provisioners, mac_provisioners, targets
from .util import log


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="server4home")
def cli() -> None:
    """Deploy server4home VMs from declarative YAML manifests."""


@cli.command("deploy")
@click.argument("manifest_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--ssh-user", default=None,
              help="SSH user inside the VM (default: developer, or $SSH_USER).")
@click.option("--ssh-key", default=None,
              help="SSH private key (default: ~/.ssh/id_ed25519, or $SSH_KEY).")
@click.option("--kubeconfig-dir", default="./kubeconfigs",
              show_default=True, type=click.Path(file_okay=False),
              help="Directory to store the fetched kubeconfig.")
def deploy_cmd(manifest_path: str, ssh_user: str | None,
               ssh_key: str | None, kubeconfig_dir: str) -> None:
    """Provision a VM and apply all install: entries from MANIFEST_PATH."""
    manifest = Manifest.load(manifest_path)
    runner.deploy(
        manifest,
        ssh_user=ssh_user, ssh_key=ssh_key,
        kubeconfig_dir=kubeconfig_dir,
    )


@cli.command("destroy")
@click.argument("manifest_path", type=click.Path(exists=True, dir_okay=False))
@click.confirmation_option(prompt="Destroy the VM described by this manifest?")
def destroy_cmd(manifest_path: str) -> None:
    """Tear down the VM described by MANIFEST_PATH."""
    manifest = Manifest.load(manifest_path)
    runner.destroy(manifest)


@cli.command("validate")
@click.argument("manifest_path", type=click.Path(exists=True, dir_okay=False))
def validate_cmd(manifest_path: str) -> None:
    """Parse + validate a manifest. Prints the resolved model and exits 0 on success."""
    manifest = Manifest.load(manifest_path)
    click.echo(manifest.model_dump_json(indent=2))


@cli.command("list-plugins")
def list_plugins_cmd() -> None:
    """Show every registered plugin grouped by extension point."""
    rows: list[tuple[str, list[str]]] = [
        ("target",          targets.keys()),
        ("mac-provisioner", mac_provisioners.keys()),
        ("ip-provisioner",  ip_provisioners.keys()),
        ("installer",       installers.keys()),
    ]
    for kind, keys in rows:
        click.echo(f"{kind}:")
        for k in keys:
            click.echo(f"  - {k}")


def main() -> None:
    try:
        cli()
    except KeyboardInterrupt:
        log.error("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
