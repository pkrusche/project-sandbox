#!/usr/bin/env python3
"""Opt-in, billable end-to-end checks for --agent copilot.

Runs real GitHub Copilot CLI sessions through project-sandbox, so it needs a
Copilot subscription and a token the CLI can resolve: a fine-grained PAT with
only "Copilot Requests" in pass (copilot-github-token, or
copilot-github-token/<tenant>.ghe.com), or a `gh auth login` for the host.
Every check except the bash half of `isolation` sends Copilot requests.

Checks (default: all; `tenant` is skipped unless --copilot-host names a
GHE.com tenant):

  headless-ok     a trivial prompt exits 0, the JSONL parses, a transcript renders
  headless-fail   an invalid --model yields a nonzero CLI exit (raw exit and the
                  final record are captured)
  trusted-folder  no trust prompt/error in a -p run; $COPILOT_HOME/config.json
                  still lists /workspace
  isolation       Copilot API reachable, unrelated host blocked, no built-in MCP
                  tools; a bash session has no Copilot token mount or env
  tenant          tenant token resolution, a headless run through the pinned
                  tenant hosts, copilot-api.<tenant> reachable

--capture-dir DIR keeps each check's JSONL stream and exit code (token
redacted) so real records can become transcript fixtures.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from project_sandbox import copilot_auth, session

ROOT = Path(__file__).resolve().parents[1]
CHECKS = ("headless-ok", "headless-fail", "trusted-folder", "isolation", "tenant")
PERSONAL_HOST = copilot_auth.DEFAULT_HOST
OK_MARKER = "COPILOT_E2E_OK_51F3"
INVALID_MODEL = "project-sandbox-e2e-no-such-model"
# Built-in GitHub MCP tools are named after their server; with
# --disable-builtin-mcps none may appear in a run's tool calls.
BUILTIN_MCP_MARKER = "github-mcp-server"

# Runs inside the sandbox (via Copilot's shell tool, or a bash session) and
# writes its findings to /workspace so the host can read them back.
AUDIT_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

mode = sys.argv[1]
reachable = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else []
blocked = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else []
failures = []


def http_status(url):
    proc = subprocess.run(
        ["curl", "--silent", "--output", "/dev/null", "--max-time", "10",
         "--write-out", "%{http_code}", url],
        capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip() or "000"


for url in reachable:
    if http_status(url) == "000":
        failures.append("expected reachable: " + url)
for url in blocked:
    if http_status(url) != "000":
        failures.append("expected blocked by the firewall: " + url)

if mode == "bash":
    if Path("/project-sandbox-secrets/copilot").exists():
        failures.append("bash session has /project-sandbox-secrets/copilot")
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            failures.append("bash session has " + name)
    if os.environ.get("COPILOT_HOME"):
        failures.append("bash session has COPILOT_HOME")
else:
    home = Path(os.environ.get("COPILOT_HOME", ""))
    if not home.is_dir():
        failures.append("COPILOT_HOME is not a directory")
    token = os.environ.get("COPILOT_GITHUB_TOKEN", "")
    for path in home.rglob("*") if token and home.is_dir() else ():
        try:
            if path.is_file() and token in path.read_text(errors="replace"):
                failures.append("token copied into COPILOT_HOME: " + str(path))
        except OSError:
            pass

result = {"mode": mode, "ok": not failures, "failures": failures}
Path("copilot-audit-" + mode + ".json").write_text(json.dumps(result) + "\n")
print(json.dumps(result))
"""


@dataclass(frozen=True)
class Run:
    returncode: int
    output: str
    log_path: Path


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--runtime",
        default="auto",
        choices=("auto", "apple-container", "docker", "podman"),
    )
    p.add_argument("--base-image", default="python:3.12-slim")
    p.add_argument("--image-tag", default="project-sandbox-e2e-copilot:latest")
    p.add_argument(
        "--copilot-host",
        metavar="HOST",
        help="GHE.com tenant (<tenant>.ghe.com) for the `tenant` check; "
        "the other checks always use github.com",
    )
    p.add_argument(
        "--check",
        action="append",
        choices=CHECKS,
        metavar="NAME",
        help=f"run only this check (repeatable; one of: {', '.join(CHECKS)})",
    )
    p.add_argument(
        "--capture-dir",
        metavar="DIR",
        help="write each check's JSONL stream and exit code (token redacted)",
    )
    p.add_argument(
        "--output-tail",
        type=int,
        default=40,
        metavar="N",
        help="lines of redacted CLI output shown for a failing check (default: 40)",
    )
    p.add_argument("--model", help="model for the passing checks (default: agent's)")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--keep", action="store_true")
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = parser()
    args = p.parse_args(argv)
    if args.copilot_host is not None:
        try:
            host = copilot_auth.validate_host(args.copilot_host)
        except SystemExit as exc:
            p.error(str(exc))
        if not copilot_auth.is_tenant(host):
            p.error("--copilot-host is for a <tenant>.ghe.com tenant")
        args.copilot_host = host
    if args.output_tail < 0:
        p.error("--output-tail must be >= 0")
    return args


