#!/usr/bin/env python3
"""Colima spikes for isolated-sidecar-network: S1, S2 and S6 (tasks 0.1, 0.2, 0.6).

  S1  Host path and streaming. From a container on the uplink network, reach a
      Mac service bound to 127.0.0.1 and one bound to 0.0.0.0 through
      host.lima.internal (resolved address recorded, VM type recorded). Then run
      Squid with D3's accel ports and originserver peers on the gateway, and from
      a sandbox on the internal network time an Ollama-style NDJSON stream and a
      1-event-per-second SSE stream, plus D3's separation checks (pivot through a
      service port, host address through the forward proxy).
  S2  Containment without the firewall. From a container on the internal
      network with no added capabilities and no firewall, probe 192.168.5.2
      (22, 80, 11434, both test services, a closed port) and repeat Spike B's
      matrix. Then with NET_ADMIN, route via the gateway and probe again.
  S6  Attribution. Two sandboxes use the forward proxy; record Squid's client=.

The spike creates psbx-spike-uplink (bridge) and psbx-spike-sandbox (bridge,
--internal) with the same argv as colima_network.create_argv, so the project's
real networks and sidecar are never touched. Everything named psbx-spike-* is
removed at the end (the image is kept for re-runs; --cleanup removes it too).

The Mac test services listen on 127.0.0.1:<loopback-port> and
0.0.0.0:<any-port> for the duration of the run and serve only fixed test data.
macOS may ask whether Python may accept incoming connections: allow it, or the
0.0.0.0 probes measure the macOS firewall instead of the VM.

Needs: a running Colima profile (VZ driver for S1), its Docker context, network
access for the image build and the forward-proxy checks. Takes a few minutes.

  uv run python openspec/changes/isolated-sidecar-network/spikes/spike_colima.py
  ... --colima-profile work --only S2
  ... --ollama-model llama3.2   # also stream from the Mac's real Ollama
  ... --cleanup                 # remove psbx-spike-* leftovers and the image
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path

import spikelib as lib
from spikelib import (
    GATEWAY,
    PEER_PORT,
    PROXY_PORT,
    SANDBOX,
    UPLINK,
    Engine,
    Report,
    tcp,
    udp_dns,
)

from project_sandbox import colima_network, container_cli

DEFAULT_HOST = "192.168.5.2"
GATEWAY_LISTENER_PORT = 8082


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--colima-profile", default="default")
    parser.add_argument(
        "--only", default="S1,S2,S6", help="comma-separated subset of S1,S2,S6"
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


class ColimaSpike:
    def __init__(self, args: argparse.Namespace, report: Report) -> None:
        self.args = args
        self.report = report
        self.runtime = container_cli.colima_runtime(args.colima_profile)
        self.engine = Engine(self.runtime.argv(), apple=False, report=report)
        self.hosts = lib.HostServices(args.loopback_port, args.any_port)
        self.host_address = DEFAULT_HOST
        self.gateway_ip = ""
        self.uplink_ip = ""
        self.sandbox_net: ipaddress.IPv4Network | None = None
        self._vm_ports: list[int] | None = None

    # Setup -------------------------------------------------------------------
    def environment(self) -> None:
        r, e = self.report, self.engine
        r.h("Environment")
        profile = self.args.colima_profile
        rows = []
        for label, argv in (
            ("macOS", ["sw_vers", "-productVersion"]),
            ("colima", ["colima", "version"]),
            (
                "docker client/server",
                [
                    *self.runtime.argv(
                        "version",
                        "--format",
                        "{{.Client.Version}} / {{.Server.Version}}",
                    )
                ],
            ),
            ("colima status", ["colima", "status", "--profile", profile, "--json"]),
        ):
            proc = lib.sh(argv, r, timeout=60)
            text = (proc.stdout or proc.stderr).strip()
            r.data[label] = text
            rows.append((label, text.splitlines()[0] if text else "?"))
        listing = lib.sh(["colima", "list", "--json"], r, timeout=60).stdout
        for line in listing.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("name") == profile:
                r.data["colima_profile"] = entry
                rows.append(("profile", json.dumps(entry)))
        r.table(("Item", "Value"), rows)
        r.code(r.data.get("colima status", ""), "json")
        if e.run("info", timeout=60).returncode != 0:
            raise lib.SpikeError(
                f"Docker context {self.runtime.context} does not answer; "
                f"start the profile with `colima start --profile {profile}`"
            )

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
        for name, role, internal in (
            (UPLINK, "egress-uplink", False),
            (SANDBOX, "sandbox", True),
        ):
            argv = colima_network.create_argv(
                self.runtime, colima_network.NetworkSpec(name, role, internal)
            )
            argv[-1:-1] = ["--label", lib.SPIKE_LABEL]
            r.code("$ " + " ".join(argv))
            lib.sh(argv, r, check=True, timeout=120)
        self.hosts.start()
        r.p(
            f"Mac test services: 127.0.0.1:{self.args.loopback_port} (loopback), "
            f"0.0.0.0:{self.args.any_port} (any)."
        )
        info = lib.learn_subnet(e, SANDBOX, r)
        self.sandbox_net = info["subnet"]
        self.internal_first_host = info["first_host"]
        # Gateway as the sidecar runs today: uplink first, ip_forward=0, then
        # connected to the internal network under the alias egress.
        e.start(GATEWAY, [UPLINK], extra=["--sysctl", "net.ipv4.ip_forward=0"])
        lib.sh(
            colima_network.connect_argv(self.runtime, SANDBOX, GATEWAY, "egress"),
            r,
            check=True,
        )
        addrs = e.exec_probe(GATEWAY, [{"kind": "addrs"}])[0]
        r.data["gateway_addrs"] = addrs
        self.gateway_ip = lib.address_in(addrs, self.sandbox_net) or ""
        self.uplink_ip = next(
            (str(i.ip) for i in lib.own_ipv4(addrs) if i.ip not in self.sandbox_net), ""
        )
        if not self.gateway_ip:
            raise lib.SpikeError(f"gateway has no address on {SANDBOX}: {addrs}")
        r.p(f"Gateway {GATEWAY}: internal {self.gateway_ip}, uplink {self.uplink_ip}.")

    def resolve_host(self) -> None:
        r, e = self.report, self.engine
        vm = lib.sh(
            [
                "colima",
                "--profile",
                self.args.colima_profile,
                "ssh",
                "--",
                "getent",
                "hosts",
                "host.lima.internal",
            ],
            r,
            timeout=60,
        )
        in_container = e.exec_probe(
            GATEWAY, [{"kind": "resolve", "name": "host.lima.internal"}]
        )[0]
        r.table(
            ("Where", "host.lima.internal"),
            [
                (
                    "VM (`colima ssh -- getent hosts`)",
                    (vm.stdout or vm.stderr).strip() or "(nothing)",
                ),
                ("container on the uplink", lib.describe(in_container)),
            ],
        )
        addresses = in_container.get("addresses") or (
            vm.stdout.split()[:1] if vm.returncode == 0 else []
        )
        if addresses:
            self.host_address = addresses[0]
        r.data["host_address"] = self.host_address
        r.p(
            f"Host address used for the cache_peer literal: {self.host_address}"
            + ("" if addresses else " (fallback: not resolved anywhere)")
        )

    def start_squid(self) -> dict[str, int]:
        r, e = self.report, self.engine
        services = {"loopback": self.args.loopback_port, "any": self.args.any_port}
        if lib.host_ollama_ok(self.args.ollama_model):
            services["ollama"] = 11434
        elif self.args.ollama_model:
            r.p(
                "Ollama did not answer on the Mac's 127.0.0.1:11434; skipping the real-Ollama stream."
            )
        conf = lib.squid_conf(
            self.gateway_ip, self.host_address, services, self.args.squid_directive
        )
        r.code(conf, "squid")
        proc = e.write_and_start_squid(GATEWAY, conf)
        r.code(
            f"$ squid -k parse && squid  (rc {proc.returncode})\n{proc.stderr[-2000:]}"
        )
        if proc.returncode != 0:
            raise lib.SpikeError("Squid did not accept the spike config (see report)")
        lib.wait_for_squid(e, GATEWAY, self.gateway_ip, r)
        return services

    # Spikes ------------------------------------------------------------------
    def s1(self) -> None:
        r, e = self.report, self.engine
        r.h("S1. Colima host path and streaming (task 0.1)")
        self.resolve_host()
        loop, anyp = self.args.loopback_port, self.args.any_port
        r.h("From the gateway on the uplink, direct", 3)
        ops = []
        for host in dict.fromkeys(("host.lima.internal", self.host_address)):
            ops += [
                {
                    "kind": "http",
                    "url": f"http://{host}:{loop}/",
                    "label": f"{host}:{loop} (127.0.0.1)",
                },
                {
                    "kind": "http",
                    "url": f"http://{host}:{anyp}/",
                    "label": f"{host}:{anyp} (0.0.0.0)",
                },
            ]
        ops += [
            {
                "kind": "stream",
                "url": f"http://{self.host_address}:{loop}/api/generate",
                "method": "POST",
                "body": "{}",
                "label": "baseline ndjson",
            },
            {
                "kind": "stream",
                "url": f"http://{self.host_address}:{loop}/sse",
                "sse": True,
                "label": "baseline sse",
            },
        ]
        results = e.exec_probe(GATEWAY, ops)
        r.data["s1_direct"] = results
        failures, rows = [], []
        for res in results:
            label = res["op"]["label"]
            if res["op"]["kind"] == "stream":
                ok, detail = lib.grade_stream(res)
            else:
                ok = res.get("status") == 200 and lib.MARKER in res.get("body", "")
                detail = lib.describe(res)
            rows.append((label, detail, "ok" if ok else "**FAIL**"))
            if not ok:
                failures.append(f"direct {label}: {detail}")
        r.table(("Request", "Observed", "Result"), rows)

        r.h("From a sandbox on the internal network, through Squid", 3)
        services = self.start_squid()
        failures += lib.service_checks(
            r,
            e,
            networks=[SANDBOX],
            gateway_ip=self.gateway_ip,
            services=services,
            host_label="host.lima.internal",
            host_address=self.host_address,
            host_name="host.lima.internal",
            ollama_model=self.args.ollama_model,
            hosts=self.hosts,
        )
        status = (
            r.data.get("colima status", "")
            + json.dumps(r.data.get("colima_profile", {}))
        ).lower()
        # colima status reports VZ as "driver": "macOS Virtualization.Framework".
        if "vz" not in status and "virtualization.framework" not in status:
            failures.append(
                "the profile does not look like the VZ driver (see Environment); S1 asks for VZ"
            )
        r.verdict(
            "S1",
            "PASS" if not failures else "FAIL (review: WORKAROUND or BLOCKED)",
            failures,
        )

    def s2(self) -> None:
        r, e = self.report, self.engine
        r.h("S2. Colima containment without the firewall (task 0.2)")
        if "host_address" not in r.data:
            self.resolve_host()
        bridge = (
            e.run(
                "network",
                "inspect",
                "bridge",
                "--format",
                "{{(index .IPAM.Config 0).Gateway}}",
            ).stdout.strip()
            or "172.17.0.1"
        )
        e.start(
            lib.BRIDGE_PEER,
            [],
            command=["python3", "-m", "http.server", str(PEER_PORT)],
        )
        peer = e.exec_probe(lib.BRIDGE_PEER, [{"kind": "addrs"}])[0]
        peer_ip = next((str(i.ip) for i in lib.own_ipv4(peer)), "")
        h, loop, anyp = self.host_address, self.args.loopback_port, self.args.any_port
        host_ports = [
            (22, "ssh"),
            (80, "http"),
            (11434, "ollama"),
            (loop, "test service on 127.0.0.1"),
            (anyp, "test service on 0.0.0.0"),
            (lib.CLOSED_PORT, "closed port"),
        ]
        ops = [tcp(h, p, f"Mac {h}:{p} ({what})") for p, what in host_ports]
        ops += [
            tcp("1.1.1.1", 443, "public 1.1.1.1:443"),
            tcp("8.8.8.8", 53, "public 8.8.8.8:53/tcp"),
            udp_dns("1.1.1.1", "DNS to 1.1.1.1"),
            udp_dns("192.168.5.3", "DNS to Lima's host resolver 192.168.5.3"),
            udp_dns("@resolv", "DNS to the default resolver"),
            udp_dns(
                "@resolv",
                "default resolver, tunnel-style name",
                name="psbx-spike-tunnel-check.example.com",
            ),
            {
                "kind": "resolve",
                "name": "example.com",
                "label": "system resolver: example.com",
            },
            tcp("2606:4700:4700::1111", 443, "IPv6 [2606:4700:4700::1111]:443"),
            udp_dns("2606:4700:4700::1111", "IPv6 DNS"),
            *self.vm_listener_ops(self.internal_first_host),
            tcp(bridge, 22, f"default bridge gateway {bridge}:22"),
            tcp(bridge, anyp, f"default bridge gateway {bridge}:{anyp}"),
            tcp(
                peer_ip or "172.17.0.2",
                PEER_PORT,
                f"container on the default bridge {peer_ip}:{PEER_PORT}",
            ),
            tcp(
                self.uplink_ip,
                PROXY_PORT,
                f"gateway uplink {self.uplink_ip}:{PROXY_PORT}",
            ),
            tcp("169.254.169.254", 80, "metadata 169.254.169.254:80"),
        ]
        r.h("No capabilities, no firewall", 3)
        results = e.probe([SANDBOX], [{"kind": "addrs"}, *ops])
        r.data["s2_plain"] = results
        r.code(
            "$ ip route\n"
            + results[0].get("routes4", "")
            + "\n$ ip -6 route\n"
            + results[0].get("routes6", "")
        )
        failures = lib.containment_table(r, results)
        r.p(
            "A `refused` from a host address means the address answered with a reset; "
            "the decisive rows are the listening test services and the public addresses."
        )

        r.h("NET_ADMIN, default route via the gateway", 3)
        forward = e.exec_probe(
            GATEWAY, [{"kind": "read", "path": "/proc/sys/net/ipv4/ip_forward"}]
        )[0]
        r.p(f"Gateway net.ipv4.ip_forward = {forward.get('text', '?').strip()}")
        if forward.get("text", "").strip() != "0":
            failures.append("gateway ip_forward is not 0")
        route = {
            "kind": "sh",
            "cmd": [
                "sh",
                "-c",
                f"ip route replace default via {self.gateway_ip} && ip route",
            ],
        }
        bypass = [
            tcp("1.1.1.1", 443, "public 1.1.1.1:443 via gateway"),
            tcp("8.8.8.8", 53, "public 8.8.8.8:53/tcp via gateway"),
            udp_dns("1.1.1.1", "DNS to 1.1.1.1 via gateway"),
            tcp(h, loop, f"Mac {h}:{loop} via gateway"),
            tcp(h, anyp, f"Mac {h}:{anyp} via gateway"),
            tcp(bridge, 22, f"default bridge gateway {bridge}:22 via gateway"),
        ]
        results2 = e.probe(
            [SANDBOX], [route, *bypass], extra=["--cap-add", "NET_ADMIN"]
        )
        r.data["s2_net_admin"] = results2
        r.code(results2[0].get("out", lib.describe(results2[0])))
        if results2[0].get("rc") != 0:
            failures.append(
                "could not set the default route (see output); bypass not tested"
            )
        failures += lib.containment_table(r, results2)

        passing = self.gateway_mode_variants()
        if not failures:
            verdict = "PASS"
        elif passing:
            verdict = "WORKAROUND"
            failures.append(
                "a sandbox network created with "
                + " or ".join(f"`{' '.join(v)}`" for v in passing)
                + " closes these (see the variants table)"
            )
        else:
            verdict = "FAIL (review: WORKAROUND or BLOCKED)"
        r.verdict("S2", verdict, failures)

    # S2 helpers ----------------------------------------------------------------
    def vm_listeners(self) -> list[int]:
        """TCP ports the Colima VM listens on for non-loopback addresses."""
        if self._vm_ports is not None:
            return self._vm_ports
        proc = lib.sh(
            [
                "colima",
                "--profile",
                self.args.colima_profile,
                "ssh",
                "--",
                "ss",
                "-Hltn",
            ],
            self.report,
            timeout=60,
        )
        ports = set()
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            address, _, port = fields[3].rpartition(":")
            if (
                port.isdigit()
                and address.strip("[]") not in ("127.0.0.1", "::1")
                and not (address.startswith("127."))
            ):
                ports.add(int(port))
        self.report.p("Colima VM TCP listeners (`ss -Hltn`):")
        self.report.code(proc.stdout or proc.stderr)
        print(proc.stdout or proc.stderr, flush=True)
        self._vm_ports = sorted(ports | {22, 53})
        return self._vm_ports

    def vm_listener_ops(self, address: str) -> list[dict]:
        return [
            tcp(address, port, f"internal network gateway (the VM) {address}:{port}")
            for port in self.vm_listeners()
        ]

    def gateway_mode_variants(self) -> list[list[str]]:
        """Re-probe the VM from internal networks whose bridge has no host address.

        Returns the option sets that blocked every VM probe while the sidecar
        stayed reachable.
        """
        r, e = self.report, self.engine
        r.h("Internal network variants without a VM address on the bridge", 3)
        r.p(
            "The plain `--internal` bridge still gives the VM an address (the network "
            "gateway), so VM listeners answer. Each variant below is a separate "
            "internal network; the gateway joins it, and a sandbox probes the gateway "
            f"(must connect, port {GATEWAY_LISTENER_PORT}) and the VM (must not)."
        )
        e.exec(
            GATEWAY,
            "sh",
            "-c",
            f"nohup python3 -m http.server {GATEWAY_LISTENER_PORT} >/dev/null 2>&1 &",
        )
        variants = [
            (
                lib.SANDBOX_VARIANTS[0],
                ["-o", "com.docker.network.bridge.gateway_mode_ipv4=isolated"],
            ),
            (
                lib.SANDBOX_VARIANTS[1],
                ["-o", "com.docker.network.bridge.inhibit_ipv4=true"],
            ),
        ]
        rows, passing = [], []
        h, loop, anyp = self.host_address, self.args.loopback_port, self.args.any_port
        for name, options in variants:
            argv = colima_network.create_argv(
                self.runtime, colima_network.NetworkSpec(name, "sandbox", True)
            )
            argv[-1:-1] = [*options, "--label", lib.SPIKE_LABEL]
            created = lib.sh(argv, r, timeout=120)
            shown = " ".join(options)
            if created.returncode != 0:
                rows.append(
                    (shown, "network create refused", created.stderr.strip()[:200], "-")
                )
                continue
            inspect = e.run(
                "network",
                "inspect",
                name,
                "--format",
                "{{json .IPAM.Config}} {{json .Options}}",
            )
            r.data[f"variant:{name}"] = inspect.stdout
            connect = lib.sh(
                colima_network.connect_argv(self.runtime, name, GATEWAY, "egress"),
                r,
                timeout=120,
            )
            if connect.returncode != 0:
                rows.append(
                    (
                        shown,
                        "gateway connect refused",
                        connect.stderr.strip()[:200],
                        "-",
                    )
                )
                continue
            info = lib.learn_subnet(e, name, r)
            addrs = e.exec_probe(GATEWAY, [{"kind": "addrs"}])[0]
            gateway_ip = lib.address_in(addrs, info["subnet"]) or ""
            vm_bridge = e.run(
                "network", "inspect", name, "--format", "{{.Id}}"
            ).stdout.strip()[:12]
            bridge_addr = lib.sh(
                [
                    "colima",
                    "--profile",
                    self.args.colima_profile,
                    "ssh",
                    "--",
                    "ip",
                    "-o",
                    "addr",
                    "show",
                    f"br-{vm_bridge}",
                ],
                r,
                timeout=60,
            )
            bridge_text = bridge_addr.stdout or ""
            vm_ipv4 = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", bridge_text)
            vm_link_local = re.findall(r"\binet6 (fe80::[0-9a-f:]+)/", bridge_text)
            # The VM is reachable only through an address on this bridge: any IPv4
            # address, or its IPv6 link-local one if the sandbox has IPv6 enabled.
            vm_ops = [
                tcp(address, port, f"VM {address}:{port}")
                for address in vm_ipv4
                for port in self.vm_listeners()
            ] + [
                tcp(f"{ll}%eth0", 22, f"VM link-local [{ll}%eth0]:22")
                for ll in vm_link_local
            ]
            ops = [
                tcp(gateway_ip, GATEWAY_LISTENER_PORT, "sidecar by address"),
                tcp("egress", GATEWAY_LISTENER_PORT, "sidecar by alias egress"),
                {"kind": "read", "path": "/proc/sys/net/ipv6/conf/eth0/disable_ipv6"},
                *vm_ops,
                tcp(h, loop, f"Mac {h}:{loop}"),
                tcp(h, anyp, f"Mac {h}:{anyp}"),
                tcp("1.1.1.1", 443, "public 1.1.1.1:443"),
                udp_dns("@resolv", "DNS to the default resolver"),
                tcp("169.254.169.254", 80, "metadata 169.254.169.254:80"),
            ]
            results = e.probe([name], ops)
            r.data[f"s2_variant:{name}"] = results
            reach, alias, ipv6 = results[0], results[1], results[2]
            leaks = [
                f"{res['op']['label']}: {lib.describe(res)}"
                for res in results[3:]
                if not lib.is_blocked(res)
            ]
            if vm_ipv4:
                leaks.insert(0, f"the VM has IPv4 {', '.join(vm_ipv4)} on the bridge")
            ok = reach.get("outcome") == "connected" and not leaks
            disabled = ipv6.get("text", lib.describe(ipv6)).strip()
            rows.append(
                (
                    shown,
                    f"{lib.describe(reach)}; alias: {lib.describe(alias)}",
                    f"VM IPv4: {', '.join(vm_ipv4) or 'none'}; sandbox disable_ipv6={disabled}",
                    "; ".join(leaks) or "VM, Mac, Internet and metadata all blocked",
                    "PASS" if ok else "**FAIL**",
                )
            )
            r.p(
                f"`{shown}`: VM bridge `br-{vm_bridge}`: `{bridge_text.strip() or 'no address'}`"
            )
            r.table(
                ("Sidecar (must connect)", "Outcome"),
                [(res["op"]["label"], lib.describe(res)) for res in (reach, alias)],
            )
            lib.containment_table(r, results[3:])
            if ok:
                passing.append(options)
        r.table(
            (
                "Options",
                "Sidecar reachable",
                "VM address / sandbox IPv6",
                "Leaks",
                "Result",
            ),
            rows,
        )
        return passing

    def s6(self, services_started: bool) -> None:
        r = self.report
        r.h("S6. Attribution (task 0.6)")
        if not services_started:
            if "host_address" not in r.data:
                self.resolve_host()
            self.start_squid()
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
    unknown = only - {"S1", "S2", "S6"}
    if unknown:
        raise SystemExit(f"unknown spikes: {', '.join(sorted(unknown))}")
    report = Report(f"colima-{args.colima_profile}", args.out)
    spike = ColimaSpike(args, report)
    if args.cleanup:
        spike.engine.cleanup(image=True)
        print("removed psbx-spike-* containers, networks and image (where present)")
        return 0
    code = 0
    try:
        spike.environment()
        spike.setup()
        if "S1" in only:
            spike.s1()
        if "S2" in only:
            spike.s2()
        if "S6" in only:
            spike.s6(services_started="S1" in only)
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
