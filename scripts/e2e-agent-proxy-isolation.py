#!/usr/bin/env python3
"""Verify agent-proxy credential isolation and gateway-only container egress."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from project_sandbox import agent_proxy

AUDIT_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

failures = []

def check(condition, message):
    if not condition:
        failures.append(message)

key = os.environ.get("OPENAI_API_KEY", "")
base_url = os.environ.get("OPENAI_BASE_URL", "")
model = os.environ.get("OPENAI_MODEL", "")
check(bool(key), "OPENAI_API_KEY is missing")
check(bool(base_url), "OPENAI_BASE_URL is missing")
check(model == __MODEL__, "OPENAI_MODEL does not match the selected model")

sensitive_names = {
    name
    for name, value in os.environ.items()
    if value and any(marker in name.upper() for marker in (
        "API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"
    ))
}
check(
    sensitive_names == {"OPENAI_API_KEY"},
    "unexpected credential-like environment variables: "
    + ", ".join(sorted(sensitive_names - {"OPENAI_API_KEY"})),
)

home = Path.home()
pi_models_path = home / ".pi/agent/models.json"
pi_settings_path = home / ".pi/agent/settings.json"
opencode_path = home / ".config/opencode/opencode.json"
for path in (pi_models_path, pi_settings_path, opencode_path):
    check(path.is_file(), f"missing proxy config: {path}")

if all(path.is_file() for path in (pi_models_path, pi_settings_path, opencode_path)):
    pi_models = json.loads(pi_models_path.read_text())
    pi_settings = json.loads(pi_settings_path.read_text())
    opencode = json.loads(opencode_path.read_text())
    pi_provider = pi_models["providers"]["agent-proxy"]
    oc_provider = opencode["provider"]["agent-proxy"]
    check(pi_provider["baseUrl"] == base_url, "Pi proxy URL mismatch")
    check(pi_provider["apiKey"] == key, "Pi gateway key mismatch")
    check(model in [item["id"] for item in pi_provider["models"]], "Pi model missing")
    check(pi_settings.get("defaultProvider") == "agent-proxy", "Pi provider mismatch")
    check(pi_settings.get("defaultModel") == model, "Pi default model mismatch")
    check(oc_provider["options"]["baseURL"] == base_url, "OpenCode proxy URL mismatch")
    check(oc_provider["options"]["apiKey"] == key, "OpenCode gateway key mismatch")
    check(model in oc_provider["models"], "OpenCode model missing")
    check(opencode.get("model") == f"agent-proxy/{model}", "OpenCode default model mismatch")

for path in (
    home / ".claude/.credentials.json",
    home / ".claude.json",
    home / ".codex/auth.json",
    home / ".pi/agent/auth.json",
    home / ".config/opencode/auth.json",
    home / ".local/share/opencode/auth.json",
):
    check(not path.exists(), f"unexpected host credential file: {path}")

secret_root = Path("/project-sandbox-secrets")
secret_files = [path for path in secret_root.rglob("*") if path.is_file()]
check(not secret_files, "credential files were mounted under /project-sandbox-secrets")

request = urllib.request.Request(
    base_url.rstrip("/") + "/models",
    headers={"Authorization": f"Bearer {key}"},
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        check(200 <= response.status < 300, "gateway /models returned a non-2xx status")
except Exception as exc:
    failures.append(f"gateway /models is unreachable: {type(exc).__name__}")

for blocked_url in (
    "https://api.openai.com/v1/models",
    "https://api.anthropic.com/v1/models",
    "https://github.com/",
    "https://example.com/",
):
    try:
        urllib.request.urlopen(blocked_url, timeout=3)
    except urllib.error.HTTPError as exc:
        failures.append(
            f"unexpected external connectivity: {blocked_url} (HTTP {exc.code})"
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    else:
        failures.append(f"unexpected external connectivity: {blocked_url}")

result = {"ok": not failures, "failures": failures}
Path("proxy-isolation-result.json").write_text(json.dumps(result, indent=2) + "\n")
raise SystemExit(0 if not failures else 1)
"""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--runtime",
        default="auto",
        choices=("auto", "apple-container", "docker", "podman"),
    )
    p.add_argument("--base-image", default="python:3.12-slim")
    p.add_argument("--proxy", default="http://127.0.0.1:4000/v1")
    p.add_argument(
        "--model", default=os.environ.get("AGENT_PROXY_TEST_MODEL", "gpt-5-mini")
    )
    p.add_argument("--key-env", default=agent_proxy.DEFAULT_KEY_ENV)
    p.add_argument("--key", help="Unsafe last-resort key; argv/history may expose it")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--keep", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    agent_proxy.validate_url(args.proxy)
    key, source = agent_proxy.resolve_key(args.key_env, args.key)
    if source == "command line":
        print("WARNING: raw key was exposed in parent argv/history", file=sys.stderr)
    models = agent_proxy.discover_models(args.proxy, key)
    if args.model not in models:
        raise SystemExit(f"Selected model {args.model!r} is unavailable")

    root = Path(__file__).resolve().parents[1]
    project = Path(tempfile.mkdtemp(prefix="agent-proxy-isolation."))
    try:
        audit = AUDIT_SOURCE.replace("__MODEL__", repr(args.model))
        (project / "proxy-isolation-audit.py").write_text(
            textwrap.dedent(audit), encoding="utf-8"
        )
        command = [
            sys.executable,
            "-m",
            "project_sandbox",
            "--runtime",
            args.runtime,
            "--agent",
            "bash",
            "--agent-proxy",
            args.proxy,
            "--agent-proxy-key-env",
            args.key_env,
            "--model",
            args.model,
            "--prompt-text",
            "python proxy-isolation-audit.py",
            "--timeout",
            str(args.timeout),
        ]
        if args.no_build:
            command.append("--no-build")
        command += [str(project), args.base_image]
        env = os.environ.copy()
        env[args.key_env] = key
        proc = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout + 30,
            check=False,
        )
        output = (proc.stdout + proc.stderr).replace(key, agent_proxy.REDACTED)
        result_path = project / "proxy-isolation-result.json"
        if proc.returncode != 0 or not result_path.is_file():
            print(output, file=sys.stderr)
            print("FAIL: isolation audit did not complete", file=sys.stderr)
            return 1
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not result.get("ok"):
            for failure in result.get("failures", []):
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1
        print("PASS: gateway-only network and credential isolation verified")
        return 0
    finally:
        if args.keep:
            print(f"Test project kept for inspection: {project}")
        else:
            shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
