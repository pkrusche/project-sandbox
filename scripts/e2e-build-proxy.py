#!/usr/bin/env python3
"""Opt-in build proxy/CA acceptance test using an isolated IPL Squid workspace.

Requires internet-proxy-locally sources, uv, Docker or Apple container, and a
local non-loopback IP reachable from both the image builder and the sandbox.
Never controls the caller's existing proxy, configuration, or signing CA.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from project_sandbox import container_cli, fixture_network

FIXTURES = Path(__file__).parent / "fixtures"
PROXY_NAMES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
BUILD_HOSTS = (
    "deb.debian.org",
    "security.debian.org",
    "nodejs.org",
    "github.com",
    "*.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)


def fixture_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError("not a usable fixture address")
        if "%" in value:
            raise ValueError("scoped addresses are unsupported")
        return str(address)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "use a local non-loopback unicast IP reachable from the builder"
        ) from exc


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runtime", choices=("docker", "apple-container"), required=True)
    p.add_argument("--internet-proxy-dir", type=Path, required=True)
    p.add_argument(
        "--fixture-host",
        type=fixture_host,
        help="Local fixture IP (default: outbound IPv4 source address).",
    )
    p.add_argument("--base-image", default="python:3.12-slim")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--keep", action="store_true")
    return p


def clean_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name.lower() not in PROXY_NAMES and not name.startswith("IPL_")
    }


def prepare_fixture(source: Path, target: Path) -> None:
    # Copy packaged sources and metadata, never a caller's state/CA or venv.
    target.mkdir()
    for name in (
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "README.md",
        "LICENSE",
    ):
        path = source / name
        if path.is_file():
            shutil.copy2(path, target / name)
    shutil.copytree(
        source / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    policy = (source / "config.toml").read_text()
    allow = tomllib.loads(policy)["policy"]["allow"]
    expanded = list(dict.fromkeys([*allow, *BUILD_HOSTS]))
    policy, count = re.subn(
        r"(?ms)^(\[policy\]\s*\n.*?\ballow\s*=\s*)\[.*?\]",
        lambda match: match[1] + json.dumps(expanded),
        policy,
        count=1,
    )
    if count != 1:
        raise RuntimeError("could not extend the fixture's [policy] allowlist")
    (target / "config.toml").write_text(policy)


def run_logged(
    command: list[str], log: Path, *, cwd: Path, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess:
    with log.open("a") as output:
        output.write("COMMAND: " + shlex.join(command) + "\n")
        output.flush()
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )
    return result


def check_build(returncode: int, output: str, expected: str) -> None:
    if expected == "success":
        if returncode:
            raise RuntimeError("proxied build failed")
        return
    patterns = {
        "ca": (
            "certificate verification failed",
            "certificate verify failed",
            "ssl certificate problem",
            "unable to get local issuer",
        ),
        "proxy": (
            "could not connect",
            "failed to connect",
            "connection refused",
            "connection timed out",
            "couldn't connect",
        ),
    }
    if not returncode or not any(
        pattern in output.lower() for pattern in patterns[expected]
    ):
        raise RuntimeError(
            f"expected a specific {expected} failure, not an unrelated build failure or cache hit"
        )


def reserved_port(host: str) -> socket.socket:
    sock = socket.socket(
        socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM
    )
    try:
        sock.bind((host, 0))
    except BaseException:
        sock.close()
        raise
    return sock


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.fixture_host is None:
        try:
            args.fixture_host = fixture_network.default_host()
        except ValueError as exc:
            argument_parser.error(str(exc))
        print(f"Using proxy fixture host: {args.fixture_host}", flush=True)
    runtime = container_cli.select_runtime(args.runtime)
    if shutil.which("uv") is None:
        raise SystemExit("uv is required")
    source = args.internet_proxy_dir.expanduser().resolve()
    if (
        not (source / "src/internet_proxy_locally").is_dir()
        or not (source / "config.toml").is_file()
    ):
        raise SystemExit(
            "--internet-proxy-dir must contain internet-proxy-locally sources and config.toml"
        )
    root = Path(tempfile.mkdtemp(prefix="project-sandbox-build-proxy."))
    fixture = root / "squid"
    project = root / "project"
    project.mkdir()
    env = clean_environment()
    fixture_env = {
        **env,
        "IPL_ROOT": str(fixture),
        "UV_PROJECT_ENVIRONMENT": str(fixture / ".venv"),
    }
    fixture_env.pop("PYTHONPATH", None)
    success = False
    started = False
    images = []
    results = {}
    ipl = []
    try:
        prepare_fixture(source, fixture)
        with reserved_port(args.fixture_host) as reservation:
            port = reservation.getsockname()[1]
        authority = (
            f"[{args.fixture_host}]" if ":" in args.fixture_host else args.fixture_host
        )
        proxy_url = f"http://{authority}:{port}"
        ipl = [
            "uv",
            "run",
            "ipl",
            "--engine",
            "squid",
            "--backend",
            "docker" if args.runtime == "docker" else "container",
            "--ip",
            args.fixture_host,
            "--port",
            str(port),
        ]

        def control(*arguments: str) -> None:
            result = run_logged(
                [*ipl, *arguments],
                root / "squid.log",
                cwd=fixture,
                env=fixture_env,
                timeout=args.timeout,
            )
            if result.returncode:
                raise RuntimeError(
                    "Squid fixture command failed: " + " ".join(arguments)
                )

        control("setup", "--tls-interception")
        started = True  # up can leave a container behind even if health checking fails.
        control("up", "--tls-interception")
        certificate = root / "squid-public-ca.pem"
        control("ca", "export", "--out", str(certificate))
        fingerprint = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(certificate.read_text())
        ).hexdigest()
        template = Environment(
            loader=FileSystemLoader(FIXTURES), undefined=StrictUndefined
        ).get_template("build-proxy.Dockerfile.j2")
        base_command = [
            sys.executable,
            "-m",
            "project_sandbox",
            "--runtime",
            args.runtime,
            "--verbose",
            "--no-forward-credentials",
            "--no-verify-dockerfile",
        ]
        good_image = ""
        for scenario in ("success", "ca", "proxy"):
            nonce = uuid.uuid4().hex
            image = "project-sandbox-build-proxy:" + nonce
            images.append(image)
            (project / "Dockerfile").write_text(
                template.render(base_image=args.base_image, nonce=nonce) + "\n"
            )
            command = [
                *base_command,
                "--build-only",
                "--force-build",
                "--image-tag",
                image,
                "--dockerfile",
                str(project / "Dockerfile"),
            ]
            if scenario != "ca":
                command += ["--build-ca-cert", str(certificate)]
            # Reserve a closed port throughout the negative run to avoid collisions.
            with reserved_port(args.fixture_host) as closed:
                url = (
                    f"http://{authority}:{closed.getsockname()[1]}"
                    if scenario == "proxy"
                    else proxy_url
                )
                log = root / f"build-{scenario}.log"
                result = run_logged(
                    [*command, "--build-proxy", url, str(project)],
                    log,
                    cwd=Path(__file__).resolve().parents[1],
                    env=env,
                    timeout=args.timeout,
                )
            check_build(result.returncode, log.read_text(), scenario)
            results["build-" + scenario] = True
            print("PASS: build scenario " + scenario, flush=True)
            if scenario == "success":
                good_image = image
        # Audit the successful image, independently of the negative build inputs.
        shutil.copy2(FIXTURES / "build-proxy-audit.py", project / "audit.py")
        for phase in ("blocked", "allowed"):
            command = [
                *base_command,
                "--no-build",
                "--image-tag",
                good_image,
                "--agent",
                "bash",
                "--timeout",
                "120",
                "--prompt-text",
                shlex.join(["python3", "audit.py", phase, proxy_url, fingerprint]),
            ]
            if phase == "allowed":
                command += ["--internet-proxy", proxy_url]
            command += [str(project), args.base_image]
            result = run_logged(
                command,
                root / f"runtime-{phase}.log",
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                timeout=args.timeout,
            )
            report = project / f"build-proxy-runtime-{phase}.json"
            if (
                result.returncode
                or not report.is_file()
                or not json.loads(report.read_text()).get("ok")
            ):
                raise RuntimeError("runtime audit failed: " + phase)
            results["runtime-" + phase] = True
            print("PASS: runtime proxy " + phase, flush=True)
        control("logs")
        squid_log = (root / "squid.log").read_text()
        for host in (
            "deb.debian.org",
            "pypi.org",
            "registry.npmjs.org",
            "files.pythonhosted.org",
        ):
            if not re.search(r"https?://" + re.escape(host) + r"[/:]", squid_log):
                raise RuntimeError(
                    "missing decrypted Squid request evidence for " + host
                )
        results["squid-decrypted-requests"] = True
        success = True
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        results["error"] = str(exc)
        print("FAIL: " + str(exc), file=sys.stderr)
    finally:
        if started:
            # Keep all diagnostics before removing the isolated endpoint.
            for action in ("logs", "down"):
                try:
                    result = run_logged(
                        [*ipl, action],
                        root / "squid-cleanup.log",
                        cwd=fixture,
                        env=fixture_env,
                        timeout=120,
                    )
                    if result.returncode:
                        success = False
                        results["cleanup-" + action] = "failed"
                except (OSError, subprocess.SubprocessError) as exc:
                    success = False
                    results["cleanup-" + action] = str(exc)
        if not args.keep:
            for image in images:
                try:
                    run_logged(
                        [runtime.binary, "image", "rm", image],
                        root / "image-cleanup.log",
                        cwd=root,
                        env=env,
                        timeout=120,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass  # Failed builds may never have produced a tagged image.
        (root / "result.json").write_text(
            json.dumps({"ok": success, **results}, indent=2) + "\n"
        )
        if args.keep or not success:
            print(f"Build proxy E2E artifacts: {root}")
        else:
            shutil.rmtree(root)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
