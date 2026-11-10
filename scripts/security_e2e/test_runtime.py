"""Real CLI sessions: builds, mount grammar, lifetime ownership and host capture."""

import json
import shutil
import sys
from pathlib import Path

import pytest
from conftest import ROOT, git, init_git, run, wait_for

from project_sandbox import container_cli


@pytest.mark.parametrize("suite", ["firewall", "vcs-access"])
def test_existing_mount_and_firewall_audits(runtime, case_dir, suite):
    result = run(
        [
            sys.executable,
            ROOT / f"scripts/e2e-{suite}.py",
            "--runtime",
            runtime.name,
            "--keep",
        ],
        check=False,
        timeout=3600,
    )
    (case_dir / "suite.log").write_text(result.stdout)
    assert result.returncode == 0, result.stdout[-8000:]


def test_cargo_literal_paths(sandbox):
    project = sandbox.project()
    names = [
        "space here",
        "single'quote",
        'double"quote',
        "dollar$HOME",
        "star*name",
        "bracket[1]",
        "semi;colon",
        "$(touch INJECTED)",
        "`touch INJECTED`",
    ]
    (project / "Cargo.toml").write_text(
        '[workspace]\nresolver = "2"\nmembers = ' + json.dumps(["crates/*"]) + "\n"
    )
    lock = "version = 3\n"
    for i, name in enumerate(names):
        member = project / "crates" / name
        (member / "src").mkdir(parents=True)
        (member / "src/lib.rs").write_text("pub fn literal() -> u8 { 42 }\n")
        (member / "Cargo.toml").write_text(
            f'[package]\nname="literal_{i}"\nversion="0.1.0"\nedition="2021"\n'
        )
        lock += f'\n[[package]]\nname = "literal_{i}"\nversion = "0.1.0"\n'
    (project / "Cargo.lock").write_text(lock)
    sandbox.session(
        project,
        "cargo check --workspace --offline --locked && printf ok > cargo.ok",
        build=True,
        extra=["--rust-cargo"],
        base=None,
    )
    assert (project / "cargo.ok").read_text() == "ok"
    # cargo fetch during image generation must have copied each literal manifest.
    assert not (project / "INJECTED").exists()


@pytest.mark.parametrize(
    "name",
    ["space here", "quote'and\"double", "dollar$HOME", "bracket[1]*", "comma,name"],
)
def test_mount_roundtrip(sandbox, name):
    project = sandbox.project(name)
    (project / "canary").write_text(name)
    sandbox.session(project, "cp canary roundtrip", build=True)
    assert (project / "roundtrip").read_text() == name


@pytest.mark.parametrize("name", ["line\nbreak"])
def test_unsupported_mount_grammar_fails_closed(sandbox, name):
    project = sandbox.project(name)
    proc = sandbox.start(sandbox.argv(project, "touch unsafe-launch", build=True))
    code = sandbox.finish(proc, ok=False, timeout=1800)
    assert code != 0, (
        "unsupported path was accepted; add a positive literal roundtrip test before allowing it"
    )
    assert not (project / "unsafe-launch").exists()
    assert any(
        word in proc.audit_log.read_text().lower()
        for word in ("mount", "control", "path")
    )


# The file barrier is reached only after entrypoint provisioning and mount setup.
HOLD = """import json, os, pathlib, time
p = pathlib.Path('/workspace')
p.joinpath('container-id').write_text(os.uname().nodename)
name = __import__('sys').argv[1]
prompt = pathlib.Path(os.environ['PROJECT_SANDBOX_PROMPT_FILE'])
original = prompt.read_bytes()
p.joinpath(name + '.ready').write_text(str(prompt))
end = time.monotonic() + 180
while not p.joinpath(name + '.release').exists():
    assert time.monotonic() < end, 'host did not release barrier'
    assert prompt.read_bytes() == original, 'another session changed or removed this prompt'
    time.sleep(.1)
p.joinpath(name + '.ok').write_text('ok')
"""


