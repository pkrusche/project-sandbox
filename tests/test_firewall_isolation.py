"""The firewall script runs as root and must not load workspace code or config.

isolate-firewall-helpers tasks 1.1-1.3: the script changes to ``/`` first, its
embedded Python helpers run isolated (``python3 -I``), every ``curl`` skips
``.curlrc`` (``curl -q``), and the script reads no caller environment.
"""

import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from project_sandbox import firewall

SHADOW_MODULES = ("ipaddress", "socket", "shlex", "ssl", "pathlib")


def _variants(root: Path) -> dict[str, str]:
    """Render every policy variant with all optional root-run commands on."""
    texts = {}
    normal = root / "normal"
    normal.mkdir()
    texts["normal"] = firewall.render(
        normal,
        extra_domains=["example.com"],
        allow_github=True,
        pi_ollama=True,
        agent_proxy_port=4000,
    ).read_text()
    destinations = [
        firewall.TcpDestination("Internet proxy", "localhost", 3128),
        firewall.TcpDestination(
            "Agent proxy", "gateway.example", 443, scheme="https", local=False
        ),
    ]
    proxy = root / "internet-proxy"
    proxy.mkdir()
    texts["internet-proxy"] = firewall.render(
        proxy,
        extra_domains=[],
        policy=firewall.INTERNET_PROXY,
        local_destinations=destinations,
        agent_proxy_port=443,
        agent_proxy_hostname="gateway.example",
        agent_proxy_url="https://gateway.example:443/",
    ).read_text()
    sidecar = root / "sidecar"
    sidecar.mkdir()
    texts["sidecar"] = firewall.render(
        sidecar,
        extra_domains=[],
        policy=firewall.INTERNET_PROXY,
        local_destinations=[
            firewall.TcpDestination("Internet proxy", "host.docker.internal", 3128)
        ],
        egress_sidecar=True,
    ).read_text()
    return texts


def _heredoc(text: str, tag: str) -> tuple[list[str], str]:
    """Return the interpreter flags and body of a ``python3 ... - <<'TAG'``."""
    match = re.search(
        r"python3((?: -[A-Za-z]+)*) -(?: [^<\n]*)? <<'" + tag + r"'\n(.*?)\n" + tag,
        text,
        re.DOTALL,
    )
    assert match, f"no {tag} heredoc"
    return match.group(1).split(), match.group(2)


def _shell_code(text: str) -> list[str]:
    """Shell lines with heredoc bodies, comments and single-quoted text removed."""
    lines = []
    in_heredoc = None
    for line in text.splitlines():
        if in_heredoc:
            if line == in_heredoc:
                in_heredoc = None
            continue
        heredoc = re.search(r"<<'(\w+)'", line)
        if heredoc:
            in_heredoc = heredoc.group(1)
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(re.sub(r"'[^']*'", "''", line))
    return lines


def caller_environment_references(text: str) -> set[str]:
    """Variables the script reads but never assigns itself."""
    code = "\n".join(_shell_code(text))
    referenced = set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", code))
    assigned = set(
        re.findall(r"(?:^|[\s;(])([A-Za-z_][A-Za-z0-9_]*)\+?=", code, re.MULTILINE)
    )
    assigned |= set(re.findall(r"mapfile -t ([A-Za-z_]\w*)", code))
    assigned |= set(re.findall(r"\bfor ([A-Za-z_]\w*) in\b", code))
    assigned |= set(re.findall(r"\bread -r ([A-Za-z_]\w*)", code))
    if 'eval "$ENDPOINT_ADDRESSES"' in code:
        # The endpoint helper prints ENDPOINT_<n>=(...) assignments.
        assigned |= {name for name in referenced if re.fullmatch(r"ENDPOINT_\d+", name)}
    return referenced - assigned


def relative_paths(text: str) -> list[str]:
    """Relative path arguments, redirections or directory changes."""
    found = []
    for line in _shell_code(text):
        code = re.sub(r'"[^"$]*"', '""', line)
        found.extend(re.findall(r"(?:^|\s)(\.{1,2}/\S*|~\S*)", code))
        for target in re.findall(r"(?<![<0-9&])[0-9]?>>?\s*([^\s&;|)]+)", code):
            if not target.startswith(("/", "$", '"$')):
                found.append(target)
        for target in re.findall(r"(?<![<(])<\s*([^\s<(;|)]+)", code):
            if not target.startswith(("/", "$", '"$', "''")):
                found.append(target)
        for target in re.findall(r"(?:^|[\s;&])(?:cd|source|\.)\s+(\S+)", code):
            if target != "/":
                found.append(target)
    return found


