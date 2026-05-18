"""Infrastructure helpers: logging, subprocess, ssh, helm.

Single module to keep navigation simple; split into a `util/` subpackage when
files cross ~300 lines.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Sequence


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def get_logger(name: str = "server4home") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(
            logging.Formatter("\033[1;34m[%(name)s]\033[0m %(message)s")
        )
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger


log = get_logger()


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #
class CommandError(RuntimeError):
    def __init__(self, cmd: Sequence[str], rc: int, stdout: str, stderr: str):
        self.cmd = list(cmd)
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"command failed (rc={rc}): {shlex.join(self.cmd)}\nSTDERR:\n{stderr}"
        )


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess:
    """Run a subprocess; pretty-log it; raise CommandError on non-zero."""
    log.info("$ %s", shlex.join(cmd))
    proc = subprocess.run(
        list(cmd),
        check=False,
        capture_output=capture,
        text=True,
        input=input_text,
        env={**os.environ, **(env or {})} if env else None,
        cwd=str(cwd) if cwd else None,
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout or "", proc.stderr or "")
    return proc


def require_tool(name: str, hint: str = "") -> None:
    if shutil.which(name) is None:
        msg = f"required tool not found on PATH: {name}"
        if hint:
            msg += f" ({hint})"
        raise RuntimeError(msg)


# --------------------------------------------------------------------------- #
# SSH / SCP wrappers
# --------------------------------------------------------------------------- #
class SSH:
    """Light wrapper around `ssh` / `scp` to a target VM."""

    def __init__(self, host: str, user: str = "developer",
                 key: str | Path | None = None) -> None:
        self.host = host
        self.user = user
        self.key = Path(key) if key else Path.home() / ".ssh" / "id_ed25519"

    @property
    def _base(self) -> list[str]:
        return [
            "ssh",
            "-i", str(self.key),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=" + str(Path.home() / ".ssh" / "known_hosts"),
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
        ]

    def reachable(self, timeout: int = 3) -> bool:
        cmd = self._base + ["-o", f"ConnectTimeout={timeout}",
                            f"{self.user}@{self.host}", "true"]
        return subprocess.run(cmd, capture_output=True).returncode == 0

    def wait_reachable(self, attempts: int = 60, delay: float = 5.0) -> None:
        log.info("Waiting for SSH on %s@%s", self.user, self.host)
        for _ in range(attempts):
            if self.reachable():
                log.info("SSH reachable")
                return
            time.sleep(delay)
        raise TimeoutError(f"SSH on {self.host} not reachable within "
                           f"{attempts * delay:.0f}s")

    def run(self, remote_cmd: str, *, sudo: bool = False,
            check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
        if sudo:
            remote_cmd = "sudo " + remote_cmd
        cmd = self._base + [f"{self.user}@{self.host}", remote_cmd]
        return run(cmd, check=check, capture=capture)

    def stdout(self, remote_cmd: str, *, sudo: bool = False) -> str:
        p = self.run(remote_cmd, sudo=sudo, capture=True)
        return p.stdout

    def put_text(self, content: str, remote_path: str, *,
                 mode: str = "0644") -> None:
        """Write a string to a file on the remote host (root-owned)."""
        # Heredoc via stdin so we don't fight with quoting in the SSH command.
        script = (
            f"sudo install -d -m 0755 \"$(dirname {shlex.quote(remote_path)})\" && "
            f"sudo install -m {mode} /dev/stdin {shlex.quote(remote_path)}"
        )
        cmd = self._base + [f"{self.user}@{self.host}", script]
        log.info("$ ssh ... %s  (stdin=%d bytes)", script, len(content))
        proc = subprocess.run(cmd, input=content, text=True, capture_output=True)
        if proc.returncode != 0:
            raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)


# --------------------------------------------------------------------------- #
# Helm wrapper
# --------------------------------------------------------------------------- #
class Helm:
    def __init__(self, kubeconfig: Path) -> None:
        require_tool("helm", "install via `dnf install helm` or your package manager")
        require_tool("kubectl", "install via your package manager")
        self.kubeconfig = Path(kubeconfig)

    def _base(self) -> list[str]:
        return ["helm", "--kubeconfig", str(self.kubeconfig)]

    def repo_add(self, name: str, url: str) -> None:
        run(self._base() + ["repo", "add", name, url, "--force-update"], capture=True)

    def repo_update(self, *names: str) -> None:
        run(self._base() + ["repo", "update", *names], capture=True)

    def upgrade_install(
        self,
        release: str,
        chart: str,
        *,
        namespace: str,
        version: str | None = None,
        values_file: Path | None = None,
        set_flags: dict[str, str] | None = None,
        timeout: str = "20m",
        create_namespace: bool = True,
        wait: bool = True,
    ) -> None:
        cmd = self._base() + [
            "upgrade", "--install", release, chart,
            "--namespace", namespace,
        ]
        if create_namespace:
            cmd.append("--create-namespace")
        if version:
            cmd += ["--version", version]
        if values_file is not None:
            cmd += ["-f", str(values_file)]
        for k, v in (set_flags or {}).items():
            cmd += ["--set", f"{k}={v}"]
        if wait:
            cmd += ["--wait", "--timeout", timeout]
        run(cmd)
