#!/usr/bin/env python3
"""Exercise Internet-proxy routing, bypass prevention, and service isolation.

This is an opt-in destructive integration test: it stops and restarts the two
external services named on the command line while one sandbox remains running.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from project_sandbox import agent_proxy, internet_proxy

AUDIT_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

failures = []

def check(condition, message):
    print(("PASS: " if condition else "FAIL: ") + message, flush=True)
    if not condition:
        failures.append(message)

def curl(*args, timeout=12):
    return subprocess.run(
        ["curl", "--silent", "--show-error", "--max-time", str(timeout), *args],
        capture_output=True, text=True, check=False,
    )

def internet_works():
    return curl("--fail", __ALLOWED_URL__).returncode == 0

def gateway_request(expect_success=True):
    body = json.dumps({
        "model": __MODEL__,
        "messages": [{"role": "user", "content": "Reply with only OK"}],
        "max_tokens": 8,
    }).encode()
    request = urllib.request.Request(
        __GATEWAY_URL__.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
            ok = 200 <= response.status < 300 and bool(payload.get("choices"))
    except Exception:
        ok = False
    return ok if expect_success else not ok

def blocked_connection(address, socktype, port, payload=b""):
    sock = socket.socket(socket.AF_INET, socktype)
    sock.settimeout(3)
    try:
        sock.connect((address, port))
        if payload:
            sock.sendall(payload)
            sock.recv(1)
        return False
    except OSError:
        return True
    finally:
        sock.close()

check(internet_works(), "allowlisted HTTPS succeeds through the Internet proxy")
denied = curl("--fail-with-body", "--include", __BLOCKED_URL__)
denial_text = (denied.stdout + denied.stderr).lower()
check(
    denied.returncode != 0 and any(word in denial_text for word in __DENIAL_WORDS__),
    "blocked destination returns a recognizable proxy-policy denial",
)

direct = curl("--noproxy", "*", __ALLOWED_URL__)
check(direct.returncode != 0, "curl --noproxy cannot bypass the sandbox firewall")
unproxied_env = os.environ.copy()
for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    unproxied_env.pop(name, None)
unset = subprocess.run(
    ["curl", "--silent", "--show-error", "--max-time", "5", __ALLOWED_URL__],
    env=unproxied_env, capture_output=True, text=True, check=False,
)
check(unset.returncode != 0, "Internet fails with every proxy variable unset")
check(blocked_connection(__PUBLIC_IP__, socket.SOCK_STREAM, 443), "raw public TCP/443 is blocked")
dns_query = bytes.fromhex("123401000001000000000000076578616d706c6503636f6d0000010001")
check(blocked_connection(__DNS_IP__, socket.SOCK_DGRAM, 53, dns_query), "raw public UDP is blocked")
check(blocked_connection(__DNS_IP__, socket.SOCK_STREAM, 53), "direct DNS is blocked")
check(gateway_request(), "AI completion succeeds through Agentgateway")

Path("baseline.ready").write_text("ready\n")
for marker in ("proxy-stopped.go", "proxy-restarted.go", "gateway-stopped.go"):
    deadline = time.monotonic() + __PHASE_TIMEOUT__
    while not Path(marker).exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    check(Path(marker).exists(), "host signalled " + marker)
    if marker == "proxy-stopped.go":
        check(not internet_works(), "proxy loss fails closed without direct fallback")
        check(gateway_request(), "AI still works while the Internet proxy is stopped")
        Path("proxy-stopped.done").write_text("done\n")
    elif marker == "proxy-restarted.go":
        check(internet_works(), "proxy restart restores service at the stable endpoint")
        check(gateway_request(), "Agentgateway remains independent after proxy restart")
        Path("proxy-restarted.done").write_text("done\n")
    else:
        check(internet_works(), "ordinary Internet still works while Agentgateway is stopped")
        check(gateway_request(False), "AI fails when Agentgateway is stopped")
        Path("gateway-stopped.done").write_text("done\n")

Path("result.json").write_text(json.dumps({"ok": not failures, "failures": failures}, indent=2) + "\n")
raise SystemExit(0 if not failures else 1)
"""


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--runtime", required=True, choices=("apple-container", "docker", "podman")
    )
    p.add_argument("--base-image", default="python:3.12-slim")
    p.add_argument("--internet-proxy", default="http://127.0.0.1:18080")
    p.add_argument("--gateway", default="http://127.0.0.1:4000/v1")
    p.add_argument("--key-env", default=agent_proxy.DEFAULT_KEY_ENV)
    p.add_argument(
        "--model", default=os.environ.get("AGENT_PROXY_TEST_MODEL", "gpt-5-mini")
    )
    p.add_argument("--allowed-url", default="https://github.com/")
    p.add_argument("--blocked-url", required=True)
    p.add_argument("--denial-words", default="denied,blocked,forbidden,policy")
    p.add_argument("--public-ip", default="1.1.1.1")
    p.add_argument("--dns-ip", default="1.1.1.1")
    p.add_argument("--internet-proxy-dir", required=True, type=Path)
    p.add_argument("--agentgateway-dir", required=True, type=Path)
    p.add_argument("--internet-proxy-control", default="./run.py {action}")
    p.add_argument("--agentgateway-control", default="./run.py {action}")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--keep", action="store_true")
    return p


