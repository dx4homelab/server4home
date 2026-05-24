"""Click-based CLI entrypoint for `server4home`."""

from __future__ import annotations

import sys

import click

from . import history, runner
from .manifest import Manifest
from .registry import (
    installers,
    ip_provisioners,
    mac_provisioners,
    secret_providers,
    targets,
)
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
@click.option("--wipe-data", is_flag=True, default=False,
              help="Drop the preserved per-VM data disk + identity meta "
                   "before deploying. Use when the cluster identity has "
                   "changed (different hostname); otherwise the deploy "
                   "refuses on identity mismatch.")
def deploy_cmd(manifest_path: str, ssh_user: str | None,
               ssh_key: str | None, kubeconfig_dir: str,
               wipe_data: bool) -> None:
    """Provision a VM and apply all install: entries from MANIFEST_PATH."""
    manifest = Manifest.load(manifest_path)
    runner.deploy(
        manifest,
        ssh_user=ssh_user, ssh_key=ssh_key,
        kubeconfig_dir=kubeconfig_dir,
        wipe_data=wipe_data,
    )


@cli.command("apply")
@click.argument("manifest_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--kubeconfig", default=None, type=click.Path(dir_okay=False),
              help="Path to the kubeconfig (default: kubeconfigs/<hostname>.kubeconfig).")
@click.option("--kubeconfig-dir", default="./kubeconfigs",
              show_default=True, type=click.Path(file_okay=False),
              help="Directory holding fetched kubeconfigs (only used if --kubeconfig is not set).")
@click.option("--only", "only_csv", default=None,
              help="Comma-separated installer names to reconcile (skip everything else).")
@click.option("--skip", "skip_csv", default=None,
              help="Comma-separated installer names to skip.")
def apply_cmd(manifest_path: str, kubeconfig: str | None,
              kubeconfig_dir: str, only_csv: str | None,
              skip_csv: str | None) -> None:
    """Reconcile installers against an EXISTING cluster.

    Use this to bump helm-chart versions after editing a manifest, without
    re-creating the VM. Installers that require a fresh node (today: k3s)
    are skipped automatically.
    """
    manifest = Manifest.load(manifest_path)
    only = [s.strip() for s in only_csv.split(",") if s.strip()] if only_csv else None
    skip = [s.strip() for s in skip_csv.split(",") if s.strip()] if skip_csv else None
    runner.apply(
        manifest,
        kubeconfig=kubeconfig,
        kubeconfig_dir=kubeconfig_dir,
        only=only,
        skip=skip,
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


@cli.group("history")
def history_group() -> None:
    """Inspect or re-render the deployment-history ledger."""


@history_group.command("render")
@click.option("--deploy-dir", default="deployments", show_default=True,
              type=click.Path(file_okay=False),
              help="Directory containing per-event JSON files.")
@click.option("--out", "out_path", default="docs/deployment-history.md",
              show_default=True, type=click.Path(dir_okay=False),
              help="Markdown file to write.")
def history_render_cmd(deploy_dir: str, out_path: str) -> None:
    """Regenerate the markdown history from the JSON ledger."""
    history.render(deploy_dir=deploy_dir, out_path=out_path)


@history_group.command("check")
@click.option("--deploy-dir", default="deployments", show_default=True,
              type=click.Path(file_okay=False))
@click.option("--out", "out_path", default="docs/deployment-history.md",
              show_default=True, type=click.Path(dir_okay=False))
def history_check_cmd(deploy_dir: str, out_path: str) -> None:
    """Exit non-zero if the rendered markdown is stale w.r.t. the JSON ledger.

    Use in CI: a PR that adds a deployments/*.json file must also include
    a re-rendered docs/deployment-history.md.
    """
    if not history.check(deploy_dir=deploy_dir, out_path=out_path):
        click.echo(
            f"{out_path} is out of sync with {deploy_dir}/*.json — "
            "run `just history` and commit the result.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"{out_path} is up to date.")


@cli.command("list-plugins")
def list_plugins_cmd() -> None:
    """Show every registered plugin grouped by extension point."""
    rows: list[tuple[str, list[str]]] = [
        ("target",          targets.keys()),
        ("mac-provisioner", mac_provisioners.keys()),
        ("ip-provisioner",  ip_provisioners.keys()),
        ("installer",       installers.keys()),
        ("secret-provider", secret_providers.keys()),
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
