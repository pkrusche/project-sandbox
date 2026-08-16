#!/usr/bin/env python3
"""Run one billable headless request through each supported proxy agent."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from project_sandbox import agent_proxy


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--proxy", default="http://127.0.0.1:4000/v1")
    default = os.environ.get("AGENT_PROXY_TEST_MODEL", "gpt-5-mini")
    p.add_argument("--pi-model", default=default)
    p.add_argument("--opencode-model", default=default)
    p.add_argument("--key-env", default=agent_proxy.DEFAULT_KEY_ENV)
    p.add_argument("--key", help="Unsafe last-resort key; argv/history may expose it")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--base-image", default="ubuntu:24.04")
    return p


def run_agent(project: Path, agent: str, model: str, marker: str, args, env) -> bool:
    command = [
        sys.executable,
        "-m",
        "project_sandbox",
        str(project),
        args.base_image,
        "--agent",
        agent,
        "--agent-proxy",
        args.proxy,
        "--agent-proxy-key-env",
        args.key_env,
        "--model",
        model,
        "--timeout",
        str(args.timeout),
        "--prompt-text",
        f"Reply with exactly {marker} and nothing else.",
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=env,
            timeout=args.timeout + 30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"FAIL {agent}: timed out", file=sys.stderr)
        return False
    output = (proc.stdout + proc.stderr).replace(
        env[args.key_env], agent_proxy.REDACTED
    )
    if proc.returncode or marker not in output:
        print(
            f"FAIL {agent}: exit={proc.returncode}; expected marker missing",
            file=sys.stderr,
        )
        return False
    print(f"PASS {agent}")
    return True


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    agent_proxy.validate_url(args.proxy)
    key, source = agent_proxy.resolve_key(args.key_env, args.key)
    if source == "command line":
        print("WARNING: raw key was exposed in parent argv/history", file=sys.stderr)
    models = agent_proxy.discover_models(args.proxy, key)
    for model in (args.pi_model, args.opencode_model):
        if model not in models:
            raise SystemExit(f"Selected model {model!r} is unavailable")
    print("WARNING: this check makes two billable LLM requests.")
    print("The gateway is checked but never started, stopped, or reconfigured.")
    child_env = os.environ.copy()
    child_env[args.key_env] = key
    with tempfile.TemporaryDirectory(prefix="project-sandbox-agent-proxy-") as tmp:
        project = Path(tmp)
        pi_ok = run_agent(
            project, "pi", args.pi_model, "PI_PROXY_OK_7C91", args, child_env
        )
        oc_ok = run_agent(
            project,
            "opencode",
            f"agent-proxy/{args.opencode_model}",
            "OPENCODE_PROXY_OK_4A62",
            args,
            child_env,
        )
    return 0 if pi_ok and oc_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
