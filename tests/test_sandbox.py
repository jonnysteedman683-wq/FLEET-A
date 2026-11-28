"""The integrator's verify can run in a Podman or Docker container that sees only the export.

No real engine is needed: a fake ``podman``/``docker`` on PATH records each call and runs the
command in the mounted directory, so these tests check what FLEET-A asks the engine to do.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from conftest import config, git, plan_of, spec

from fleet_a.cli import build_fleet
from fleet_a.config import ConfigError, FleetConfig
from fleet_a.errors import FleetError
from fleet_a.executors import CallableExecutor
from fleet_a.model import MergeStatus
from fleet_a.sandbox import ContainerSandbox, find_engine, make_sandbox

from test_isolation import RUN, attempt, isolated

IMAGE = "fleet-test:1"

FAKE_ENGINE = '''#!{python}
import json, os, subprocess, sys
log = os.environ["FAKE_ENGINE_LOG"]
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0])] + args) + "\\n")
if args[:1] == ["info"]:
    sys.exit(0)
if args[:1] == ["rm"]:
    sys.exit(0)
assert args[0] == "run", args
mount = args[args.index("--mount") + 1]
source = dict(kv.split("=", 1) for kv in mount.split(","))["source"]
command = args[args.index("{image}") + 1:]
sys.exit(subprocess.call(command, cwd=source))
'''


@pytest.fixture
def engines(tmp_path, monkeypatch):
    """Install fake engines on PATH; returns (bin dir, a reader for the call log)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "engine.log"
    monkeypatch.setenv("FAKE_ENGINE_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    def install(*names: str) -> None:
        for name in names:
            exe = bin_dir / name
            exe.write_text(FAKE_ENGINE.format(python=sys.executable, image=IMAGE))
            exe.chmod(0o755)

    def calls() -> list[list[str]]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return install, calls


# ------------------------------------------------------------------------- the container


@pytest.mark.parametrize("engine", ["podman", "docker"])
def test_the_container_sees_only_the_export_with_no_network(tmp_path, monkeypatch, engine):
    monkeypatch.setattr("os.geteuid", lambda: 1000)  # rootless
    export = tmp_path / "export"
    export.mkdir()
    argv = ContainerSandbox(engine, IMAGE).argv(["pytest", "-q"], export, "n1")
    assert argv[:3] == [engine, "run", "--rm"]
    assert "--network=none" in argv and "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a in ("--mount", "-v", "--volume")]
    assert mounts == [f"type=bind,source={export.resolve()},target=/work"]
    assert argv[argv.index(IMAGE) + 1 :] == ["pytest", "-q"]
    assert not [a for a in argv if a.startswith(("--env", "-e", "--privileged"))]
    if engine == "podman":
        assert "--userns=keep-id" in argv  # rootless: files stay the fleet user's
    else:
        assert argv[argv.index("--user") + 1].count(":") == 1


def test_a_path_that_would_break_the_mount_option_is_refused(tmp_path):
    bad = tmp_path / "a,b"
    bad.mkdir()
    with pytest.raises(FleetError, match="cannot mount"):
        ContainerSandbox("podman", IMAGE).argv(["true"], bad, "n1")


# ----------------------------------------------------------------------------- choosing


def test_auto_prefers_podman_then_docker(engines):
    install, _ = engines
    assert find_engine("auto") is None
    install("docker")
    assert Path(find_engine("auto")).name == "docker"
    install("podman")
    assert Path(find_engine("auto")).name == "podman"
    assert Path(find_engine("docker")).name == "docker"


def test_without_an_image_verify_is_unsandboxed_and_says_so(engines, caplog):
    install, _ = engines
    install("podman")
    with caplog.at_level(logging.WARNING):
        assert make_sandbox(None, "auto") is None
    assert "verify_image" in caplog.text


def test_auto_with_no_engine_falls_back_with_a_warning(engines, caplog):
    with caplog.at_level(logging.WARNING):
        assert make_sandbox(IMAGE, "auto") is None
    assert "neither Podman nor Docker" in caplog.text


def test_a_named_engine_that_is_missing_is_an_error(engines):
    install, _ = engines
    install("podman")
    with pytest.raises(FleetError, match="docker is not installed"):
        make_sandbox(IMAGE, "docker")
    assert make_sandbox(IMAGE, "none") is None


def test_config_accepts_and_checks_the_sandbox_keys():
    conf = FleetConfig.from_dict({"verify_image": IMAGE, "verify_sandbox": "podman"})
    assert (conf.verify_image, conf.verify_sandbox) == (IMAGE, "podman")
    assert FleetConfig().verify_image is None and FleetConfig().verify_sandbox == "auto"
    with pytest.raises(ConfigError, match="verify_sandbox"):
        FleetConfig.from_dict({"verify_sandbox": "vm"})
    with pytest.raises(ConfigError, match="verify_image"):
        FleetConfig.from_dict({"verify_image": " "})


# ------------------------------------------------------------------------ the merge queue


CHECK_EXPORT = [
    sys.executable,
    "-c",
    "import pathlib; p = pathlib.Path('.');"
    "assert not (p / '.git').exists();"
    "assert (p / 'src' / 't.py').read_text() == 'T = 1\\n'",
]


def test_the_merge_queue_verifies_the_export_in_the_container(repo, tmp_path, engines):
    install, calls = engines
    install("podman")
    landing, clone, work, mq, guard = isolated(repo, tmp_path, verify=CHECK_EXPORT)
    mq.sandbox = make_sandbox(IMAGE, "auto")
    c = attempt(work, "T", 1, 1, {"src/t.py": "T = 1\n"})

    out = mq.integrate(RUN, "T", 1, 1, c, ("src/",))

    assert out.status is MergeStatus.MERGED, out.detail
    runs = [c for c in calls() if c[1] == "run"]
    assert len(runs) == 1
    mount = runs[0][runs[0].index("--mount") + 1]
    integrator = str((tmp_path / "integrator").resolve())
    assert f"source={integrator}/{RUN}/" in mount
    assert not [a for a in runs[0] if str(repo) in a or str(clone.repo) in a]


def test_a_failing_sandboxed_verify_blocks_the_landing(repo, tmp_path, engines):
    install, _ = engines
    install("podman")
    fail = [sys.executable, "-c", "import sys; sys.exit(4)"]
    landing, clone, work, mq, guard = isolated(repo, tmp_path, verify=fail)
    mq.sandbox = make_sandbox(IMAGE, "auto")
    before = git(repo, "rev-parse", "main")
    c = attempt(work, "T", 1, 1, {"src/t.py": "T = 1\n"})
    assert mq.integrate(RUN, "T", 1, 1, c, ("src/",)).status is MergeStatus.VERIFY_FAILED
    assert git(repo, "rev-parse", "main") == before


def test_a_timed_out_container_is_removed(repo, tmp_path, engines):
    install, calls = engines
    install("podman")
    hang = [sys.executable, "-c", "import time; time.sleep(60)"]
    landing, clone, work, mq, guard = isolated(repo, tmp_path, verify=hang)
    mq.verify_timeout_s = 1
    mq.sandbox = make_sandbox(IMAGE, "auto")
    c = attempt(work, "T", 1, 1, {"src/t.py": "T = 1\n"})
    out = mq.integrate(RUN, "T", 1, 1, c, ("src/",))
    assert out.status is MergeStatus.VERIFY_FAILED and "timed out" in out.detail
    run = next(c for c in calls() if c[1] == "run")
    name = run[run.index("--name") + 1]
    assert ["podman", "rm", "--force", name] in calls()


def test_build_fleet_sandboxes_the_integrator_when_an_engine_is_there(repo, tmp_path, engines):
    install, _ = engines
    install("podman")
    conf = config(verify_command=("true",), verify_image=IMAGE)
    _, fleet = build_fleet(
        repo,
        plan_of(spec("A")),
        conf,
        events_path=tmp_path / "e.jsonl",
        worktrees=tmp_path / "fleet",
        claude_binary="unused",
        executor=CallableExecutor(lambda r: None),
    )
    queues = {id(r.merge_queue): r.merge_queue for r in fleet.runners if r.merge_queue is not None}
    assert len(queues) == 1
    (mq,) = queues.values()
    assert mq.sandbox is not None and Path(mq.sandbox.engine).name == "podman"


def test_podman_run_as_root_gets_a_user_not_keep_id(tmp_path, monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 0)
    argv = ContainerSandbox("podman", IMAGE).argv(["true"], tmp_path, "n1")
    assert "--userns=keep-id" not in argv and "--user" in argv


@pytest.mark.parametrize(
    "output, version",
    [
        ("git version 2.43.0", (2, 43)),
        ("git version 2.39.5 (Apple Git-154)", (2, 39)),
        ("git version 2.42.0.windows.2", (2, 42)),
        ("something else", (0, 0)),
    ],
)
def test_git_version_is_read_from_the_start_of_the_output(output, version):
    from fleet_a.git import git_version

    assert git_version(output) == version


def test_apple_git_239_is_refused_for_the_integrator(repo, monkeypatch):
    import subprocess as sp

    from fleet_a.git import Git
    from fleet_a.errors import GitError

    real = Git.run

    def fake(self, *args, **kw):
        if args == ("version",):
            return sp.CompletedProcess(["git", "version"], 0, "git version 2.39.5 (Apple Git-154)\n", "")
        return real(self, *args, **kw)

    monkeypatch.setattr(Git, "run", fake)
    with pytest.raises(GitError, match="2.42"):
        Git(repo, tree_attributes=False)