def test_concurrent_sessions_private_inputs_and_image_identity(sandbox):
    project = sandbox.project()
    (project / "hold.py").write_text(HOLD)
    # Build once, then exercise concurrent provisioning against the same project/tag.
    sandbox.session(project, build=True)
    identity = container_cli.image_identity(sandbox.runtime, sandbox.image)
    one = sandbox.start(sandbox.argv(project, "python3 -I hold.py one"))
    two = sandbox.start(sandbox.argv(project, "python3 -I hold.py two"))
    wait_for(
        lambda: all((project / f"{n}.ready").exists() for n in ("one", "two")),
        processes=(one, two),
    )
    inputs = sandbox.directory / "state/project-sandbox/session-inputs"
    trees = list(inputs.iterdir())
    assert len(trees) == 2, f"expected independent session trees: {trees}"
    assert all(p.stat().st_mode & 0o777 == 0o700 for p in trees)
    # Inspect these sessions' exact runtime objects, not just rendered argv.
    own = [
        json.loads(
            run([sandbox.runtime.binary, "inspect", sandbox.container_for(proc)]).stdout
        )[0]
        for proc in (one, two)
    ]
    (sandbox.directory / "containers-live.json").write_text(json.dumps(own, indent=2))
    if sandbox.runtime.name == "apple-container":
        aliases = [row["configuration"]["image"]["reference"] for row in own]
        assert len(set(aliases)) == 2
        assert all(
            alias.startswith("localhost/project-sandbox-session:") for alias in aliases
        )
        assert all(
            container_cli.image_identity(sandbox.runtime, alias) == identity
            for alias in aliases
        )
    else:
        assert all(
            row["Image"].removeprefix("sha256:") == identity.removeprefix("sha256:")
            for row in own
        )
        assert all(row["Config"]["Image"] == identity for row in own), (
            "runtime was launched with a mutable tag"
        )
    (project / "one.release").touch()
    sandbox.finish(one)
    assert len(list(inputs.iterdir())) == 1
    assert two.poll() is None
    (project / "two.release").touch()
    sandbox.finish(two)
    assert not list(inputs.iterdir())
    assert (project / "one.ok").exists() and (project / "two.ok").exists()
    if sandbox.runtime.name == "apple-container":
        for alias in aliases:
            result = run(
                [sandbox.runtime.binary, "image", "inspect", alias], check=False
            )
            assert result.returncode != 0, "session alias survived normal teardown"


@pytest.mark.parametrize("backend", ["git", "jj"])
@pytest.mark.parametrize("mode", ["full", "read-only", "hidden"])
def test_workspace_lease_and_capture(sandbox, backend, mode):
    if backend == "jj" and not shutil.which("jj"):
        pytest.skip("host jj required for jj workspace sessions")
    if (
        backend == "git"
        and mode != "full"
        and sandbox.runtime.name == "apple-container"
    ):
        pytest.skip("Apple Git pointer-file mounts are an open roadmap feature")
    project = sandbox.project()
    (project / "hold.py").write_text(HOLD)
    init_git(project)
    if backend == "jj":
        run(["jj", "git", "init", "--colocate", project], env=sandbox.env)
        for key, value in (
            ("user.name", "Security E2E"),
            ("user.email", "e2e@example.invalid"),
            ("signing.behavior", "drop"),
        ):
            run(
                ["jj", "-R", project, "config", "set", "--repo", key, value],
                env=sandbox.env,
            )
    sandbox.session(project, build=True)
    workspace_root = sandbox.directory / "owned-workspaces"
    workspace = workspace_root / "security-owner"
    extra = [
        "--branch",
        "security-owner",
        "--worktree-dir",
        str(workspace_root),
        "--keep-workspace",
        "--vcs-access",
        mode,
    ]
    first = sandbox.start(
        sandbox.argv(project, "python3 -I hold.py owner", extra=extra)
    )
    wait_for(lambda: (workspace / "owner.ready").exists(), processes=(first,))
    second = sandbox.start(sandbox.argv(project, "touch second-entered", extra=extra))
    assert sandbox.finish(second, ok=False, timeout=30) != 0
    assert "in use" in second.audit_log.read_text().lower(), (
        second.audit_log.read_text()
    )
    assert not (workspace / "second-entered").exists()
    assert first.poll() is None and workspace.exists()
    (workspace / "owner.release").touch()
    sandbox.finish(first)
    # A released lease allows a fresh session and host capture persists edits.
    sandbox.session(project, "printf captured > captured.txt", extra=extra)
    if backend == "git":
        assert git(project, "show", "security-owner:captured.txt") == "captured"
    else:
        assert (
            run(
                [
                    "jj",
                    "-R",
                    project,
                    "file",
                    "show",
                    "-r",
                    "security-owner",
                    "captured.txt",
                ],
                env=sandbox.env,
            ).stdout
            == "captured"
        )