class InterpreterAndCurlFlagTests(unittest.TestCase):
    def test_every_variant_runs_python_isolated_and_curl_without_curlrc(self):
        with tempfile.TemporaryDirectory() as tmp:
            texts = _variants(Path(tmp))
        for variant, text in texts.items():
            with self.subTest(variant=variant):
                code = _shell_code(text)
                pythons = [line for line in code if re.search(r"\bpython3\b", line)]
                curls = [line for line in code if re.search(r"\bcurl\b", line)]
                self.assertTrue(pythons, "variant exercises no Python helper")
                for line in pythons:
                    self.assertRegex(line, r"\bpython3 -I -( |$)")
                for line in curls:
                    self.assertRegex(line, r"\bcurl -q ")
                commands = [line for line in code if line.strip()]
                self.assertEqual(commands[:2], ["set -euo pipefail", "cd /"])
        self.assertTrue(
            any(re.search(r"\bcurl\b", line) for line in _shell_code(texts["normal"]))
        )


class ShadowModuleTests(unittest.TestCase):
    """Run the rendered helpers from a directory full of shadow modules."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.fired = root / "fired"
        for name in SHADOW_MODULES:
            (self.workspace / f"{name}.py").write_text(
                f"with open({str(self.fired)!r}, 'a') as f:\n"
                f"    f.write({name!r} + '\\n')\n",
                encoding="utf-8",
            )
        self.hosts = root / "hosts"
        self.hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(self.listener.close)
        port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()
        render_dir = root / "render"
        render_dir.mkdir()
        self.text = firewall.render(
            render_dir,
            extra_domains=[],
            policy=firewall.INTERNET_PROXY,
            local_destinations=[
                firewall.TcpDestination("Internet proxy", "127.0.0.1", port),
                firewall.TcpDestination(
                    "Agent proxy", "192.0.2.10", 443, scheme="https", local=False
                ),
            ],
        ).read_text()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            conn.close()

    def _run(self, tag: str, *, isolated: bool, env: dict | None = None):
        flags, body = _heredoc(self.text, tag)
        if not isolated:
            flags = [flag for flag in flags if flag != "-I"]
        body = body.replace('Path("/etc/hosts")', f"Path({str(self.hosts)!r})")
        args = ["1"] if tag == "PY_ENDPOINTS" else []
        return subprocess.run(
            [sys.executable, *flags, "-", *args],
            input=body,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )

    def _fired(self) -> str:
        return self.fired.read_text() if self.fired.exists() else ""

    def test_rendered_helpers_ignore_shadow_modules(self):
        for tag in ("PY_ENDPOINTS", "PY_PREFLIGHT"):
            with self.subTest(helper=tag):
                self.assertIn("-I", _heredoc(self.text, tag)[0])
                result = self._run(tag, isolated=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self._fired(), "")
        self.assertIn(
            "ENDPOINT_0=(127.0.0.1)", self._run("PY_ENDPOINTS", isolated=True).stdout
        )

    def test_rendered_helpers_ignore_pythonpath(self):
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(self.workspace)}
        for tag in ("PY_ENDPOINTS", "PY_PREFLIGHT"):
            with self.subTest(helper=tag):
                result = self._run(tag, isolated=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self._fired(), "")

    def test_control_without_isolation_loads_the_shadow_modules(self):
        # Proves the fixture would catch a regression: the same helpers without
        # -I import the workspace's modules.
        for tag in ("PY_ENDPOINTS", "PY_PREFLIGHT"):
            with self.subTest(helper=tag):
                self.fired.unlink(missing_ok=True)
                self._run(tag, isolated=False)
                self.assertNotEqual(self._fired(), "")


class CallerEnvironmentTests(unittest.TestCase):
    def test_script_reads_no_caller_environment_or_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            texts = _variants(Path(tmp))
        for variant, text in texts.items():
            with self.subTest(variant=variant):
                self.assertEqual(caller_environment_references(text), set())
                self.assertEqual(relative_paths(text), [])
                self.assertNotRegex(text, r"\$\{?(PROJECT_SANDBOX_\w*|HOME|PWD)\b")

    def test_checks_detect_injected_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = _variants(Path(tmp))["normal"]
        injected = text.replace("cd /\n", 'cd /\necho "$PWD" >> log\n', 1)
        self.assertEqual(caller_environment_references(injected), {"PWD"})
        self.assertEqual(relative_paths(injected), ["log"])
        for line, expected in (
            ('cat "${PROJECT_SANDBOX_X:-}"', {"PROJECT_SANDBOX_X"}),
            ("ls $HOME/.curlrc", {"HOME"}),
        ):
            self.assertEqual(
                caller_environment_references(text + line + "\n"), expected
            )
        for line in ("bash ./helper.sh", ". helper.sh", "jq . < meta.json", "cd tmp"):
            self.assertTrue(relative_paths(text + line + "\n"), line)


if __name__ == "__main__":
    unittest.main()
