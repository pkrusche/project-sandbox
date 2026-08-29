#!/usr/bin/env python3
"""Smoke-test Internet-proxy routing and firewall bypass prevention.

The test is availability-gated: when no listener is running at the configured
host-loopback endpoint, it prints a skip note and exits successfully.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from project_sandbox import agent_proxy, internet_proxy

AUDIT_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import subprocess
from pathlib import Path

failures = []

proxied = subprocess.run(
    [
        "curl", "--fail", "--silent", "--show-error", "--max-time", "20",
        __ALLOWED_URL__,
    ],
    capture_output=True,
    text=True,
    check=False,
)
if proxied.returncode != 0:
    failures.append(
        "allowlisted HTTPS failed through the Internet proxy: "
        + (proxied.stderr.strip() or f"curl exit {proxied.returncode}")
    )

direct = subprocess.run(
    [
        "curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
        "--max-time", "5", __ALLOWED_URL__,
    ],
    capture_output=True,
    text=True,
    check=False,
)
if direct.returncode == 0:
    failures.append("curl --noproxy bypassed the sandbox firewall")

if __EXPECT_AGENT_PROXY__:
    key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "")
    if not key or not base_url:
        failures.append("Agent proxy environment is missing")
    else:
        models = subprocess.run(
            [
                "curl", "--fail", "--silent", "--show-error", "--max-time", "10",
                "--header", "Authorization: Bearer " + key,
                base_url.rstrip("/") + "/models",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if models.returncode != 0:
            failures.append(
                "Agent proxy /models is unreachable: "
                + (models.stderr.strip() or f"curl exit {models.returncode}")
            )

    sensitive_names = {
        name
        for name, value in os.environ.items()
        if value and any(
            marker in name.upper()
            for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        )
    }
    if sensitive_names != {"OPENAI_API_KEY"}:
        failures.append(
            "unexpected credential-like environment variables: "
            + ", ".join(sorted(sensitive_names - {"OPENAI_API_KEY"}))
        )

    home = Path.home()
    for path in (
        home / ".claude/.credentials.json",
        home / ".claude.json",
        home / ".codex/auth.json",
        home / ".pi/agent/auth.json",
        home / ".config/opencode/auth.json",
        home / ".local/share/opencode/auth.json",
    ):
        if path.exists():
            failures.append("unexpected forwarded credential file: " + str(path))

result = {"ok": not failures, "failures": failures}
Path("internet-proxy-smoke-result.json").write_text(
    json.dumps(result, indent=2) + "\n", encoding="utf-8"
)
for failure in failures:
    print("FAIL: " + failure)
if not failures:
    print("PASS: allowed HTTPS used the proxy and direct bypass was blocked")
    if __EXPECT_AGENT_PROXY__:
        print("PASS: Agent proxy worked without forwarding host credentials")
raise SystemExit(0 if not failures else 1)
"""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--runtime",
        default="auto",
        choices=("auto", "apple-container", "docker", "podman"),
    )
    p.add_argument("--base-image", default="python:3.12-slim")
    p.add_argument("--image-tag", default="project-sandbox-internet-proxy-smoke:latest")
    p.add_argument("--internet-proxy", default="http://127.0.0.1:18080")
    p.add_argument("--agent-proxy", default="http://127.0.0.1:4000/v1")
    p.add_argument("--agent-proxy-key-env", default=agent_proxy.DEFAULT_KEY_ENV)
    p.add_argument("--model")
    p.add_argument("--allowed-url", default="https://github.com/")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--keep", action="store_true")
    return p


def _agent_proxy_scenario(
    args: argparse.Namespace,
) -> tuple[str, str] | None:
    _path, port = agent_proxy.validate_url(args.agent_proxy)
    host = agent_proxy.loopback_host(args.agent_proxy)
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except OSError:
        print(
            f"SKIP: Agent proxy is not running at {args.agent_proxy}; "
            "running the Internet-proxy-only scenario."
        )
        return None

    try:
        key, _source = agent_proxy.resolve_key(args.agent_proxy_key_env, None)
    except SystemExit:
        print(
            f"SKIP: Agent proxy is running but {args.agent_proxy_key_env} is "
            "unavailable; running the Internet-proxy-only scenario."
        )
        return None
    models = agent_proxy.discover_models(args.agent_proxy, key)
    model = args.model or models[0]
    if model not in models:
        raise SystemExit(
            f"Selected model {model!r} is unavailable from the agent proxy"
        )
    return key, model


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    proxy = internet_proxy.parse(args.internet_proxy)
    try:
        internet_proxy.preflight(proxy, timeout=0.5)
    except SystemExit:
        print(
            f"SKIP: Internet proxy is not running at {proxy.original_url}; "
            "start it to run the routing smoke test."
        )
        return 0

    gateway = _agent_proxy_scenario(args)

    root = Path(__file__).resolve().parents[1]
    temp_root = root / ".project-sandbox"
    temp_root.mkdir(exist_ok=True)
    project = Path(tempfile.mkdtemp(prefix="internet-proxy-smoke.", dir=temp_root))
    result_path = project / "internet-proxy-smoke-result.json"
    try:
        audit = textwrap.dedent(AUDIT_SOURCE).replace(
            "__ALLOWED_URL__", repr(args.allowed_url)
        )
        audit = audit.replace("__EXPECT_AGENT_PROXY__", repr(gateway is not None))
        (project / "internet-proxy-smoke.py").write_text(audit, encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "project_sandbox",
            "--runtime",
            args.runtime,
            "--agent",
            "bash",
            "--no-forward-credentials",
            "--internet-proxy",
            args.internet_proxy,
            "--image-tag",
            args.image_tag,
            "--prompt-text",
            "python internet-proxy-smoke.py",
            "--timeout",
            str(args.timeout),
        ]
        run_env = os.environ.copy()
        if gateway is not None:
            key, model = gateway
            run_env[args.agent_proxy_key_env] = key
            command += [
                "--agent-proxy",
                args.agent_proxy,
                "--agent-proxy-key-env",
                args.agent_proxy_key_env,
                "--model",
                model,
            ]
        if args.no_build:
            command.append("--no-build")
        command += [str(project), args.base_image]
        completed = subprocess.run(command, cwd=root, env=run_env, check=False)
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        if completed.returncode or not result.get("ok"):
            for failure in result.get(
                "failures", ["Internet-proxy smoke sandbox did not complete"]
            ):
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1
        print("PASS: Internet proxy routing and fail-closed bypass smoke test")
        if gateway is not None:
            print("PASS: combined Internet proxy + Agent proxy credential isolation")
        return 0
    finally:
        if args.keep:
            print(f"Test project kept for inspection: {project}")
        else:
            shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
