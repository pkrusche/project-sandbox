#!/usr/bin/env python3
"""Opt-in end-to-end checks for --runtime colima (add-colima-runtime task 4.1).

Runs real project-sandbox launches (agent: bash, no credentials, no billable
calls) against a running Colima profile, graded from both sides: what the probe
inside the sandbox saw, and what Docker and the sidecar report about the VM.

Checks (default: all):
  launch      one --egress sidecar session: proxy variables name egress:3128,
              the sandbox has one interface and no default route, an allowed
              host reaches the Internet through Squid, an unlisted host is
              denied, and raw TCP/UDP, DNS, IPv6, the VM's bridge gateway, the
              network gateway and the sidecar's uplink address all fail. The
              sidecar publishes nothing, does not forward IP, and is on both
              networks; the denial is logged with the sandbox's address.
  concurrent  two sandboxes at once share one sidecar and create no network.
  recover     `restart` and a sidecar detached behind its back both recover on
              the next launch; `down` keeps both networks and a later launch
              starts the sidecar again.
  firewall    --egress firewall on Colima: the default bridge, an extra domain
              reachable, an unlisted host not, and none of the private pieces.

  hardened    (opt-in, not in the default set) `colima init` a throwaway profile,
              run the launch and recover checks against it, assert the VM cannot
              see ~/.ssh, ~/.aws or the host-only session records, then delete
              the profile and confirm the active Docker context and every other
              profile are unchanged. This is the one check that creates and
              deletes a VM, and only the one it named itself.

All state lives in a temporary XDG_CONFIG_HOME/XDG_STATE_HOME, never the
user's egress.toml. The project directories are created under the repository
(Colima exports the home directory by default) and removed afterwards. The
profile's sidecar name is fixed, so the suite refuses to start while one exists
unless --replace-existing is given. It never creates, starts or stops a Colima
VM and never changes the Docker context.

Needs network access and builds; each launch takes tens of seconds.
Example: uv run python scripts/e2e-colima.py --colima-profile default
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from project_sandbox import colima_network, container_cli, egress_sidecar

ALLOWED_HOST = "api.anthropic.com"  # an agent provider host, always allowed
DENIED_HOST = "example.com"
FIREWALL_DOMAIN = "pypi.org"
CHECKS = ("launch", "concurrent", "recover", "firewall")
OPT_IN_CHECKS = ("hardened",)
HARDENED_PROFILE = "psbx-hardened-e2e"
PROXY = f"http://{colima_network.ALIAS}:{colima_network.PROXY_PORT}"


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


PROBE_SOURCE = r"""
import json, os, socket, struct, urllib.error, urllib.request

cfg = __CONFIG__
out = {}

def tcp(host, port, family=socket.AF_INET, timeout=4):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
        return True
    except OSError:
        return False

