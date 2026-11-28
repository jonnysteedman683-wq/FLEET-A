"""Run the integrator's verify command in a throwaway container.

The export the integrator verifies has no ``.git``, but the code under test is the attempt's own
code and could still walk the host filesystem by path. With a container engine (Podman, which runs
rootless, or Docker) the command runs in a fresh container that sees only the export, mounted at
``/work``, with networking off, every capability dropped and no host environment. The landing
repository, the worker clones and the other scratch directories are not mounted.

Both engines are free: Podman is open source, and Docker Engine is free on Linux. The image must
already hold everything the verify command needs (the interpreter, test libraries), because the
container has no network to install anything.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .errors import FleetError

log = logging.getLogger(__name__)

ENGINES = ("podman", "docker")
WORKDIR = "/work"


@dataclass(frozen=True)
class ContainerSandbox:
    engine: str  # the engine's executable: "podman", "docker" or a path to one
    image: str
    pids_limit: int = 1024

    @property
    def rootless_podman(self) -> bool:
        # keep-id exists only in rootless mode; Podman run as root is treated like Docker.
        return Path(self.engine).name == "podman" and os.geteuid() != 0

    def argv(self, command: Sequence[str], path: Path, name: str) -> list[str]:
        """The ``<engine> run`` command that runs ``command`` with only ``path`` mounted."""
        source = str(Path(path).resolve())
        if "," in source or ":" in source:
            raise FleetError(f"cannot mount {source} in a container: ',' and ':' are not allowed in the path")
        # Files the command writes in /work must stay removable by the fleet's own user.
        user = ["--userns=keep-id"] if self.rootless_podman else ["--user", f"{os.getuid()}:{os.getgid()}"]
        return [
            self.engine,
            "run",
            "--rm",
            "--name",
            name,
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--pids-limit={self.pids_limit}",
            *user,
            "--mount",
            f"type=bind,source={source},target={WORKDIR}",
            "--workdir",
            WORKDIR,
            self.image,
            *command,
        ]

    def wrap(self, command: Sequence[str], path: Path) -> tuple[list[str], Callable[[], None]]:
        """The wrapped command, and a cleanup that removes the container if it is still running.

        Killing the engine's client on a timeout does not always stop the container, so the
        caller runs the cleanup after a kill.
        """
        name = f"fleet-a-verify-{uuid.uuid4().hex[:12]}"

        def remove() -> None:
            subprocess.run(
                [self.engine, "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )

        return self.argv(command, path, name), remove


def _works(engine: str) -> bool:
    """True if ``engine`` is installed and can reach its daemon or runtime."""
    try:
        probe = subprocess.run(
            [engine, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def find_engine(choice: str) -> str | None:
    """The engine to use for ``choice`` ("auto", "podman" or "docker"), or None if none works.

    "auto" tries Podman first, because it runs without a root daemon, then Docker.
    """
    candidates = ENGINES if choice == "auto" else (choice,)
    for name in candidates:
        found = shutil.which(name)
        if found and _works(found):
            return found
    return None


def make_sandbox(image: str | None, choice: str) -> ContainerSandbox | None:
    """The sandbox the config asks for, or None to run verify directly (with a warning).

    An engine named explicitly that isn't available is an error. With "auto" and no engine
    found, verify runs as before and the warning says why.
    """
    if choice == "none":
        return None
    if not image:
        log.warning(
            "verify_command runs without a sandbox: set verify_image (and install Podman or Docker) "
            "to run it in a container that sees only the exported tree"
        )
        return None
    engine = find_engine(choice)
    if engine is None:
        if choice != "auto":
            raise FleetError(f"verify_sandbox is {choice!r} but {choice} is not installed or not running")
        log.warning(
            "verify_image is %s but neither Podman nor Docker is installed and running: "
            "verify_command runs without a sandbox",
            image,
        )
        return None
    log.info("verify_command runs in %s containers from %s", engine, image)
    return ContainerSandbox(engine, image)
