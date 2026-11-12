"""Explicitly invoked live tests; never collected by the ordinary tests/ run."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest

from project_sandbox import build_cache, container_cli, mount_storage

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = "python:3.12-slim"
# Options that change the generated image, so a launch using one cannot share.
IMAGE_OPTIONS = frozenset(
    {
        "--dockerfile",
        "--rust-cargo",
        "--python-uv",
        "--node-npm",
        "--extra-domain",
        "--pi-ollama",
        "--ca-cert",
        "--build-ca-cert",
        "--build-proxy",
        "--internet-proxy",
        "--agent-proxy",
    }
)


def pytest_addoption(parser):
    parser.addoption(
        "--security-runtime",
        choices=("docker", "podman", "apple-container", "host"),
        default="host",
    )
    parser.addoption(
        "--fixture-host", help="Host IPv4 address reachable from test containers"
    )
    parser.addoption(
        "--nat64-origin",
        help="Controlled TLS origin hostname routed through the deployment NAT64 translator",
    )
    parser.addoption("--peer-runtime", choices=("docker", "podman", "apple-container"))
    parser.addoption(
        "--strict-coverage",
        action="store_true",
        help="Fail if any check skips or records a known limitation",
    )
    parser.addoption(
        "--security-artifacts", help="Empty directory for retained logs and fixtures"
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "shared_image: launch from one image per egress mode built once per run "
        "(the test must not depend on building or replacing its own image)",
    )


def pytest_sessionfinish(session, exitstatus):
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if (
        session.config.getoption("--strict-coverage")
        and reporter
        and (reporter.stats.get("skipped") or reporter.stats.get("xfailed"))
    ):
        session.exitstatus = 1


def run(argv, *, cwd=ROOT, env=None, timeout=600, check=True):
    result = subprocess.run(
        [str(a) for a in argv],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if check:
        assert result.returncode == 0, f"{argv!r}\n{result.stdout[-8000:]}"
    return result


def wait_for(predicate, *, timeout=120, processes=()):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        assert all(p.poll() is None for p in processes), (
            "session exited before reaching barrier; inspect artifacts"
        )
        time.sleep(0.1)
    raise AssertionError("timed out waiting for barrier; inspect artifacts")


@pytest.fixture(scope="session")
def artifacts(request):
    supplied = request.config.getoption("--security-artifacts")
    if supplied:
        path = Path(supplied).resolve()
        mount_storage.ensure_private_dirs(path)
        assert not list(path.iterdir()), "artifact directory must be empty"
    else:
        base = (
            Path(tempfile.gettempdir()) / "project-sandbox-security-e2e"
            if request.config.getoption("--security-runtime") == "host"
            else ROOT / ".project-sandbox/e2e"
        )
        # XDG_STATE_HOME lives below here; group-writable ancestors (umask
        # 002) are refused as unsafe parents of private mount storage.
        mount_storage.ensure_private_dirs(base)
        path = Path(tempfile.mkdtemp(prefix="security.", dir=base))
    print(f"\nSecurity artifacts retained: {path}")
    return path


@pytest.fixture
def case_dir(artifacts, request):
    path = artifacts / re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.name)
    path.mkdir(mode=0o700)
    return path


@pytest.fixture(scope="session")
def runtime(request):
    name = request.config.getoption("--security-runtime")
    if name == "host":
        pytest.skip("requires --security-runtime docker|podman|apple-container")
    binary = "container" if name == "apple-container" else name
    assert shutil.which(binary), f"{binary} is required for explicitly selected runtime"
    return container_cli.select_runtime(name)


def input_fingerprint(context_dir):
    """Content fingerprint of the generated image inputs, independent of tag."""
    return build_cache.compute_fingerprint(context_dir, extra={})


class SharedImages:
    """One image per egress mode, built by the first launch that needs it.

    Most security tests probe policy at runtime and only build because their
    image tag is new; on Apple container every such build re-exports the whole
    image (~15 s). Sharing is safe only while the generated inputs match, so
    every shared launch is checked against the inputs the image was built from.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.prefix = "project-sandbox-security:shared-" + uuid4().hex[:8]
        self.inputs: dict[str, str] = {}

    def tag(self, key):
        return f"{self.prefix}-{key}"

    def verify(self, key, context_dir):
        fingerprint = input_fingerprint(context_dir)
        recorded = self.inputs.setdefault(key, fingerprint)
        assert fingerprint == recorded, (
            f"generated image inputs in {context_dir} differ from the shared "
            f"{key!r} image; this launch must build its own image (drop the "
            "shared_image marker or add the option to IMAGE_OPTIONS)"
        )

    def close(self):
        command = (
            ["image", "delete"]
            if self.runtime.name == "apple-container"
            else ["image", "rm"]
        )
        for key in self.inputs:
            run([self.runtime.binary, *command, self.tag(key)], check=False, timeout=60)


def shared_key(extra, base):
    """The shared image a launch can use, or None if it changes the image."""
    if base != DEFAULT_BASE or IMAGE_OPTIONS.intersection(extra):
        return None
    extra = list(extra)
    for index, value in enumerate(extra[:-1]):
        if value == "--egress":
            return f"egress-{extra[index + 1]}"
    return "default"