def via(url, proxy):
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    try:
        with urllib.request.build_opener(*handlers).open(url, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__

out["env"] = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
out["interfaces"] = sorted(n for n in os.listdir("/sys/class/net") if n != "lo")
routes = [line.split() for line in open("/proc/net/route").read().splitlines()[1:]]
out["default_route"] = any(r[1] == "00000000" for r in routes)
try:
    out["egress_address"] = socket.gethostbyname("egress")
except OSError:
    out["egress_address"] = None

proxy = cfg["proxy"]
out["allowed_https"] = via("https://" + cfg["allowed"] + "/", proxy)
out["denied_http"] = via("http://" + cfg["denied"] + "/", proxy)
out["denied_https"] = via("https://" + cfg["denied"] + "/", proxy)
out["extra_https"] = via("https://" + cfg["extra"] + "/", None if cfg["direct"] else proxy)
out["direct_https_denied"] = via("https://" + cfg["denied"] + "/", None)

out["public_tcp"] = tcp("1.1.1.1", 443)
out["public_ipv6_tcp"] = tcp("2606:4700:4700::1111", 443, socket.AF_INET6)
try:
    query = struct.pack(">HHHHHH", 1, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00\x00\x01\x00\x01"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(4)
        s.sendto(query, ("1.1.1.1", 53))
        s.recvfrom(512)
    out["public_dns_udp"] = True
except OSError:
    out["public_dns_udp"] = False
try:
    socket.gethostbyname(cfg["denied"])
    out["resolves_public_name"] = True
except OSError:
    out["resolves_public_name"] = False
for key in ("vm_gateway", "own_gateway", "uplink"):
    host, port = cfg[key]
    out[key + "_tcp"] = tcp(host, port) if host else None
out["host_docker_internal"] = tcp("host.docker.internal", 80, timeout=2)

open("egress-probe.json", "w").write(json.dumps(out))
"""


def probe_source(config: dict) -> str:
    return PROBE_SOURCE.replace("__CONFIG__", repr(config))


def _is_status(value) -> bool:
    return isinstance(value, int) and 200 <= value < 500


def grade_launch(probe: dict) -> list[Result]:
    """Grade the sidecar-mode probe (pure; tested without a runtime)."""
    env = probe.get("env") or {}
    blocked = (
        "public_tcp",
        "public_ipv6_tcp",
        "public_dns_udp",
        "resolves_public_name",
        "vm_gateway_tcp",
        "own_gateway_tcp",
        "uplink_tcp",
        "host_docker_internal",
    )
    results = [
        Result(
            "launch:proxy-variables",
            len(env) == 4 and all(v == PROXY for v in env.values()),
            str(env),
        ),
        Result(
            "launch:one-interface-no-default-route",
            len(probe.get("interfaces") or []) == 1
            and probe.get("default_route") is False,
            f"{probe.get('interfaces')} default_route={probe.get('default_route')}",
        ),
        Result(
            "launch:allowed-host-through-squid",
            _is_status(probe.get("allowed_https")),
            str(probe.get("allowed_https")),
        ),
        Result(
            "launch:unlisted-http-is-403",
            probe.get("denied_http") == 403,
            str(probe.get("denied_http")),
        ),
        Result(
            "launch:unlisted-https-is-not-served",
            not _is_status(probe.get("denied_https")),
            str(probe.get("denied_https")),
        ),
        Result(
            "launch:proxy-ignored-is-denied",
            not _is_status(probe.get("direct_https_denied")),
            str(probe.get("direct_https_denied")),
        ),
    ]
    for key in blocked:
        value = probe.get(key)
        results.append(Result(f"launch:{key}-fails", value is False, str(value)))
    return results


def grade_firewall(probe: dict) -> list[Result]:
    return [
        Result(
            "firewall:extra-domain-reachable",
            _is_status(probe.get("extra_https")),
            str(probe.get("extra_https")),
        ),
        Result(
            "firewall:unlisted-host-denied",
            not _is_status(probe.get("direct_https_denied")),
            str(probe.get("direct_https_denied")),
        ),
        Result(
            "firewall:no-private-network-pieces",
            probe.get("egress_address") is None
            and not any((probe.get("env") or {}).values()),
            f"egress={probe.get('egress_address')} env={probe.get('env')}",
        ),
        Result(
            "firewall:public-dns-fails",
            probe.get("public_dns_udp") is False,
            str(probe.get("public_dns_udp")),
        ),
    ]


def grade_sidecar(
    container: dict, *, ip_forward: str, networks: list[str]
) -> list[Result]:
    """Grade ``docker inspect`` of the sidecar plus its forwarding sysctl."""
    settings = container.get("NetworkSettings") or {}
    attached = settings.get("Networks") or {}
    sandbox = attached.get(colima_network.SANDBOX_NETWORK) or {}
    return [
        Result(
            "sidecar:publishes-nothing",
            not (settings.get("Ports") or {})
            and not ((container.get("HostConfig") or {}).get("PortBindings") or {}),
            str(settings.get("Ports")),
        ),
        Result("sidecar:does-not-forward-ip", ip_forward.strip() == "0", ip_forward),
        Result(
            "sidecar:on-both-networks",
            colima_network.UPLINK_NETWORK in attached
            and colima_network.ALIAS in (sandbox.get("Aliases") or []),
            str(sorted(attached)),
        ),
        Result(
            "sidecar:exactly-two-project-networks",
            sorted(networks)
            == sorted([colima_network.UPLINK_NETWORK, colima_network.SANDBOX_NETWORK]),
            str(sorted(networks)),
        ),
    ]


def grade_denials(log_text: str, sandbox_subnet_prefix: str) -> list[Result]:
    lines = [line for line in log_text.splitlines() if DENIED_HOST in line]
    return [
        Result(
            "launch:denial-logged-with-cause",
            any("host-not-allowed" in line for line in lines),
            "; ".join(lines[:2]),
        ),
        Result(
            "launch:denial-attributed-to-the-sandbox-address",
            any(sandbox_subnet_prefix in line for line in lines),
            sandbox_subnet_prefix,
        ),
    ]


def grade_hardened(report: dict) -> list[Result]:
    """Grade the hardened-profile facts gathered around ``colima init``/delete.

    ``report`` keys: ``exports`` (paths the VM exported), ``expected_exports``,
    ``hidden`` (paths the VM could not see), ``must_hide``, ``context_before``,
    ``context_after``, ``profiles_before`` / ``profiles_after`` (name -> status of
    every profile other than the throwaway one), ``profile_gone`` and
    ``context_gone``.
    """
    exports = sorted(report.get("exports") or [])
    expected = sorted(report.get("expected_exports") or [])
    must_hide = list(report.get("must_hide") or [])
    leaked = sorted(set(must_hide) - set(report.get("hidden") or []))
    return [
        Result(
            "hardened:exports-are-exactly-the-expected-set",
            bool(exports) and exports == expected,
            f"exports={exports} expected={expected}",
        ),
        Result(
            "hardened:credentials-and-records-not-visible",
            bool(must_hide) and not leaked,
            f"visible in the VM: {leaked}",
        ),
        Result(
            "hardened:active-context-unchanged",
            report.get("context_before") is not None
            and report.get("context_before") == report.get("context_after"),
            f"{report.get('context_before')!r} -> {report.get('context_after')!r}",
        ),
        Result(
            "hardened:other-profiles-unchanged",
            report.get("profiles_before") == report.get("profiles_after"),
            f"{report.get('profiles_before')} -> {report.get('profiles_after')}",
        ),
        Result(
            "hardened:throwaway-profile-removed",
            bool(report.get("profile_gone")) and bool(report.get("context_gone")),
            f"profile_gone={report.get('profile_gone')} "
            f"context_gone={report.get('context_gone')}",
        ),
    ]


class Suite:
    def __init__(self, args: argparse.Namespace, work: Path) -> None:
        self.args = args
        self.work = work
        self.runtime = container_cli.colima_runtime(args.colima_profile)
        self.results: list[Result] = []

    # -- plumbing ---------------------------------------------------------
    def docker(self, *argv: str, check=False) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.runtime.argv(*argv),
            capture_output=True,
            text=True,
            errors="replace",
            check=check,
            timeout=120,
        )

    def egress(self, *command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "project_sandbox.egress_cli",
                *command,
                "--runtime",
                "colima",
                "--colima-profile",
                self.args.colima_profile,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=self.args.timeout,
        )

    def project(self, name: str, config: dict) -> Path:
        path = self.work / name
        path.mkdir()
        (path / "README.md").write_text(f"# colima e2e {name}\n")
        (path / "probe.py").write_text(probe_source(config))
        return path

    def launch(self, project: Path, prompt: str, *extra: str, egress="sidecar"):
        args = self.args
        cpus, memory = self.vm_limits()
        command = [
            sys.executable,
            "-m",
            "project_sandbox",
            "--cpus",
            str(cpus),
            "--memory",
            memory,
            "--runtime",
            "colima",
            "--colima-profile",
            args.colima_profile,
            "--image-tag",
            args.image_tag,
            "--agent",
            "bash",
            "--no-forward-credentials",
            "--egress",
            egress,
            "--prompt-text",
            prompt,
            "--log",
            str(project.parent / f"{project.name}.log"),
            "--timeout",
            str(args.timeout),
            *extra,
            str(project),
            args.base_image,
        ]
        run = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        # Kept next to the session log so a failed check can be diagnosed.
        (project.parent / f"{project.name}.out").write_text(
            f"exit {run.returncode}\n--- stdout\n{run.stdout}\n--- stderr\n{run.stderr}"
        )
        return run

    def vm_limits(self) -> tuple[int, str]:
        """CPUs and memory a sandbox may ask for here (defaults: 4 and 8g).

        Docker refuses more CPUs than the VM has, and a small Colima VM
        (the default profile has 2) is common.
        """
        proc = self.docker("info", "--format", "{{.NCPU}} {{.MemTotal}}")
        try:
            ncpu, total = (int(x) for x in proc.stdout.split())
        except ValueError:
            return 1, "512m"
        mib = max(512, min(8192, total // (1024 * 1024) // 2))
        return max(1, min(4, ncpu)), f"{mib}m"

    def record(self, results: list[Result]) -> None:
        self.results += results
        for r in results:
            print(
                f"{'PASS' if r.ok else 'FAIL'}  {r.name}  {r.detail[:200]}", flush=True
            )

    # -- facts from the VM -------------------------------------------------
    def inspect_sidecar(self) -> dict:
        proc = self.docker("inspect", egress_sidecar.CONTAINER)
        try:
            return json.loads(proc.stdout)[0]
        except (ValueError, IndexError):
            return {}

    def project_networks(self) -> list[str]:
        proc = self.docker("network", "ls", "--format", "{{.Name}}")
        return [n for n in proc.stdout.split() if n.startswith("psbx-")]

    def project_images(self) -> set[str]:
        proc = self.docker("images", "--format", "{{.Repository}}:{{.Tag}}")
        return {i for i in proc.stdout.split() if i.startswith("project-sandbox")}

    def network_id(self, name: str) -> str:
        return self.docker(
            "network", "inspect", name, "--format", "{{.Id}}"
        ).stdout.strip()

    def bridge_gateway(self) -> str | None:
        proc = self.docker(
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        )
        return proc.stdout.strip() or None

    def sandbox_gateway_and_uplink(self) -> tuple[str | None, str | None]:
        gateway = self.docker(
            "network",
            "inspect",
            colima_network.SANDBOX_NETWORK,
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ).stdout.strip()
        uplink = (
            (self.inspect_sidecar().get("NetworkSettings") or {})
            .get("Networks", {})
            .get(colima_network.UPLINK_NETWORK, {})
            .get("IPAddress")
        )
        return gateway or None, uplink or None

    def config(self, *, proxy: str | None, direct=False, extra=FIREWALL_DOMAIN) -> dict:
        own, uplink = self.sandbox_gateway_and_uplink()
        vm = self.bridge_gateway()
        return {
            "proxy": proxy,
            "allowed": ALLOWED_HOST,
            "denied": DENIED_HOST,
            "extra": extra,
            "direct": direct,
            "vm_gateway": (vm, 80),
            "own_gateway": (own, 80),
            "uplink": (uplink, colima_network.PROXY_PORT),
        }

    def read_probe(self, project: Path) -> dict:
        try:
            return json.loads((project / "egress-probe.json").read_text())
        except (OSError, ValueError):
            return {}

    # -- checks -----------------------------------------------------------
    def check_launch(self) -> None:
        # The sidecar does not exist yet: its addresses are read after it starts.
        warm = self.project("warmup", self.config(proxy=PROXY))
        run = self.launch(warm, "true")
        started = run.returncode == 0
        self.record(
            [
                Result(
                    "launch:warmup-starts-the-sidecar",
                    started,
                    (run.stdout + run.stderr)[-300:],
                )
            ]
        )
        if not started:
            return
        project = self.project("launch", self.config(proxy=PROXY))
        run = self.launch(project, "python3 probe.py")
        out = run.stdout + run.stderr
        probe = self.read_probe(project)
        results = [
            Result("launch:exit-code", run.returncode == 0, out[-300:]),
            Result(
                "launch:uses-the-private-endpoint",
                "at egress:3128" in out,
                "no 'at egress:3128' in the launch output",
            ),
        ]
        results += grade_launch(probe)
        sidecar = self.inspect_sidecar()
        ip_forward = self.docker(
            "exec", egress_sidecar.CONTAINER, "cat", "/proc/sys/net/ipv4/ip_forward"
        ).stdout
        results += grade_sidecar(
            sidecar, ip_forward=ip_forward, networks=self.project_networks()
        )
        resolved = probe.get("egress_address")
        address = (
            (sidecar.get("NetworkSettings") or {})
            .get("Networks", {})
            .get(colima_network.SANDBOX_NETWORK, {})
            .get("IPAddress")
        )
        results.append(
            Result(
                "launch:egress-is-pinned-to-the-sidecar",
                bool(resolved) and resolved == address,
                f"{resolved} vs {address}",
            )
        )
        prefix = ".".join((address or "0.0.0.0").split(".")[:3]) + "."
        results += grade_denials(self.egress("log", "--denied").stdout, prefix)
        self.record(results)

    def check_concurrent(self) -> None:
        before = {n: self.network_id(n) for n in self.project_networks()}
        projects = [
            self.project(f"concurrent{i}", self.config(proxy=PROXY)) for i in (1, 2)
        ]
        outputs: dict[int, subprocess.CompletedProcess] = {}

        def go(i: int) -> None:
            outputs[i] = self.launch(projects[i], "sleep 25; python3 probe.py")

        threads = [threading.Thread(target=go, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        overlap = 0
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and any(t.is_alive() for t in threads):
            ps = self.docker(
                "ps",
                "--filter",
                f"network={colima_network.SANDBOX_NETWORK}",
                "--format",
                "{{.Names}}",
            ).stdout.split()
            overlap = max(
                overlap, len([n for n in ps if n != egress_sidecar.CONTAINER])
            )
            if overlap >= 2:
                break
            time.sleep(2)
        for t in threads:
            t.join()
        after = {n: self.network_id(n) for n in self.project_networks()}
        sidecars = self.docker(
            "ps", "-a", "--filter", f"name=^{egress_sidecar.CONTAINER}$", "-q"
        ).stdout.split()
        results = [
            Result(
                "concurrent:overlapping-sandboxes",
                overlap >= 2,
                f"max {overlap} at once",
            ),
            Result("concurrent:one-sidecar", len(sidecars) == 1, str(sidecars)),
            Result(
                "concurrent:no-network-created-or-replaced",
                before == after,
                f"{before} vs {after}",
            ),
        ]
        for i, project in enumerate(projects):
            probe = self.read_probe(project)
            results.append(
                Result(
                    f"concurrent:sandbox-{i + 1}-reaches-the-allowed-host",
                    outputs[i].returncode == 0
                    and _is_status(probe.get("allowed_https")),
                    str(probe.get("allowed_https")),
                )
            )
        self.record(results)

    def _sidecar_launch_ok(self, name: str) -> Result:
        project = self.project(name, self.config(proxy=PROXY))
        run = self.launch(project, "python3 probe.py")
        probe = self.read_probe(project)
        ok = run.returncode == 0 and _is_status(probe.get("allowed_https"))
        return Result(f"recover:{name}", ok, (run.stdout + run.stderr)[-200:])

    def check_recover(self) -> None:
        results = []
        restart = self.egress("restart")
        results.append(
            Result(
                "recover:restart-command",
                restart.returncode == 0,
                restart.stderr[-200:],
            )
        )
        results.append(self._sidecar_launch_ok("launch-after-restart"))
        gone = self.docker(
            "network",
            "disconnect",
            colima_network.SANDBOX_NETWORK,
            egress_sidecar.CONTAINER,
        )
        results.append(
            Result("recover:detach-sidecar", gone.returncode == 0, gone.stderr[-200:])
        )
        status = self.egress("status").stdout
        results.append(
            Result(
                "recover:status-reports-detached",
                "NOT attached" in status,
                status[-200:],
            )
        )
        results.append(self._sidecar_launch_ok("launch-after-detach"))
        attached = colima_network.attached_to_both(self.inspect_sidecar())
        results.append(Result("recover:sidecar-reattached", attached, ""))
        down = self.egress("down")
        results.append(
            Result("recover:down-command", down.returncode == 0, down.stderr[-200:])
        )
        left = self.docker("inspect", egress_sidecar.CONTAINER)
        results.append(
            Result("recover:down-removes-the-sidecar", left.returncode != 0, "")
        )
        results.append(
            Result(
                "recover:down-keeps-both-networks",
                sorted(self.project_networks())
                == sorted(
                    [colima_network.UPLINK_NETWORK, colima_network.SANDBOX_NETWORK]
                ),
                str(self.project_networks()),
            )
        )
        results.append(self._sidecar_launch_ok("launch-after-down"))
        self.record(results)

    def check_firewall(self) -> None:
        project = self.project("firewall", self.config(proxy=None, direct=True))
        run = self.launch(
            project,
            "python3 probe.py",
            "--extra-domain",
            FIREWALL_DOMAIN,
            egress="firewall",
        )
        probe = self.read_probe(project)
        results = [
            Result(
                "firewall:exit-code",
                run.returncode == 0,
                (run.stdout + run.stderr)[-300:],
            )
        ]
        results += grade_firewall(probe)
        self.record(results)


def _out(argv: list[str]) -> str:
    proc = subprocess.run(
        argv, capture_output=True, text=True, errors="replace", check=False, timeout=900
    )
    return proc.stdout.strip()


def _profile_states() -> dict[str, str]:
    states = {}
    for line in _out(["colima", "list", "--json"]).splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("name") != HARDENED_PROFILE:
            states[item["name"]] = item.get("status", "")
    return states


def run_hardened(args: argparse.Namespace) -> list[Result]:
    """init a throwaway profile, run launch + recover against it, grade, delete."""
    from project_sandbox import colima_mounts, colima_profile

    name = HARDENED_PROFILE
    # Before the child run overrides XDG_STATE_HOME process-wide.
    record = colima_profile.record_path(name)
    if name in {
        n for n in _out(["colima", "list", "--json"]).replace('"', " ").split()
    }:
        return [Result("hardened:profile-name-free", False, f"{name} already exists")]
    work = Path(tempfile.mkdtemp(prefix=".e2e-colima-hardened-", dir=ROOT)).resolve()
    home = Path.home()
    before = {
        "context": _out(["docker", "context", "show"]),
        "profiles": _profile_states(),
    }
    report: dict = {
        "context_before": before["context"],
        "profiles_before": before["profiles"],
        "must_hide": [
            str(p)
            for p in (
                home / ".ssh",
                home / ".aws",
                colima_profile.state_root() / "sessions",
            )
            if p.exists()
        ],
    }
    results: list[Result] = []
    created = False
    try:
        init = subprocess.run(
            [
                sys.executable,
                "-m",
                "project_sandbox",
                "colima",
                "init",
                "--workspace-root",
                str(work),
                "--colima-profile",
                name,
                "--cpu",
                "4",
                "--memory",
                "6",
                "--disk",
                "30",
                "--yes",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=args.timeout,
        )
        created = True
        results.append(
            Result("hardened:init", init.returncode == 0, init.stderr[-300:])
        )
        if init.returncode != 0:
            return results
        runtime = container_cli.colima_runtime(name)
        exports = colima_mounts.exported_mounts(runtime) or []
        report["exports"] = [e.path for e in exports]
        report["expected_exports"] = [
            str(work),
            *map(str, colima_mounts.required_state_dirs()),
        ]
        report["hidden"] = [
            str(p)
            for p in (
                colima_mounts.missing_in_vm(runtime, map(Path, report["must_hide"]))
                or []
            )
        ]
        child = argparse.Namespace(**vars(args))
        child.colima_profile = name
        child.checks = "launch,recover"
        child.keep = False
        # The child runs in a work directory under the repository, which the new
        # profile does not export; give it the exported one instead.
        os.environ["PSBX_E2E_WORKDIR"] = str(work)
        code = main_checks(child)
        results.append(Result("hardened:launch-and-recover", code == 0, f"exit {code}"))
    finally:
        os.environ.pop("PSBX_E2E_WORKDIR", None)
        if created and not args.keep:
            subprocess.run(
                ["colima", "delete", "--profile", name, "--force"],
                capture_output=True,
                check=False,
                timeout=300,
            )
        report["context_after"] = _out(["docker", "context", "show"])
        report["profiles_after"] = _profile_states()
        report["profile_gone"] = name not in _out(["colima", "list", "--json"])
        report["context_gone"] = (
            f"colima-{name}"
            not in _out(["docker", "context", "ls", "--format", "{{.Name}}"]).split()
        )
        shutil.rmtree(work, ignore_errors=True)
        record.unlink(missing_ok=True)
    if args.keep:
        report["profile_gone"] = report["context_gone"] = True
    return [*results, *grade_hardened(report)]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--colima-profile", default=container_cli.COLIMA_DEFAULT_PROFILE)
    p.add_argument(
        "--checks",
        default=",".join(CHECKS),
        help=f"comma list of: {', '.join(CHECKS)}; opt-in: {', '.join(OPT_IN_CHECKS)}",
    )
    p.add_argument("--base-image", default="python:3.12-slim")
    p.add_argument("--image-tag", default="project-sandbox-e2e-colima:latest")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument(
        "--keep", action="store_true", help="keep the work directory and VM resources"
    )
    p.add_argument(
        "--check-available",
        action="store_true",
        help="exit 0 if the profile is running, 3 (with the reason) if not; runs nothing",
    )
    p.add_argument(
        "--replace-existing",
        action="store_true",
        help="allow replacing the profile's existing egress sidecar",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    checks = tuple(c.strip() for c in args.checks.split(",") if c.strip())
    unknown = [c for c in checks if c not in CHECKS + OPT_IN_CHECKS]
    if unknown:
        print(f"unknown checks: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if "hardened" in checks:
        if len(checks) > 1:
            print(
                "hardened runs on its own throwaway profile; pass it alone",
                file=sys.stderr,
            )
            return 2
        results = run_hardened(args)
        for r in results:
            print(
                f"{'ok  ' if r.ok else 'FAIL'} {r.name} {'' if r.ok else r.detail[:300]}"
            )
        failed = [r for r in results if not r.ok]
        print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
        return 1 if failed else 0
    return main_checks(args)


def main_checks(args: argparse.Namespace) -> int:
    checks = tuple(c.strip() for c in args.checks.split(",") if c.strip())
    runtime = container_cli.colima_runtime(args.colima_profile)
    try:
        container_cli.verify_colima_profile(runtime)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 3 if args.check_available else 2
    if args.check_available:
        return 0
    existing = subprocess.run(
        runtime.argv("inspect", egress_sidecar.CONTAINER),
        capture_output=True,
        text=True,
        check=False,
    )
    if existing.returncode == 0 and not args.replace_existing:
        print(
            f"{egress_sidecar.CONTAINER} already exists in Colima profile "
            f"{args.colima_profile!r}; the suite would replace and stop it. Pass "
            "--replace-existing to allow that.",
            file=sys.stderr,
        )
        return 2

    work = Path(
        tempfile.mkdtemp(
            prefix=".e2e-colima-", dir=os.environ.get("PSBX_E2E_WORKDIR") or ROOT
        )
    )
    # Under the repository, not $TMPDIR: the VM exports the home directory only.
    config_home = work / "xdg"
    config_home.mkdir()
    os.environ["XDG_CONFIG_HOME"] = str(config_home / "config")
    os.environ["XDG_STATE_HOME"] = str(config_home / "state")
    suite = Suite(args, work)
    # Remove only what this run created, never what was there before.
    networks_before = set(suite.project_networks())
    images_before = suite.project_images()
    try:
        for name in checks:
            print(f"== {name}", flush=True)
            getattr(suite, f"check_{name}")()
    finally:
        if not args.keep:
            suite.egress("down")
            created = sorted(set(suite.project_networks()) - networks_before)
            if created:
                suite.docker("network", "rm", *created)
            for image in sorted(suite.project_images() - images_before):
                suite.docker("rmi", image)
            shutil.rmtree(work, ignore_errors=True)
    failed = [r for r in suite.results if not r.ok]
    print(f"\n{len(suite.results) - len(failed)} passed, {len(failed)} failed")
    for r in failed:
        print(f"FAILED {r.name}: {r.detail[:300]}")
    return 1 if failed or not suite.results else 0


if __name__ == "__main__":
    sys.exit(main())
