"""Deployment history — JSON event ledger + markdown renderer.

Each `server4home deploy` (and, in the future, `apply` / `destroy`) writes a
single JSON event file to `deployments/` capturing what happened: which
manifest at which git SHA, which target, which VM IP/vmid, which installer
versions, success or failure (and at which step). The files are checked in
to git — that's the durable history. A separate renderer walks the JSON
files and produces `docs/deployment-history.md`, which is also checked in
so PR diffs show what changed and operators can grep it offline.

Design notes (see docs/deployment-history.md for the operator-facing view):

* Schema versioning from day one (`schema_version: 1`). Future field
  additions are additive; field removals or type changes bump the version.

* One file per event, timestamped filename. Append-only. No merge conflicts
  because two events can't share a UTC ISO timestamp + hostname.

* Failures are first-class. The recorder context manager wraps the runner's
  body; an exception is captured with `outcome: "failure"`, `failed_step:`
  set to whichever phase was in flight, and a one-line `error:` summary.
  Stack traces stay in the runner's logs, not in the ledger.

* No secret values are ever serialized. We record installer `name` and
  `version` only — never `config` — because the runner resolves
  `{ secret: ... }` references in-place into the config dict.

* The runner does NOT `git add && git commit` automatically. It prints a
  one-line nudge with the file to commit. Auto-committing from a tool that
  is also editing the manifest mid-iteration would create messy histories.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .manifest import Manifest
from .util import log

SCHEMA_VERSION = 1
DEFAULT_DIR = Path("deployments")
DEFAULT_RENDERED = Path("docs/deployment-history.md")


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #
@dataclass
class _InstallerRow:
    name: str
    version: str | None
    outcome: str = "pending"   # pending | success | failure | skipped


@dataclass
class DeploymentRecorder:
    """Mutable, in-flight event. Use via the `record()` context manager."""

    manifest: Manifest
    kind: str                                    # "deploy" | "apply" | "destroy"
    started_at: float = field(default_factory=time.time)
    vm: dict[str, Any] = field(default_factory=dict)
    image: dict[str, Any] = field(default_factory=dict)
    installers: list[_InstallerRow] = field(default_factory=list)
    outcome: str = "in-progress"                 # in-progress | success | failure | skipped
    failed_step: str | None = None
    error: str | None = None
    out_dir: Path = DEFAULT_DIR

    # ---- mutation helpers ----
    def set_vm(self, **fields: Any) -> None:
        self.vm.update({k: v for k, v in fields.items() if v is not None})

    def set_image(self, **fields: Any) -> None:
        self.image.update({k: v for k, v in fields.items() if v is not None})

    def installer_start(self, name: str, version: str | None = None) -> None:
        self.installers.append(_InstallerRow(name=name, version=version))

    def installer_ok(self) -> None:
        if self.installers:
            self.installers[-1].outcome = "success"

    def installer_skipped(self) -> None:
        if self.installers:
            self.installers[-1].outcome = "skipped"

    def installer_failed(self) -> None:
        if self.installers:
            self.installers[-1].outcome = "failure"

    def fail(self, *, step: str, error: str) -> None:
        self.outcome = "failure"
        self.failed_step = step
        # Trim the error to a single line — full traces belong in runner logs.
        first_line = error.strip().splitlines()[0] if error.strip() else ""
        self.error = first_line[:500]

    # ---- serialization ----
    def to_dict(self) -> dict[str, Any]:
        manifest_path = (self.manifest.source_path or Path("<unknown>"))
        # Resolve relative to repo root if we're inside one; harmless otherwise.
        try:
            manifest_path = manifest_path.relative_to(Path.cwd())
        except (ValueError, OSError):
            pass

        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind,
            "timestamp": _utc_iso(self.started_at),
            "duration_seconds": round(time.time() - self.started_at, 1),
            "operator": _operator(),
            "manifest": {
                "path": str(manifest_path),
                "git_sha": _git_sha_for(manifest_path),
                "dirty": _git_dirty_for(manifest_path),
            },
            "hostname": self.manifest.hostname,
            "target": self.manifest.target,
            "k3s_mode": self.manifest.k3s_mode(),
            "vm": self.vm,
            "image": {
                "ref": self.image.get("ref")
                or f"ghcr.io/dx4homelab/{self.manifest.image_ref()}:stable",
                "digest": self.image.get("digest"),
                "k3s_version": self.image.get("k3s_version"),
            },
            "installers": [
                {"name": r.name, "version": r.version, "outcome": r.outcome}
                for r in self.installers
            ],
            "outcome": self.outcome,
            "failed_step": self.failed_step,
            "error": self.error,
        }

    def write(self) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts = _safe_ts(self.started_at)
        path = self.out_dir / f"{ts}-{self.kind}-{self.manifest.hostname}.json"
        # Stable formatting for clean git diffs.
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        return path


class record:
    """Context manager wrapping a single deploy/apply/destroy event."""

    def __init__(self, manifest: Manifest, *, kind: str,
                 out_dir: Path | str = DEFAULT_DIR) -> None:
        self.rec = DeploymentRecorder(
            manifest=manifest, kind=kind, out_dir=Path(out_dir),
        )

    def __enter__(self) -> DeploymentRecorder:
        return self.rec

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None and self.rec.outcome == "in-progress":
            # Caller didn't classify the failure — mark the in-flight
            # installer (if any) failed, derive the step name from it.
            if self.rec.installers and self.rec.installers[-1].outcome == "pending":
                self.rec.installer_failed()
                step = self.rec.installers[-1].name
            else:
                step = "runner"
            self.rec.fail(step=step, error=f"{exc_type.__name__}: {exc}")
        elif exc_type is None and self.rec.outcome == "in-progress":
            self.rec.outcome = "success"
        path = self.rec.write()
        log.info("Deployment event recorded: %s", path)
        # Nudge — never auto-commit.
        log.info("  git add %s && git commit -m '<context>'  (and re-run `just history` to refresh the markdown)", path)
        return False  # never swallow the exception


# --------------------------------------------------------------------------- #
# Markdown renderer
# --------------------------------------------------------------------------- #
_OUTCOME_GLYPH = {
    "success": "✅",
    "failure": "❌",
    "skipped": "⏭",
    "in-progress": "⏳",
}


def render(deploy_dir: Path | str = DEFAULT_DIR,
           out_path: Path | str = DEFAULT_RENDERED) -> Path:
    """Re-render the markdown history from every JSON event in deploy_dir."""
    deploy_dir = Path(deploy_dir)
    out_path = Path(out_path)
    events = _load_events(deploy_dir)
    text = _render_markdown(events)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    log.info("Rendered %d event(s) -> %s", len(events), out_path)
    return out_path


def check(deploy_dir: Path | str = DEFAULT_DIR,
          out_path: Path | str = DEFAULT_RENDERED) -> bool:
    """Return True iff the rendered markdown is up-to-date."""
    out_path = Path(out_path)
    expected = _render_markdown(_load_events(Path(deploy_dir)))
    actual = out_path.read_text() if out_path.is_file() else ""
    return expected == actual


def _load_events(deploy_dir: Path) -> list[dict[str, Any]]:
    if not deploy_dir.is_dir():
        return []
    events: list[dict[str, Any]] = []
    for f in sorted(deploy_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            log.warning("Skipping malformed event %s: %s", f, e)
            continue
        if data.get("schema_version") != SCHEMA_VERSION:
            log.warning("Skipping event %s with unsupported schema_version=%s",
                        f, data.get("schema_version"))
            continue
        events.append(data)
    return events


def _render_markdown(events: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("# Deployment history")
    lines.append("")
    lines.append("> Auto-generated from `deployments/*.json` — **do not edit by hand**.")
    lines.append("> Regenerate with `just history` (or `server4home history render`).")
    lines.append("")

    if not events:
        lines.append("_No deployments recorded yet._")
        lines.append("")
        return "\n".join(lines)

    # ---- Latest per hostname (overview) ----
    latest: dict[str, dict[str, Any]] = {}
    for e in events:
        h = e["hostname"]
        if h not in latest or e["timestamp"] > latest[h]["timestamp"]:
            latest[h] = e

    lines.append("## Latest per VM")
    lines.append("")
    lines.append("| Hostname | Target | Last event | Outcome | K3s | Rancher | MetalLB |")
    lines.append("|----------|--------|------------|---------|-----|---------|---------|")
    for h in sorted(latest):
        e = latest[h]
        lines.append("| {host} | {target} | {kind} @ {ts} | {glyph} {oc} | {k3s} | {rancher} | {metallb} |".format(
            host=h,
            target=e["target"],
            kind=e["kind"],
            ts=_pretty_ts(e["timestamp"]),
            glyph=_OUTCOME_GLYPH.get(e["outcome"], "?"),
            oc=e["outcome"] + (f" @ {e['failed_step']}" if e["failed_step"] else ""),
            k3s=e["image"].get("k3s_version") or "—",
            rancher=_installer_version(e, "rancher-manager"),
            metallb=_installer_version(e, "metallb"),
        ))
    lines.append("")

    # ---- Full history per hostname ----
    by_host: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_host.setdefault(e["hostname"], []).append(e)

    for host in sorted(by_host):
        evs = sorted(by_host[host], key=lambda x: x["timestamp"], reverse=True)
        first = evs[0]
        lines.append(f"## {host}")
        lines.append("")
        lines.append(f"Target: `{first['target']}` · K3s mode: `{first['k3s_mode']}`")
        lines.append("")
        lines.append("| When | Kind | Outcome | K3s | MetalLB | Rancher | Manifest | Notes |")
        lines.append("|------|------|---------|-----|---------|---------|----------|-------|")
        for e in evs:
            notes_parts: list[str] = []
            if e.get("error"):
                notes_parts.append(e["error"])
            if e["manifest"].get("dirty"):
                notes_parts.append("manifest dirty at deploy")
            notes = "; ".join(notes_parts) or ""
            lines.append("| {when} | {kind} | {glyph} {oc} | {k3s} | {metallb} | {rancher} | `{sha}` | {notes} |".format(
                when=_pretty_ts(e["timestamp"]),
                kind=e["kind"],
                glyph=_OUTCOME_GLYPH.get(e["outcome"], "?"),
                oc=e["outcome"] + (f" @ {e['failed_step']}" if e["failed_step"] else ""),
                k3s=e["image"].get("k3s_version") or "—",
                metallb=_installer_version(e, "metallb"),
                rancher=_installer_version(e, "rancher-manager"),
                sha=(e["manifest"].get("git_sha") or "—"),
                notes=_md_escape(notes),
            ))
        lines.append("")

    return "\n".join(lines)


def _installer_version(event: dict[str, Any], name: str) -> str:
    for row in event.get("installers", []):
        if row["name"] == name:
            return row.get("version") or "—"
    return "—"


def _pretty_ts(iso: str) -> str:
    # ISO 8601 'YYYY-MM-DDTHH:MM:SSZ' -> 'YYYY-MM-DD HH:MM UTC'
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):\d{2}Z$", iso)
    return f"{m.group(1)} {m.group(2)} UTC" if m else iso


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_ts(ts: float) -> str:
    # Filename-safe: colons replaced.
    return _utc_iso(ts).replace(":", "-")


def _operator() -> str:
    # Prefer git config user.email; fall back to $USER@hostname.
    email = _git("config", "user.email")
    if email:
        return email
    return f"{os.environ.get('USER', 'unknown')}@{os.uname().nodename}"


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], check=False, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_sha_for(_path: Path) -> str | None:
    return _git("rev-parse", "--short", "HEAD")


def _git_dirty_for(path: Path) -> bool:
    # Specifically: is THIS manifest file dirty relative to HEAD?
    if not _git("rev-parse", "--git-dir"):
        return False
    out = _git("status", "--porcelain", "--", str(path))
    return bool(out)