class Sandbox:
    def __init__(self, directory, runtime, shared=None):
        self.directory, self.runtime = directory, runtime
        self.shared = shared
        self.launches = {}
        self.env = dict(os.environ)
        config = directory / "xdg-config"
        config.mkdir()
        # Preserve Podman machine connections without inheriting sandbox policy.
        old = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        if (old / "containers").is_dir():
            (config / "containers").symlink_to(old / "containers")
        self.env.update(
            XDG_CONFIG_HOME=str(config),
            XDG_STATE_HOME=str(directory / "state"),
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Security E2E",
            GIT_AUTHOR_EMAIL="e2e@example.invalid",
            GIT_COMMITTER_NAME="Security E2E",
            GIT_COMMITTER_EMAIL="e2e@example.invalid",
        )
        self.own_image = "project-sandbox-security:" + uuid4().hex[:12]
        self.children = []
        self.counter = 0

    def project(self, name="project"):
        path = self.directory / name
        path.mkdir()
        (path / "README.md").write_text("Disposable security fixture\n")
        (path / ".gitignore").write_text(".project-sandbox/\n.devcontainer/\n")
        return path

    @property
    def image(self):
        """The default-mode image tag direct runtime commands should use."""
        return self.shared.tag("default") if self.shared else self.own_image

    def argv(self, project, prompt="true", *, build=False, extra=(), base=DEFAULT_BASE):
        shared = self.shared
        key = shared_key(extra, base) if shared else None
        image = self.own_image
        if shared is not None and key is not None:
            image = shared.tag(key)
            # Only the first launch of each mode builds; later ones reuse it.
            build = build and key not in shared.inputs
        argv = [
            sys.executable,
            "-m",
            "project_sandbox",
            "--runtime",
            self.runtime.name,
            "--image-tag",
            image,
            "--agent",
            "bash",
            "--no-forward-credentials",
            "--no-token-refresh",
            "--no-firewall",
            "--timeout",
            "240",
            "--prompt-text",
            prompt,
            *([] if build else ["--no-build"]),
            *extra,
            str(project),
            *([base] if base else []),
        ]
        if key is not None:
            self.launches[id(argv)] = (argv, key, project / ".project-sandbox")
        return argv

    def start(self, argv):
        self.counter += 1
        log = self.directory / f"session-{self.counter}.log"
        with log.open("w") as stream:
            proc = subprocess.Popen(
                argv, cwd=ROOT, env=self.env, stdout=stream, stderr=subprocess.STDOUT
            )
        self.children.append(proc)
        # Bookkeeping read back by finish(); Popen has no such attributes.
        proc.audit_log = log  # ty: ignore[unresolved-attribute]
        launch = self.launches.pop(id(argv), None)
        proc.shared_launch = (  # ty: ignore[unresolved-attribute]
            launch[1:] if launch and launch[0] is argv else None
        )
        return proc

    def finish(self, proc, *, ok=True, timeout=600):
        code = proc.wait(timeout=timeout)
        if ok:
            assert code == 0, proc.audit_log.read_text()[-8000:]
        if code == 0 and proc.shared_launch is not None:
            assert self.shared is not None
            key, context_dir = proc.shared_launch
            self.shared.verify(key, context_dir)
        return code

    def session(self, project, prompt="true", **kwargs):
        proc = self.start(self.argv(project, prompt, **kwargs))
        self.finish(proc, timeout=1800)
        return proc

    def records(self):
        return [
            json.loads(path.read_text())
            for path in (self.directory / "state/project-sandbox/sessions").glob(
                "*.json"
            )
        ]

    def container_for(self, proc):
        matches = [r["container_name"] for r in self.records() if r["pid"] == proc.pid]
        assert len(matches) == 1, "expected one recorded container for CLI owner"
        return matches[0]

    def close(self):
        for proc in self.children:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        # Session records are in this test's private XDG tree. Stop only its
        # containers, including a child left behind by the intentional SIGKILL.
        for record in self.records():
            if not Path(record["project_path"]).is_relative_to(self.directory):
                continue
            ident = record["container_name"]
            if ident:
                run([self.runtime.binary, "rm", "-f", ident], check=False, timeout=30)
        command = (
            ["image", "delete"]
            if self.runtime.name == "apple-container"
            else ["image", "rm"]
        )
        run([self.runtime.binary, *command, self.own_image], check=False, timeout=60)


@pytest.fixture(scope="session")
def shared_images(runtime):
    images = SharedImages(runtime)
    try:
        yield images
    finally:
        images.close()


@pytest.fixture
def sandbox(case_dir, runtime, request):
    shared = (
        request.getfixturevalue("shared_images")
        if request.node.get_closest_marker("shared_image")
        else None
    )
    fixture = Sandbox(case_dir, runtime, shared)
    try:
        yield fixture
    finally:
        fixture.close()


def git(project, *args):
    return run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Security E2E",
            "-c",
            "user.email=e2e@example.invalid",
            "-C",
            project,
            *args,
        ]
    ).stdout.strip()


def init_git(project):
    git(project, "init")
    for key, value in (
        ("core.hooksPath", "/dev/null"),
        ("commit.gpgsign", "false"),
        ("user.name", "Security E2E"),
        ("user.email", "e2e@example.invalid"),
    ):
        git(project, "config", "--local", key, value)
    git(project, "add", ".")
    git(project, "commit", "-m", "initial fixture")
