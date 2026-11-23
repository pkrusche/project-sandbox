#!/usr/bin/env python3
"""Run one billable headless request through each supported proxy agent.

Each agent's reply is read from its JSON session log (`--log`), not from the
raw output: Pi echoes the prompt, which contains the marker, so a search over
all output would pass even when the model's reply is empty. The final
assistant message must contain the marker and end with stop reason "stop".
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from project_sandbox import agent_proxy, transcript


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--proxy", default="http://127.0.0.1:4000/v1")
    default = os.environ.get("AGENT_PROXY_TEST_MODEL", "gpt-6-luna")
    p.add_argument("--pi-model", default=default)
    p.add_argument("--opencode-model", default=default)
    p.add_argument("--key-env", default=agent_proxy.DEFAULT_KEY_ENV)
    p.add_argument("--key", help="Unsafe last-resort key; argv/history may expose it")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--base-image", default="ubuntu:24.04")
    p.add_argument("--runtime", default="auto")
    p.add_argument(
        "--output-tail",
        type=int,
        default=40,
        help="Lines of redacted agent output to show when a check fails.",
    )
    return p


def final_reply(agent: str, log_path: Path) -> tuple[str, str | None, str] | None:
    """Return (text, stop reason, error) of the agent's final assistant reply."""
    if agent == "pi":
        message = transcript.final_pi_assistant_message(log_path)
        if message is None:
            return None
        stop = message.get("stopReason")
        error = message.get("errorMessage") or ""
        if stop != "stop" and not error:
            error = message.get("rawStopReason") or ""
        return transcript.pi_message_text(message), stop, str(error)
    reply = transcript.final_opencode_reply(log_path)
    return None if reply is None else (reply.text, reply.reason, reply.error)


def check_reply(agent: str, marker: str, log_path: Path) -> list[str]:
    """Return failure reasons for the agent's final assistant reply."""
    reply = final_reply(agent, log_path)
    if reply is None:
        return ["no final assistant reply in the session log"]
    text, stop, error = reply
    failures = []
    if marker not in text:
        shown = text.strip()[:200] or "<empty>"
        failures.append(f"reply text lacks {marker}: {shown!r}")
    if stop != "stop":
        failures.append(f"stop reason={stop!r} {error}".rstrip())
    elif error:
        failures.append(f"error: {error}")
    return failures


def run_agent(
    project: Path, log_dir: Path, agent: str, model: str, marker: str, args, env
) -> bool:
    log_path = log_dir / f"{agent}.jsonl"
    command = [
        sys.executable,
        "-m",
        "project_sandbox",
        str(project),
        args.base_image,
        "--runtime",
        args.runtime,
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
        "--log",
        str(log_path),
        "--prompt-text",
        f"Reply with exactly {marker} and nothing else. Do not use any tools.",
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
        output = (proc.stdout + proc.stderr).replace(
            env[args.key_env], agent_proxy.REDACTED
        )
        failures = [] if proc.returncode == 0 else [f"exit={proc.returncode}"]
    except subprocess.TimeoutExpired:
        output = ""
        failures = ["timed out"]
    failures += check_reply(agent, marker, log_path)
    if failures:
        print(f"FAIL {agent}: " + "; ".join(failures), file=sys.stderr)
        for line in output.splitlines()[-args.output_tail :]:
            print(f"  {agent}| {line}", file=sys.stderr)
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
        project = Path(tmp) / "project"
        log_dir = Path(tmp) / "logs"
        project.mkdir()
        log_dir.mkdir()
        pi_ok = run_agent(
            project, log_dir, "pi", args.pi_model, "PI_PROXY_OK_7C91", args, child_env
        )
        oc_ok = run_agent(
            project,
            log_dir,
            "opencode",
            f"agent-proxy/{args.opencode_model}",
            "OPENCODE_PROXY_OK_4A62",
            args,
            child_env,
        )
    return 0 if pi_ok and oc_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
