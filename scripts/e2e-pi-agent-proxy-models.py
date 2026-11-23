#!/usr/bin/env python3
"""Run a small headless Pi prompt through one model per gateway provider family.

Billable: makes one minimal LLM request per selected model through an already
running agentgateway (it is checked but never started, stopped, or
reconfigured). Requires a real container runtime.

For each model the script runs `project-sandbox --agent pi --agent-proxy ...
--prompt-text ...` with `--log`, then reads Pi's JSON event stream and checks
the final assistant message, not the raw output: Pi echoes the user prompt in
its events, so a marker search over all output passes even when the model's
reply is empty. Each reply must

  * contain the expected marker in its text content,
  * end with stopReason "stop", and
  * have been sent over the API that project-sandbox configures for that model
    family (Responses for OpenAI, chat completions for Claude and Gemini).

Usage:
  uv run python scripts/e2e-pi-agent-proxy-models.py [--runtime RUNTIME]
      [--base-image IMAGE] [--model MODEL ...] [--proxy URL] [--key-env NAME]
      [--timeout SECONDS] [--no-build] [--keep] [--output-tail LINES]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from project_sandbox import agent_proxy, config_agents, transcript

ROOT = Path(__file__).resolve().parents[1]
# One model per gateway provider family: OpenAI, Anthropic, Gemini.
DEFAULT_MODELS = ("gpt-6-luna", "claude-haiku-5-5", "gemini-4-hydrogen")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--proxy", default="http://127.0.0.1:4000/v1")
    p.add_argument(
        "--model",
        dest="models",
        action="append",
        help=f"Model to check; repeatable (default: {', '.join(DEFAULT_MODELS)})",
    )
    p.add_argument("--key-env", default=agent_proxy.DEFAULT_KEY_ENV)
    p.add_argument("--key", help="Unsafe last-resort key; argv/history may expose it")
    p.add_argument("--runtime", default="auto")
    p.add_argument("--base-image", default="python:3.12-slim")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--keep", action="store_true", help="Keep the test project and logs")
    p.add_argument(
        "--output-tail",
        type=int,
        default=40,
        help="Lines of redacted session output to show when a check fails.",
    )
    return p


def expected_api(model: str) -> str:
    chat_completions, _limits = config_agents._proxy_model_profile(model)
    return "openai-completions" if chat_completions else "openai-responses"


def marker_for(model: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", model.upper()).strip("_")
    return f"PI_PROXY_{slug}_OK"


final_assistant_message = transcript.final_pi_assistant_message
message_text = transcript.pi_message_text


def check_reply(model: str, message: dict | None) -> list[str]:
    """Return failure reasons for one model's final assistant message."""
    if message is None:
        return ["no assistant message_end event in the Pi log"]
    failures = []
    text = message_text(message)
    marker = marker_for(model)
    if marker not in text:
        shown = text.strip()[:200] or "<empty>"
        failures.append(f"reply text lacks {marker}: {shown!r}")
    stop = message.get("stopReason")
    if stop != "stop":
        detail = message.get("errorMessage") or message.get("rawStopReason") or ""
        failures.append(f"stopReason={stop!r} {detail}".rstrip())
    want_api = expected_api(model)
    if message.get("api") != want_api:
        failures.append(f"api={message.get('api')!r}, expected {want_api!r}")
    return failures


def run_model(
    model: str,
    project: Path,
    log_dir: Path,
    args,
    env: dict[str, str],
    *,
    build: bool,
) -> bool:
    marker = marker_for(model)
    log_path = log_dir / f"{marker.lower()}.jsonl"
    command = [
        sys.executable,
        "-m",
        "project_sandbox",
        "--runtime",
        args.runtime,
        "--agent",
        "pi",
        "--agent-proxy",
        args.proxy,
        "--agent-proxy-key-env",
        args.key_env,
        "--model",
        model,
        "--prompt-text",
        f"Reply with exactly {marker} and nothing else. Do not use any tools.",
        "--log",
        str(log_path),
        "--timeout",
        str(args.timeout),
    ]
    if not build:
        command.append("--no-build")
    command += [str(project), args.base_image]

    key = env[args.key_env]
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=args.timeout + 60,
            check=False,
        )
        output = (proc.stdout + proc.stderr).replace(key, agent_proxy.REDACTED)
        failures = [] if proc.returncode == 0 else [f"exit={proc.returncode}"]
    except subprocess.TimeoutExpired:
        output = ""
        failures = ["timed out"]

    message = final_assistant_message(log_path)
    failures += check_reply(model, message)
    if failures or message is None:
        print(f"FAIL {model}: " + "; ".join(failures), file=sys.stderr)
        for line in output.splitlines()[-args.output_tail :]:
            print(f"  {model}| {line}", file=sys.stderr)
        print(f"  {model}| log: {log_path}", file=sys.stderr)
        return False
    print(
        f"PASS {model} (api={message['api']}, reply={message_text(message).strip()!r})"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    models = args.models or list(DEFAULT_MODELS)
    agent_proxy.validate_url(args.proxy)
    key, source = agent_proxy.resolve_key(args.key_env, args.key)
    if source == "command line":
        print("WARNING: raw key was exposed in parent argv/history", file=sys.stderr)
    available = agent_proxy.discover_models(args.proxy, key)
    missing = [model for model in models if model not in available]
    if missing:
        raise SystemExit(
            f"Gateway does not serve {', '.join(missing)}; "
            f"available: {', '.join(available)}"
        )
    print(f"WARNING: this check makes {len(models)} billable LLM requests.")
    print("The gateway is checked but never started, stopped, or reconfigured.")

    child_env = os.environ.copy()
    child_env[args.key_env] = key
    # Apple's build VM cannot read the macOS per-user temp directory, so keep
    # the throwaway project under the checkout (as e2e-pi-ollama.sh does).
    tmp_base = ROOT / ".project-sandbox" / "e2e"
    tmp_base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pi-agent-proxy-models.", dir=tmp_base))
    project = work / "project"
    log_dir = work / "logs"
    project.mkdir()
    log_dir.mkdir()
    (project / "README.md").write_text(
        "# pi agent-proxy models e2e\n", encoding="utf-8"
    )

    results: list[bool] = []
    try:
        for index, model in enumerate(models):
            # Build once; later sessions reuse the same image.
            build = not args.no_build and index == 0
            results.append(
                run_model(model, project, log_dir, args, child_env, build=build)
            )
    finally:
        passed = len(results) == len(models) and all(results)
        if args.keep or not passed:
            print(f"Test project and logs kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