def control(template: str, action: str, cwd: Path) -> None:
    command = shlex.split(template.format(action=action))
    print(f"CONTROL ({cwd}): {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def wait_for(path: Path, process: subprocess.Popen[str], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise RuntimeError(
                f"sandbox exited with status {process.returncode} before {path.name}"
            )
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {path.name}")


def render_audit(args: argparse.Namespace, gateway_url: str) -> str:
    replacements = {
        "__ALLOWED_URL__": repr(args.allowed_url),
        "__BLOCKED_URL__": repr(args.blocked_url),
        "__DENIAL_WORDS__": repr(
            tuple(x.strip().lower() for x in args.denial_words.split(",") if x.strip())
        ),
        "__PUBLIC_IP__": repr(args.public_ip),
        "__DNS_IP__": repr(args.dns_ip),
        "__GATEWAY_URL__": repr(gateway_url),
        "__MODEL__": repr(args.model),
        "__PHASE_TIMEOUT__": str(args.timeout),
    }
    source = textwrap.dedent(AUDIT_SOURCE)
    for old, new in replacements.items():
        source = source.replace(old, new)
    return source


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    proxy = internet_proxy.parse(args.internet_proxy)
    internet_proxy.preflight(proxy)
    agent_proxy.validate_url(args.gateway)
    key, _ = agent_proxy.resolve_key(args.key_env, None)
    for directory in (args.internet_proxy_dir, args.agentgateway_dir):
        if not directory.is_dir():
            raise SystemExit(f"control directory does not exist: {directory}")

    root = Path(__file__).resolve().parents[1]
    temp_root = root / ".project-sandbox"
    temp_root.mkdir(exist_ok=True)
    project = Path(tempfile.mkdtemp(prefix="internet-proxy-e2e.", dir=temp_root))
    gateway_url = agent_proxy.forwarded_url(
        args.gateway, hostname=internet_proxy.HOSTNAME
    )
    (project / "audit.py").write_text(render_audit(args, gateway_url), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "project_sandbox",
        "--runtime",
        args.runtime,
        "--agent",
        "bash",
        "--internet-proxy",
        args.internet_proxy,
        "--agent-proxy",
        args.gateway,
        "--agent-proxy-key-env",
        args.key_env,
        "--model",
        args.model,
        "--prompt-text",
        "python audit.py",
        "--timeout",
        str(args.timeout),
    ]
    if args.no_build:
        command.append("--no-build")
    command += [str(project), args.base_image]
    env = os.environ.copy()
    env[args.key_env] = key
    process: subprocess.Popen[str] | None = None
    stopped = set()
    try:
        process = subprocess.Popen(command, cwd=root, env=env, text=True)
        wait_for(project / "baseline.ready", process, args.timeout)
        control(args.internet_proxy_control, "stop", args.internet_proxy_dir)
        stopped.add("proxy")
        (project / "proxy-stopped.go").write_text("go\n")
        wait_for(project / "proxy-stopped.done", process, args.timeout)
        control(args.internet_proxy_control, "restart", args.internet_proxy_dir)
        stopped.discard("proxy")
        internet_proxy.preflight(proxy)
        (project / "proxy-restarted.go").write_text("go\n")
        wait_for(project / "proxy-restarted.done", process, args.timeout)
        control(args.agentgateway_control, "stop", args.agentgateway_dir)
        stopped.add("gateway")
        (project / "gateway-stopped.go").write_text("go\n")
        wait_for(project / "gateway-stopped.done", process, args.timeout)
        return_code = process.wait(timeout=args.timeout)
        result_path = project / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        if return_code or not result.get("ok"):
            for failure in result.get("failures", ["sandbox audit did not complete"]):
                print(f"FAIL: {failure}", file=sys.stderr)
            return 1
        print("PASS: Internet proxy, bypass, separation, and fail-closed checklist")
        return 0
    finally:
        if "proxy" in stopped:
            control(args.internet_proxy_control, "restart", args.internet_proxy_dir)
        if "gateway" in stopped:
            control(args.agentgateway_control, "restart", args.agentgateway_dir)
        if process is not None and process.poll() is None:
            process.terminate()
        if args.keep:
            print(f"Test project kept for inspection: {project}")
        else:
            shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