def selected_checks(args: argparse.Namespace) -> list[tuple[str, str | None]]:
    """Return (check, skip reason or None) in execution order."""
    wanted = set(args.check or CHECKS)
    plan: list[tuple[str, str | None]] = []
    for name in CHECKS:
        if name not in wanted:
            continue
        if name == "tenant" and not args.copilot_host:
            plan.append((name, "pass --copilot-host <tenant>.ghe.com"))
        else:
            plan.append((name, None))
    return plan


def hosts_needed(plan: list[tuple[str, str | None]], args) -> list[str]:
    hosts: list[str] = []
    for name, skip in plan:
        if skip is not None:
            continue
        host = args.copilot_host if name == "tenant" else PERSONAL_HOST
        if host not in hosts:
            hosts.append(host)
    return hosts


def redact(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    return session.redact_text(text, secrets)


def tail(text: str, lines: int) -> str:
    if lines == 0:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def json_records(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def build_command(
    args: argparse.Namespace,
    project: Path,
    *,
    agent: str,
    prompt: str,
    log_path: Path,
    host: str = PERSONAL_HOST,
    model: str | None = None,
) -> list[str]:
    """project-sandbox argv for one check. Never carries the token."""
    command = [
        sys.executable,
        "-m",
        "project_sandbox",
        "--runtime",
        args.runtime,
        "--image-tag",
        args.image_tag,
        "--agent",
        agent,
        "--prompt-text",
        prompt,
        "--log",
        str(log_path),
        "--timeout",
        str(args.timeout),
    ]
    if agent == "copilot" and host != PERSONAL_HOST:
        command += ["--copilot-host", host]
    if model:
        command += ["--model", model]
    if args.no_build:
        command.append("--no-build")
    return [*command, str(project), args.base_image]


def write_capture(capture_dir: Path, name: str, run: Run, secrets: list[str]) -> None:
    capture_dir.mkdir(parents=True, exist_ok=True)
    log_text = (
        run.log_path.read_text(encoding="utf-8", errors="replace")
        if run.log_path.exists()
        else ""
    )
    records = json_records(redact(log_text, secrets))
    (capture_dir / f"{name}.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )
    (capture_dir / f"{name}.exit.json").write_text(
        json.dumps(
            {
                "check": name,
                "exit_code": run.returncode,
                "final_record": records[-1] if records else None,
                "records": len(records),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


class Suite:
    def __init__(self, args: argparse.Namespace, project: Path, secrets: list[str]):
        self.args = args
        self.project = project
        self.secrets = secrets
        self.capture_dir = Path(args.capture_dir) if args.capture_dir else None

    def run(self, name: str, **kwargs) -> Run:
        log_path = self.project / ".e2e-logs" / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = build_command(self.args, self.project, log_path=log_path, **kwargs)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        run = Run(
            completed.returncode,
            redact(completed.stdout + completed.stderr, self.secrets),
            log_path,
        )
        if self.capture_dir is not None:
            write_capture(self.capture_dir, name, run, self.secrets)
        return run

    def log_text(self, run: Run) -> str:
        if not run.log_path.exists():
            return ""
        return redact(run.log_path.read_text(errors="replace"), self.secrets)

    def audit(self, mode: str) -> dict:
        path = self.project / f"copilot-audit-{mode}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "failures": [f"{path.name} was not written"]}

    def audit_prompt(self, mode: str, reachable: list[str], blocked: list[str]) -> str:
        """Prompt that runs the audit; clears any earlier result first."""
        (self.project / f"copilot-audit-{mode}.json").unlink(missing_ok=True)
        empty = "''"
        command = (
            f"python3 copilot-audit.py {mode} "
            f"{','.join(reachable) or empty} {','.join(blocked) or empty}"
        )
        if mode == "bash":
            return command
        return (
            "Run exactly this shell command in the current directory and then "
            f"reply with its output verbatim, nothing else: {command}"
        )

    # --- checks -------------------------------------------------------------

    def headless_ok(self, host: str = PERSONAL_HOST, name: str = "headless-ok"):
        run = self.run(
            name,
            agent="copilot",
            prompt=f"Reply with exactly this text and nothing else: {OK_MARKER}",
            host=host,
            model=self.args.model,
        )
        failures = []
        if run.returncode != 0:
            failures.append(f"exit {run.returncode}")
        log = self.log_text(run)
        if not json_records(log):
            failures.append("no JSONL records in the session log")
        if OK_MARKER not in log:
            failures.append("reply marker missing from the JSONL stream")
        if not run.log_path.with_suffix(".md").exists():
            failures.append("markdown transcript was not written")
        return run, failures

    def headless_fail(self):
        run = self.run(
            "headless-fail",
            agent="copilot",
            prompt="Reply with OK.",
            model=INVALID_MODEL,
        )
        records = json_records(self.log_text(run))
        print(f"  raw CLI exit: {run.returncode}")
        if records:
            print(f"  final record: {json.dumps(records[-1], sort_keys=True)[:300]}")
        failures = [] if run.returncode != 0 else ["invalid --model exited 0"]
        return run, failures

    def trusted_folder(self):
        run = self.run(
            "trusted-folder",
            agent="copilot",
            prompt=(
                "Run exactly this shell command and reply with its output "
                'verbatim: cat "$COPILOT_HOME/config.json"'
            ),
            model=self.args.model,
        )
        log = self.log_text(run).lower()
        failures = []
        if run.returncode != 0:
            failures.append(f"exit {run.returncode}")
        for marker in ("do you trust", "not trusted", "trust this folder"):
            if marker in log:
                failures.append(f"trust prompt/error in the stream: {marker!r}")
        if "trusted_folders" not in log or "/workspace" not in log:
            failures.append("config.json output did not list /workspace")
        return run, failures

    def isolation(self):
        failures = []
        copilot = self.run(
            "isolation",
            agent="copilot",
            prompt=self.audit_prompt(
                "copilot",
                ["https://api.githubcopilot.com/"],
                ["https://example.com/"],
            ),
            model=self.args.model,
        )
        if copilot.returncode != 0:
            failures.append(f"copilot run exit {copilot.returncode}")
        failures += [f"copilot: {f}" for f in self.audit("copilot")["failures"]]
        if BUILTIN_MCP_MARKER in self.log_text(copilot):
            failures.append("a built-in GitHub MCP tool appeared in the stream")
        bash = self.run(
            "isolation-bash",
            agent="bash",
            prompt=self.audit_prompt("bash", [], ["https://example.com/"]),
        )
        if bash.returncode != 0:
            failures.append(f"bash run exit {bash.returncode}")
        failures += [f"bash: {f}" for f in self.audit("bash")["failures"]]
        combined = Run(
            copilot.returncode or bash.returncode,
            copilot.output + "\n" + bash.output,
            copilot.log_path,
        )
        return combined, failures

    def tenant(self):
        host = self.args.copilot_host
        run, failures = self.headless_ok(host=host, name="tenant")
        reach = self.run(
            "tenant-reachability",
            agent="copilot",
            host=host,
            prompt=self.audit_prompt("copilot", [f"https://copilot-api.{host}/"], []),
            model=self.args.model,
        )
        failures += [f"tenant: {f}" for f in self.audit("copilot")["failures"]]
        if reach.returncode != 0:
            failures.append(f"tenant reachability run exit {reach.returncode}")
        combined = Run(
            run.returncode or reach.returncode,
            run.output + "\n" + reach.output,
            run.log_path,
        )
        return combined, failures


def resolve_secrets(hosts: list[str]) -> list[str]:
    """Resolve each needed host's token once, only so it can be redacted."""
    secrets = []
    for host in hosts:
        token, source = copilot_auth.resolve_token(host)
        if source == copilot_auth.SOURCE_GH:
            print(copilot_auth.scope_warning(copilot_auth.default_pass_entry(host)))
        secrets.append(token)
    return secrets


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = selected_checks(args)
    for name, skip in plan:
        if skip:
            print(f"SKIP: {name} ({skip})")
    runnable = [name for name, skip in plan if skip is None]
    if not runnable:
        return 0
    secrets = resolve_secrets(hosts_needed(plan, args))

    temp_root = ROOT / ".project-sandbox"
    temp_root.mkdir(exist_ok=True)
    project = Path(tempfile.mkdtemp(prefix="e2e-copilot.", dir=temp_root))
    (project / "README.md").write_text("# copilot e2e\n", encoding="utf-8")
    (project / "copilot-audit.py").write_text(AUDIT_SOURCE, encoding="utf-8")
    suite = Suite(args, project, secrets)
    failed = []
    try:
        for name in runnable:
            print(f"RUN:  {name}")
            run, failures = getattr(suite, name.replace("-", "_"))()
            if failures:
                failed.append(name)
                for failure in failures:
                    print(f"FAIL: {name}: {redact(failure, secrets)}")
                shown = tail(run.output, args.output_tail)
                if shown:
                    print(f"--- {name} output (last {args.output_tail} lines) ---")
                    print(shown)
            else:
                print(f"PASS: {name}")
    finally:
        if args.keep:
            print(f"Test project kept for inspection: {project}")
        else:
            shutil.rmtree(project, ignore_errors=True)
    print(
        f"{len(runnable) - len(failed)}/{len(runnable)} Copilot checks passed"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