def test_live_external_git_lock_is_preserved(sandbox):
    project = sandbox.project()
    init_git(project)
    sandbox.session(project, build=True)
    workspace_root = sandbox.directory / "locked-workspaces"
    workspace = workspace_root / "external-lock"
    git(project, "worktree", "add", "-b", "external-lock", str(workspace))
    metadata = Path(git(workspace, "rev-parse", "--absolute-git-dir"))
    lock = metadata / "index.lock"
    import subprocess

    owner = subprocess.Popen(
        ["git", "-C", str(workspace), "update-index", "--index-info"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for(lock.exists, processes=(owner,))
        inode, contents = lock.stat().st_ino, lock.read_bytes()
        proc = sandbox.start(
            sandbox.argv(
                project,
                "touch entered",
                extra=[
                    "--branch",
                    "external-lock",
                    "--worktree-dir",
                    str(workspace_root),
                    "--keep-workspace",
                ],
            )
        )
        assert sandbox.finish(proc, ok=False) != 0
        assert owner.poll() is None, (
            "external Git owner must remain live during the probe"
        )
        assert lock.stat().st_ino == inode and lock.read_bytes() == contents
        assert not (workspace / "entered").exists()
    finally:
        owner.terminate()
        owner.communicate(timeout=5)


@pytest.mark.parametrize("project_name", ["project", "comma,name", 'quote" $HOME[*]'])
def test_private_mounts_and_devcontainer_reopen(sandbox, project_name):
    project = sandbox.project(project_name)
    init_git(project)
    sandbox.session(project, build=True, extra=["--vcs-access", "hidden"])
    config = json.loads((project / ".devcontainer/devcontainer.json").read_text())
    assert isinstance(config["initializeCommand"], list), (
        "initialization must use argv, not a shell"
    )
    initialize = [
        part.replace("${localWorkspaceFolder}", str(project))
        for part in config["initializeCommand"]
    ]
    mounts = [
        m.replace("${localWorkspaceFolder}", str(project)) for m in config["mounts"]
    ]
    # A reopen executes the generated standalone helper, not the package's API.
    for attempt in (1, 2):
        run(initialize, cwd=project, env=sandbox.env)
        argv = [
            sandbox.runtime.binary,
            "run",
            "--rm",
            "--entrypoint",
            "/bin/bash",
            "--mount",
            config["workspaceMount"].replace("${localWorkspaceFolder}", str(project)),
        ]
        if sandbox.runtime.name == "podman":
            argv += ["--userns=keep-id"]
        for mount in mounts:
            argv += ["--mount", mount]
        argv += [
            sandbox.image,
            "-ec",
            (
                'test -z "$(ls -A /workspace/.git)"; '
                'test -z "$(ls -A /workspace/.project-sandbox)"; '
                'test -z "$(ls -A /workspace/.devcontainer)"; '
                f"printf reopened-{attempt} > /workspace/reopened"
            ),
        ]
        result = run(argv, env=sandbox.env, check=False)
        (sandbox.directory / f"devcontainer-reopen-{attempt}.log").write_text(
            result.stdout
        )
        if (
            result.returncode
            and project_name != "project"
            and any(
                text in result.stdout.lower()
                for text in (
                    "invalid field",
                    "invalid mount",
                    "invalid csv",
                    'bare "',
                    "field count",
                )
            )
        ):
            assert not (project / "reopened").exists()
            pytest.xfail(
                "TODO: devcontainer mount strings do not encode this unusual project path"
            )
        assert result.returncode == 0, result.stdout
        assert (project / "reopened").read_text() == f"reopened-{attempt}"
        # Remove only empty generated masks; helper must recreate them on reopen.
        for empty in (sandbox.directory / "state").rglob("empty"):
            if empty.is_dir():
                assert not list(empty.iterdir())
                empty.rmdir()
    assert (project / ".git/HEAD").is_file()


def test_concurrent_builds_same_tag_use_their_own_image(sandbox):
    processes = []
    for marker in ("alpha", "beta"):
        project = sandbox.project(marker)
        (project / "Dockerfile").write_text(
            "FROM python:3.12-slim\n" + f"RUN printf {marker} > /opt/build-marker\n"
        )
        processes.append(
            (
                project,
                marker,
                sandbox.start(
                    sandbox.argv(
                        project,
                        "cp /opt/build-marker /workspace/actual-image",
                        build=True,
                        extra=["--dockerfile", str(project / "Dockerfile")],
                        base=None,
                    )
                ),
            )
        )
    for project, marker, proc in processes:
        sandbox.finish(proc, timeout=1800)
        assert (project / "actual-image").read_text() == marker


def test_installed_tool_dependency_inventory(sandbox):
    project = sandbox.project()
    inventories = []
    for attempt in (1, 2):
        sandbox.session(
            project,
            "npm ls --global --all --json > dependencies.json",
            build=True,
        )
        inventory = json.loads((project / "dependencies.json").read_text())
        (sandbox.directory / f"dependencies-{attempt}.json").write_text(
            json.dumps(inventory, sort_keys=True, indent=2)
        )
        inventories.append(inventory)
    assert inventories[0] == inventories[1], (
        "dependency inventory changed between repeated builds"
    )
    # Two cached builds alone cannot prove reproducibility. A transitive lock is
    # a prerequisite for upgrading this observation to a supply-chain guarantee.
    pytest.xfail(
        "TODO: generated global npm tool installs have no reviewed transitive lock; inventories retained, cache repeat is not reproducibility proof"
    )
