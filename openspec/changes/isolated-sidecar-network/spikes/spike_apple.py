#!/usr/bin/env python3
"""Apple container spikes for isolated-sidecar-network: S3–S6 (tasks 0.3–0.6).

  S3  Internal network. `container network create --internal` as a normal user
      (with labels if supported). From a container on it, probe public IPv4/IPv6
      TCP and UDP, DNS for a public name, the host's address on that network,
      203.0.113.113, 192.168.64.1 and 169.254.169.254.
  S4  Topology. One gateway on a NAT network and the internal network: does it
      reach the Internet, can a sandbox on the internal network reach it, can two
      sandboxes reach each other, is there --add-host or a network alias, does the
      gateway's internal address survive stop/start and recreation, and does
      ip_forward=0 stop a NET_ADMIN sandbox that routes through the gateway.
  S5  Host path and streaming. From the gateway, reach the Mac's loopback through
      host.docker.internal (the administrator-managed alias, see
      docs/apple-container-dns.md), then run Squid with D3's accel ports and time
      NDJSON and SSE streams from a sandbox, plus D3's separation checks.
  S6  Attribution. Two sandboxes use the forward proxy; record Squid's client=.

Everything is named psbx-spike-* (networks psbx-spike-uplink and
psbx-spike-sandbox), so the project's sidecar and networks are never touched.
It never runs sudo and never changes `container system dns`. Spike containers
and networks are removed at the end; the image is kept for re-runs (--cleanup
removes it too).

The Mac test services listen on 127.0.0.1:<loopback-port> and
0.0.0.0:<any-port> while the script runs and serve only fixed test data. macOS
may ask whether Python may accept incoming connections: allow it, or the
0.0.0.0 probes measure the macOS firewall instead of vmnet.

Needs: macOS 26 or later, the latest `container` with its system started, and
network access for the image build. Run as your normal user. Takes a few
minutes (every probe is a container VM).

  uv run python openspec/changes/isolated-sidecar-network/spikes/spike_apple.py
  ... --only S3
  ... --ollama-model llama3.2   # also stream from the Mac's real Ollama
  ... --cleanup                 # remove psbx-spike-* leftovers and the image
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import time
from pathlib import Path

import spikelib as lib
from spikelib import (
    GATEWAY,
    PEER,
    PEER_PORT,
    SANDBOX,
    UPLINK,
    Engine,
    Report,
    tcp,
    udp_dns,
)

ALIAS_ADDRESS = "203.0.113.113"
DEFAULT_GATEWAY = "192.168.64.1"
GATEWAY_LISTENER_PORT = 8082
LABELS = [
    "--label",
    "project-sandbox.owner=project-sandbox",
    "--label",
    lib.SPIKE_LABEL,
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only", default="S3,S4,S5,S6", help="comma-separated subset of S3,S4,S5,S6"
    )
    parser.add_argument("--loopback-port", type=int, default=18434)
    parser.add_argument("--any-port", type=int, default=18435)
    parser.add_argument(
        "--ollama-model", help="also stream from the Mac's Ollama on 11434"
    )
    parser.add_argument(
        "--squid-directive",
        action="append",
        default=[],
        help="extra squid.conf line, e.g. 'read_ahead_gap 1 KB' (repeatable)",
    )
    parser.add_argument(
        "--force-os",
        action="store_true",
        help="run on macOS older than 26 anyway (results will not count)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the spike containers and networks for inspection",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="remove psbx-spike-* containers, networks and image, then exit",
    )
    parser.add_argument("--out", type=Path, default=lib.HERE / "results")
    return parser.parse_args()


class AppleSpike:
    def __init__(self, args: argparse.Namespace, report: Report) -> None:
        self.args = args
        self.report = report
        self.engine = Engine(["container"], apple=True, report=report)
        self.hosts = lib.HostServices(args.loopback_port, args.any_port)
        self.help: dict[str, str] = {}
        self.labels_supported = False
        self.internal: dict = {}
        self.gateway_ip = ""
        self.gateway_up = False
        self.squid_up = False
        self.sysctl_flag: list[str] = []

    # Setup -------------------------------------------------------------------
    def environment(self) -> None:
        r, e = self.report, self.engine
        r.h("Environment")
        if os.geteuid() == 0:
            raise lib.SpikeError(
                "run as your normal user: S3 checks networks need no sudo"
            )
        rows = []
        for label, argv in (
            ("macOS", ["sw_vers", "-productVersion"]),
            ("macOS build", ["sw_vers", "-buildVersion"]),
            ("container", ["container", "--version"]),
            ("container system status", ["container", "system", "status"]),
            ("user", ["id", "-un"]),
        ):
            proc = lib.sh(argv, r, timeout=60)
            text = (proc.stdout or proc.stderr).strip()
            r.data[label] = text
            rows.append((label, " ".join(text.split())[:200] or "?"))
        r.table(("Item", "Value"), rows)
        major = int((r.data["macOS"].split(".") or ["0"])[0] or 0)
        if major < 26 and not self.args.force_os:
            raise lib.SpikeError(
                f"macOS {r.data['macOS']}: S3–S5 need macOS 26 or later (--force-os to run anyway)"
            )
        if lib.sh(["container", "system", "status"], r, timeout=60).returncode != 0:
            raise lib.SpikeError(
                "the container system is not running: `container system start`"
            )
        for key, argv in (
            ("network create", ["network", "create", "--help"]),
            ("run", ["run", "--help"]),
            ("network", ["network", "--help"]),
        ):
            proc = e.run(*argv, timeout=60)
            self.help[key] = proc.stdout + proc.stderr
        r.data["help"] = self.help
        flags = {
            "network create --internal": "--internal" in self.help["network create"],
            "network create --label": "--label" in self.help["network create"],
            "run --add-host": "--add-host" in self.help["run"],
            "run --network-alias / --alias": any(
                f in self.help["run"] for f in ("--network-alias", "--alias")
            ),
            "run --sysctl": "--sysctl" in self.help["run"],
            "run --cap-add": "--cap-add" in self.help["run"],
            "network connect": "connect" in self.help["network"],
        }
        r.data["flags"] = flags
        r.p("CLI support, from `--help`:")
        r.table(
            ("Flag", "Listed"), [(k, "yes" if v else "no") for k, v in flags.items()]
        )
        r.code(self.help["network create"][-3000:])

    def setup(self) -> None:
        r, e = self.report, self.engine
        left = e.leftovers()
        if left:
            raise lib.SpikeError(
                "leftovers from an earlier run: "
                + ", ".join(left)
                + "; re-run with --cleanup first"
            )
        r.h("Setup")
        r.p("Building the spike image (Alpine + squid, python3, iproute2, curl).")
        e.build()
        self.hosts.start()
        r.p(
            f"Mac test services: 127.0.0.1:{self.args.loopback_port} (loopback), "
            f"0.0.0.0:{self.args.any_port} (any)."
        )

    def create_networks(self) -> list[str]:
        """Create both networks; return blocking problems."""
        r, e = self.report, self.engine
        problems = []
        proc = e.run("network", "create", "--internal", *LABELS, SANDBOX, timeout=120)
        self.labels_supported = proc.returncode == 0
        if proc.returncode != 0:
            r.p(
                f"`network create --internal` with labels failed: {proc.stderr.strip()[:300]}"
            )
            proc = e.run("network", "create", "--internal", SANDBOX, timeout=120)
            if proc.returncode == 0:
                r.p(
                    "Created without labels: ownership validation needs another marker."
                )
        if proc.returncode != 0:
            problems.append(
                f"`container network create --internal` failed as a normal user: "
                f"{proc.stderr.strip()[:300]}"
            )
            return problems
        inspect = e.run("network", "inspect", SANDBOX, timeout=60)
        r.data["sandbox_network_inspect"] = inspect.stdout
        r.p("`container network inspect " + SANDBOX + "`:")
        r.code(inspect.stdout[-3000:], "json")
        if (
            self.labels_supported
            and lib.SPIKE_LABEL.split("=")[1] not in inspect.stdout
        ):
            r.p("Labels were accepted but do not show in `network inspect`.")
            self.labels_supported = False
        uplink = e.run(
            "network",
            "create",
            *(LABELS if self.labels_supported else []),
            UPLINK,
            timeout=120,
        )
        if uplink.returncode != 0:
            problems.append(
                f"creating the NAT uplink network failed: {uplink.stderr.strip()[:300]}"
            )
        listing = e.run("network", "list", timeout=60)
        r.code("$ container network list\n" + listing.stdout)
        return problems

    # S3 ----------------------------------------------------------------------
    def s3(self) -> bool:
        r, e = self.report, self.engine
        r.h("S3. Apple internal network (task 0.3)")
        problems = self.create_networks()
        if problems:
            r.verdict("S3", "BLOCKED", problems)
            return False
        self.internal = lib.learn_subnet(e, SANDBOX, r)
        host_net = self.internal["default_gateway"] or self.internal["first_host"]
        r.p(
            f"Host's address on the internal network (default route, else first host): {host_net}"
        )
        h, loop, anyp = host_net, self.args.loopback_port, self.args.any_port
        ops = [
            tcp("1.1.1.1", 443, "public 1.1.1.1:443"),
            tcp("8.8.8.8", 53, "public 8.8.8.8:53/tcp"),
            udp_dns("1.1.1.1", "DNS to 1.1.1.1"),
            tcp("2606:4700:4700::1111", 443, "IPv6 [2606:4700:4700::1111]:443"),
            udp_dns("2606:4700:4700::1111", "IPv6 DNS"),
            {
                "kind": "resolve",
                "name": "example.com",
                "label": "system resolver: example.com",
            },
            udp_dns("@resolv", "DNS to the configured resolver"),
            udp_dns(h, f"DNS to the host on the internal network {h}"),
            lib.tcp_dns(h, f"DNS over TCP to the host {h}: example.com"),
            lib.tcp_dns(
                h,
                f"DNS over TCP to the host {h}: tunnel-style name",
                name="psbx-spike-tunnel-check.example.com",
            ),
            lib.tcp_dns("@resolv", "DNS over TCP to the configured resolver"),
            *[
                tcp(h, p, f"host {h}:{p} ({what})")
                for p, what in (
                    (22, "ssh"),
                    (53, "dns"),
                    (80, "http"),
                    (445, "smb"),
                    (5000, "airplay/control center"),
                    (5900, "screen sharing"),
                    (7000, "airplay"),
                    (loop, "test service on 127.0.0.1"),
                    (anyp, "test service on 0.0.0.0"),
                    (lib.CLOSED_PORT, "closed port"),
                )
            ],
            *[
                tcp(ALIAS_ADDRESS, p, f"localhost alias {ALIAS_ADDRESS}:{p}")
                for p in (80, 11434, loop, anyp)
            ],
            *[
                tcp(
                    DEFAULT_GATEWAY, p, f"default network gateway {DEFAULT_GATEWAY}:{p}"
                )
                for p in (53, 80, loop, anyp)
            ],
            udp_dns(DEFAULT_GATEWAY, f"DNS to {DEFAULT_GATEWAY}"),
            tcp("169.254.169.254", 80, "metadata 169.254.169.254:80"),
        ]
        results = e.probe(
            [SANDBOX], [{"kind": "read", "path": "/etc/resolv.conf"}, *ops]
        )
        r.data["s3"] = results
        r.code(
            "$ cat /etc/resolv.conf\n"
            + results[0].get("text", lib.describe(results[0]))
        )
        failures = lib.containment_table(r, results)
        hits = [
            req
            for req in self.hosts.requests
            if ipaddress.ip_address(req["client"]) in self.internal["subnet"]
        ]
        if hits:
            r.p("Requests the Mac test services received from the internal network:")
            r.table(
                ("Service", "Client", "Target"),
                [(q["server"], q["client"], q["target"]) for q in hits],
            )
        notes = [
            (
                f"{r.data.get('container', '?')}: check its release notes for the first "
                "release with `network create --internal` (D8's minimum)."
            )
        ]
        if not self.labels_supported:
            notes.append(
                "labels are not supported on networks (WORKAROUND for ownership checks)"
            )
        if failures:
            r.verdict("S3", "BLOCKED", failures + notes)
        else:
            r.verdict("S3", "PASS" if self.labels_supported else "WORKAROUND", notes)
        return not failures

    # S4 ----------------------------------------------------------------------
    def start_gateway(self) -> tuple[bool, str]:
        e = self.engine
        extra = ["--sysctl", "net.ipv4.ip_forward=0"]
        proc = e.start(GATEWAY, [UPLINK, SANDBOX], extra=extra, check=False)
        if proc.returncode == 0:
            self.sysctl_flag = extra
            return True, "started with --sysctl net.ipv4.ip_forward=0"
        first = proc.stderr.strip()[:300]
        e.rm(GATEWAY)
        proc = e.start(GATEWAY, [UPLINK, SANDBOX], check=False)
        if proc.returncode == 0:
            return True, f"--sysctl refused ({first}); started without it"
        return False, f"two --network flags refused: {proc.stderr.strip()[:300]}"

    def gateway_address(self) -> str:
        addrs = self.engine.exec_probe(GATEWAY, [{"kind": "addrs"}])[0]
        self.report.data.setdefault("gateway_addrs", []).append(addrs)
        return lib.address_in(addrs, self.internal["subnet"]) or ""

    def ensure_ip_forward_off(self) -> str:
        e = self.engine
        read = {"kind": "read", "path": "/proc/sys/net/ipv4/ip_forward"}
        value = e.exec_probe(GATEWAY, [read])[0].get("text", "?").strip()
        if value == "1":
            e.exec(GATEWAY, "sysctl", "-w", "net.ipv4.ip_forward=0")
            after = e.exec_probe(GATEWAY, [read])[0].get("text", "?").strip()
            return f"1 at start; after `sysctl -w` in the gateway: {after}"
        return value

    def s4(self) -> bool:
        r, e = self.report, self.engine
        r.h("S4. Apple topology (task 0.4)")
        if not self.internal:
            self.internal = lib.learn_subnet(e, SANDBOX, r)
        blocked, workaround, rows = [], [], []
        ok, how = self.start_gateway()
        rows.append(("gateway on NAT + internal networks", how))
        if not ok:
            r.table(("Check", "Observed"), rows)
            r.verdict("S4", "BLOCKED", [how])
            return False
        self.gateway_up = True
        self.gateway_ip = self.gateway_address()
        gw = r.data["gateway_addrs"][-1]
        r.code(
            "$ ip -o addr (gateway)\n"
            + "\n".join(f"{a['ifname']} {a['cidr']}" for a in gw["ipv4"])
            + "\n$ ip route (gateway)\n"
            + gw.get("routes4", "")
        )
        if not self.gateway_ip:
            r.verdict(
                "S4", "BLOCKED", ["the gateway has no address on the internal network"]
            )
            return False
        rows.append(("gateway internal address", self.gateway_ip))

        # The NAT path can take a few seconds after start (first run: timeouts
        # right after start, while the same gateway later served S5 and S6).
        for attempt in range(6):
            out = e.exec_probe(
                GATEWAY,
                [
                    tcp("1.1.1.1", 443, "gateway → 1.1.1.1:443"),
                    {
                        "kind": "http",
                        "url": "http://example.com/",
                        "label": "gateway → example.com",
                    },
                ],
            )
            if all(res.get("outcome") in ("connected", "http") for res in out):
                break
            time.sleep(2)
        rows.append(("gateway Internet attempts", str(attempt + 1)))
        for res in out:
            reached = res.get("outcome") in ("connected", "http")
            rows.append((res["op"]["label"], lib.describe(res)))
            if not reached:
                blocked.append(f"{res['op']['label']}: {lib.describe(res)}")

        e.exec(
            GATEWAY,
            "sh",
            "-c",
            # setsid: `container exec` ends the session's process group.
            f"setsid python3 -m http.server {GATEWAY_LISTENER_PORT} "
            f"--bind {self.gateway_ip} >/dev/null 2>&1 &",
        )
        e.start(
            PEER, [SANDBOX], command=["python3", "-m", "http.server", str(PEER_PORT)]
        )
        peer = e.exec_probe(PEER, [{"kind": "addrs"}])[0]
        peer_ip = lib.address_in(peer, self.internal["subnet"]) or ""
        add_host = ["--add-host", f"egress:{self.gateway_ip}"]
        probe = e.probe(
            [SANDBOX],
            [
                {"kind": "sleep", "seconds": 1},
                tcp(
                    self.gateway_ip,
                    GATEWAY_LISTENER_PORT,
                    "sandbox → gateway (internal address)",
                ),
                tcp(peer_ip, PEER_PORT, "sandbox → second sandbox"),
                {"kind": "resolve", "name": "egress", "label": "--add-host egress"},
                {"kind": "resolve", "name": GATEWAY, "label": "gateway container name"},
                {"kind": "read", "path": "/etc/hosts"},
            ],
            extra=add_host,
        )
        add_host_ok = not probe[0]["outcome"].startswith("error")
        if not add_host_ok:
            probe = e.probe(
                [SANDBOX],
                [
                    {"kind": "sleep", "seconds": 1},
                    tcp(
                        self.gateway_ip,
                        GATEWAY_LISTENER_PORT,
                        "sandbox → gateway (internal address)",
                    ),
                    tcp(peer_ip, PEER_PORT, "sandbox → second sandbox"),
                    {
                        "kind": "resolve",
                        "name": GATEWAY,
                        "label": "gateway container name",
                    },
                    {"kind": "read", "path": "/etc/hosts"},
                ],
            )
        r.data["s4_probe"] = probe
        by = {res["op"].get("label"): res for res in probe}
        for label in (
            "sandbox → gateway (internal address)",
            "sandbox → second sandbox",
        ):
            res = by[label]
            rows.append((label, lib.describe(res)))
            if res.get("outcome") != "connected":
                blocked.append(f"{label}: {lib.describe(res)}")
        pinned = by.get("--add-host egress", {})
        e.add_host = add_host_ok and self.gateway_ip in pinned.get("addresses", [])
        if e.add_host:
            rows.append(("`run --add-host`", f"works: egress → {self.gateway_ip}"))
        else:
            rows.append(
                ("`run --add-host`", "not supported: pin from the entrypoint (D7)")
            )
            workaround.append(
                "no --add-host: the entrypoint pins egress in /etc/hosts (D7)"
            )
        rows.append(
            ("container name resolution", lib.describe(by["gateway container name"]))
        )
        hosts_file = next((res for res in probe if res["op"]["kind"] == "read"), {})
        r.code(
            "$ cat /etc/hosts (sandbox)\n"
            + hosts_file.get("text", lib.describe(hosts_file))
        )

        before = self.gateway_ip
        e.run("stop", GATEWAY, timeout=120)
        e.run("start", GATEWAY, timeout=120, check=True)
        after_restart = self.gateway_address()
        e.rm(GATEWAY)
        ok, _ = self.start_gateway()
        if not ok:
            blocked.append("could not recreate the gateway")
        after_recreate = self.gateway_address() if ok else ""
        self.gateway_ip = after_recreate
        rows += [
            ("internal address after stop/start", f"{after_restart} (was {before})"),
            ("internal address after recreation", f"{after_recreate} (was {before})"),
        ]

        forward = self.ensure_ip_forward_off()
        rows.append(("gateway net.ipv4.ip_forward", forward))
        if not forward.endswith("0"):
            blocked.append(f"ip_forward cannot be set to 0 in the gateway ({forward})")
        r.table(("Check", "Observed"), rows)

        r.h("NET_ADMIN sandbox routing through the gateway", 3)
        route = {
            "kind": "sh",
            "cmd": [
                "sh",
                "-c",
                f"ip route replace default via {self.gateway_ip} && ip route",
            ],
        }
        results = e.probe(
            [SANDBOX],
            [
                route,
                tcp("1.1.1.1", 443, "public 1.1.1.1:443 via gateway"),
                udp_dns("1.1.1.1", "DNS to 1.1.1.1 via gateway"),
                tcp(
                    ALIAS_ADDRESS,
                    self.args.loopback_port,
                    f"{ALIAS_ADDRESS}:{self.args.loopback_port} via gateway",
                ),
                tcp(DEFAULT_GATEWAY, 53, f"{DEFAULT_GATEWAY}:53 via gateway"),
            ],
            extra=["--cap-add", "NET_ADMIN"],
        )
        r.data["s4_bypass"] = results
        r.code(results[0].get("out", lib.describe(results[0])))
        if results[0].get("rc") != 0:
            blocked.append(
                "could not set the default route in a NET_ADMIN sandbox; bypass untested"
            )
        blocked += lib.containment_table(r, results)

        verdict = "BLOCKED" if blocked else ("WORKAROUND" if workaround else "PASS")
        r.verdict("S4", verdict, blocked + workaround)
        return not blocked

    # S5 ----------------------------------------------------------------------
    def s5(self) -> None:
        r, e = self.report, self.engine
        r.h("S5. Apple host path and streaming (task 0.5)")
        loop, anyp = self.args.loopback_port, self.args.any_port
        results = e.exec_probe(
            GATEWAY,
            [
                {"kind": "resolve", "name": "host.docker.internal", "label": "resolve"},
                {
                    "kind": "http",
                    "url": f"http://host.docker.internal:{loop}/",
                    "label": "127.0.0.1 service",
                },
                {
                    "kind": "http",
                    "url": f"http://host.docker.internal:{anyp}/",
                    "label": "0.0.0.0 service",
                },
            ],
        )
        r.data["s5_gateway"] = results
        r.table(
            ("From the gateway", "Observed"),
            [(res["op"]["label"], lib.describe(res)) for res in results],
        )
        addresses = results[0].get("addresses") or []
        if not addresses or results[1].get("status") != 200:
            r.verdict(
                "S5",
                "BLOCKED (setup)",
                [
                    (
                        "the gateway cannot reach the Mac through host.docker.internal; set up or repair "
                        "the alias (docs/apple-container-dns.md) and re-run with --only S5"
                    )
                ],
            )
            return
        host_address = addresses[0]
        services = {"loopback": loop, "any": anyp}
        if lib.host_ollama_ok(self.args.ollama_model):
            services["ollama"] = 11434
        elif self.args.ollama_model:
            r.p(
                "Ollama did not answer on the Mac's 127.0.0.1:11434; skipping the real-Ollama stream."
            )
        self.start_squid(host_address, services)
        failures = lib.service_checks(
            r,
            e,
            networks=[SANDBOX],
            gateway_ip=self.gateway_ip,
            services=services,
            host_label="host.docker.internal",
            host_address=host_address,
            host_name="host.docker.internal",
            ollama_model=self.args.ollama_model,
            hosts=self.hosts,
        )
        r.verdict(
            "S5",
            "PASS" if not failures else "FAIL (review: WORKAROUND or BLOCKED)",
            failures,
        )

    def start_squid(self, host_address: str, services: dict[str, int]) -> None:
        r, e = self.report, self.engine
        conf = lib.squid_conf(
            self.gateway_ip, host_address, services, self.args.squid_directive
        )
        r.code(conf, "squid")
        proc = e.write_and_start_squid(GATEWAY, conf)
        r.code(
            f"$ squid -k parse && squid  (rc {proc.returncode})\n{proc.stderr[-2000:]}"
        )
        if proc.returncode != 0:
            raise lib.SpikeError("Squid did not accept the spike config (see report)")
        lib.wait_for_squid(e, GATEWAY, self.gateway_ip, r)
        self.squid_up = True

    def s6(self) -> None:
        r = self.report
        r.h("S6. Attribution (task 0.6)")
        if not self.squid_up:
            self.start_squid(ALIAS_ADDRESS, {})
        lib.attribution_check(
            r,
            self.engine,
            networks=[SANDBOX],
            gateway=GATEWAY,
            gateway_ip=self.gateway_ip,
        )


def main() -> int:
    args = parse_args()
    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    unknown = only - {"S3", "S4", "S5", "S6"}
    if unknown:
        raise SystemExit(f"unknown spikes: {', '.join(sorted(unknown))}")
    report = Report("apple-container", args.out)
    spike = AppleSpike(args, report)
    if args.cleanup:
        spike.engine.cleanup(image=True)
        print("removed psbx-spike-* containers, networks and image (where present)")
        return 0
    code = 0
    try:
        spike.environment()
        spike.setup()
        # Later spikes need the networks (S3) and the gateway (S4).
        if "S3" in only or only & {"S4", "S5", "S6"}:
            if "S3" in only:
                # A containment failure still leaves a usable network; S4–S6
                # inform what the topology is worth with the firewall on.
                spike.s3()
                if not spike.internal and only - {"S3"}:
                    raise lib.SpikeError(
                        "S3 created no internal network; S4–S6 need it"
                    )
            else:
                problems = spike.create_networks()
                if problems:
                    raise lib.SpikeError("; ".join(problems))
        if only & {"S4", "S5", "S6"} and not spike.s4() and not spike.gateway_ip:
            raise lib.SpikeError("S4 could not set up the gateway; S5 and S6 need it")
        if "S5" in only:
            spike.s5()
        if "S6" in only:
            spike.s6()
    except lib.SpikeError as exc:
        report.p(f"**Stopped:** {exc}")
        print(f"\nERROR: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        report.p("**Interrupted.**")
        code = 130
    finally:
        spike.hosts.stop()
        if args.keep:
            report.p(
                "Kept the spike containers and networks (--keep); remove them with --cleanup."
            )
        else:
            spike.engine.cleanup()
        report.write()
        print(f"\nReport: {report.md_path}\nRaw:    {report.json_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
