# Spikes

Verdicts for tasks 0.1–0.6, reviewed in task 0.7. Each spike records its
versions, commands, observed output and a PASS / WORKAROUND / BLOCKED verdict.

## How to run

The scripts live in `spikes/` and need only the project's virtualenv. Run them
as your normal user from the repository root:

```bash
uv run python openspec/changes/isolated-sidecar-network/spikes/spike_colima.py   # S1, S2, S6
uv run python openspec/changes/isolated-sidecar-network/spikes/spike_apple.py    # S3, S4, S5, S6
```

- `--only S2` (or `S3,S4`, …) runs a subset. On Apple, S5 and S6 also run S4's
  gateway setup.
- `--ollama-model NAME` adds a stream from the Mac's real Ollama.
- `--squid-directive 'read_ahead_gap 1 KB'` tries Squid tuning if a stream buffers.
- `--keep` leaves the `psbx-spike-*` containers and networks for inspection,
  and `--cleanup` removes them and the spike image.

Each run writes `spikes/results/<runtime>-<timestamp>.md` (tables, suggested
verdicts, command log) and a `.json` with the raw probe output. The reports
stay local: `spikes/results/` is gitignored, because they hold host paths and
machine details. This file is the committed record. The scripts
use their own `psbx-spike-*` networks and containers, created with the same
flags as the project's. They never touch the real sidecar, the Docker context,
a Colima VM or Apple `container`'s DNS settings, and never run `sudo`.

While a run is in progress, two test services listen on the Mac:
`127.0.0.1:18434` and `0.0.0.0:18435`. If macOS asks whether Python may accept
incoming connections, allow it. Otherwise the 0.0.0.0 probes measure the macOS
firewall instead of the runtime's network.

S5 needs the `host.docker.internal` alias (`docs/apple-container-dns.md`).

## S1. Colima host path and streaming

**Verdict: PASS** (run `results/colima-default-20261116T222640Z.md`; the earlier
run's FAIL was a script bug that did not recognise the VZ driver string).

Environment: macOS 27.2.1, Colima v0.10.3 (driver "macOS Virtualization.Framework",
i.e. VZ, virtiofs, aarch64, 2 CPUs, 2 GiB), Docker client 29.9.3, server 29.5.2.

- `host.lima.internal` resolves to `192.168.5.2` both in the VM (`getent hosts`)
  and in a container on the uplink network (Docker's embedded DNS forwards it).
- From the gateway on the uplink, a Mac service bound to `127.0.0.1` and one bound
  to `0.0.0.0` both answer through `host.lima.internal` and through the literal.
  The Mac sees every request as coming from `127.0.0.1` (Lima's host forwarding),
  so a host service cannot tell a sandbox from any other client in the VM.
- Squid with D3's layout (an `accel` port and an `originserver` `cache_peer` per
  service, the literal `192.168.5.2` as the peer) on the gateway's internal address:

  | Check from a sandbox on the internal network | Observed |
  |---|---|
  | `egress:<port>` for both services | HTTP 200 from the host service |
  | NDJSON stream (Ollama-style, 6 lines, 1 s apart) | first line 0.51 s, last 5.53 s, max skew 0.02 s (direct baseline identical) |
  | SSE stream (1 event/s) | first event 0.51 s, last 5.53 s, max skew 0.02 s |
  | `GET http://example.com/` sent to a service port | rewritten to the service: logged as `GET http://loopback.service/`, served by the host service; nothing reached example.com |
  | `CONNECT example.com:443` on a service port | `405 Method Not Allowed` |
  | `egress:22`, `egress:8081` (undeclared) | refused |
  | Forward proxy to a public host | 200 |
  | Forward proxy to `http://192.168.5.2:<port>/` and `http://host.lima.internal:<port>/` | 403 (the project's `PRIVATE_DESTINATIONS` covers `192.168.0.0/16`) |

  No Squid tuning was needed: default `read_ahead_gap`, no `delay_pools`.

## S2. Colima containment without the firewall

**Verdict: WORKAROUND**: the plain `--internal` network exposes the Colima VM's
own listeners. An internal network created with
`-o com.docker.network.bridge.gateway_mode_ipv4=isolated` (or the older
`com.docker.network.bridge.inhibit_ipv4=true`) closes it. Run
`results/colima-default-20261116T222640Z.md`; environment as in S1.

From a container on `psbx-spike-sandbox` (same argv as `psbx-sandbox`) with no
added capabilities and no firewall. Its only route is `172.19.0.0/16 dev eth0`
and it has no IPv6 routes.

| Probe | Result |
|---|---|
| Mac `192.168.5.2` on 22, 80, 11434, both test services, a closed port | `ENETUNREACH` |
| Lima's host resolver `192.168.5.3:53/udp` | `ENETUNREACH` |
| TCP 1.1.1.1:443, 8.8.8.8:53; UDP DNS to 1.1.1.1 | `ENETUNREACH` |
| Default resolver (127.0.0.11) for `example.com` and a tunnel-style name | SERVFAIL, no answers; `getaddrinfo` fails |
| IPv6 TCP and UDP to 2606:4700:4700::1111 | `ENETUNREACH` |
| Default bridge gateway 172.17.0.1, a container on the default bridge | `ENETUNREACH` |
| Gateway's uplink address 172.18.0.2:3128, 169.254.169.254:80 | `ENETUNREACH` |
| **Internal network gateway 172.19.0.1:22 (the VM's sshd)** | **connected** |
| 172.19.0.1:53 | refused |

The VM listens on `0.0.0.0:22`, `[::]:22`, `127.0.0.1:53` and `192.168.5.1:53`
(`ss -Hltn` in the VM). An `--internal` bridge still gets the VM's address, so
anything listening on all VM interfaces answers a sandbox. Today that's sshd
(key-only, keys stay on the Mac), and any port another Colima container
publishes on `0.0.0.0`. Spike B did not probe this. Today's in-sandbox firewall
hides it, so it only matters with the firewall off.

With `NET_ADMIN` and the default route set to the gateway's internal address
(`ip_forward=0` in the gateway), 1.1.1.1:443, 8.8.8.8:53, UDP DNS, the Mac's
test services and 172.17.0.1:22 all time out.

**Variants.** Separate internal networks with the gateway attached as `egress`:

| Option | Sidecar reachable (address and alias) | VM address on the bridge | VM, Mac, Internet, metadata |
|---|---|---|---|
| `com.docker.network.bridge.gateway_mode_ipv4=isolated` | yes | IPv6 link-local only, no IPv4 | blocked |
| `com.docker.network.bridge.inhibit_ipv4=true` | yes | IPv6 link-local only, no IPv4 | blocked |

With either option the sidecar took the subnet's `.1` (no address is reserved for
the bridge). Confirmed in `results/colima-default-20261116T223039Z.md`:

- the sandbox has `net.ipv6.conf.eth0.disable_ipv6=1` and no IPv6 routes;
- a connection to the VM bridge's link-local address
  (`[fe80::c056:56ff:fe55:811f%eth0]:22`) fails with `ENETUNREACH`.

So the VM's `[::]:22` isn't reachable over link-local either. The VM's other
link-local listener (`[fe80::…]%eth0:53`) is on the VM's own `eth0`, not on
the bridge.

For 0.7: create `psbx-sandbox` with `gateway_mode_ipv4=isolated` (the
purpose-built option for internal networks, Docker 28+; Colima v0.10.3 ships
29.5.2). `colima_network.validate` must check the option. An existing
`psbx-sandbox` without it has to be replaced, and that needs every sandbox and
the sidecar detached first. Until the replacement ships, Colima's firewall
default must stay on.

## S3. Apple internal network

**Verdict: BLOCKED for containment without the sandbox firewall.** Run `results/apple-container-20261116T223834Z.md`.
Environment: macOS 27.2.1 (build 26B131), `container` 1.7.1 (commit a15b460),
arm64.

`container network create --internal --label …` works as a normal user, and the
labels show in `network inspect`. The network is **host-only**: subnet
`192.168.128.0/24` with a default route via the Mac's address on it,
`192.168.128.1`. It blocks the Internet, but not the Mac.

| Probe from a sandbox on it, no firewall | Result |
|---|---|
| TCP 1.1.1.1:443, 8.8.8.8:53; UDP DNS to 1.1.1.1 | timeout |
| IPv6 TCP and UDP to 2606:4700:4700::1111 | `ENETUNREACH` |
| `getaddrinfo(example.com)`, UDP DNS to the configured resolver and to `192.168.128.1` | fails / timeout |
| DNS over TCP to `192.168.128.1:53` (also the configured resolver) | connection accepted, no DNS reply |
| Default network gateway `192.168.64.1` (53, 80, test ports), 169.254.169.254:80 | timeout |
| **Mac `192.168.128.1`: AirPlay 5000 and 7000, the test service on `0.0.0.0`, TCP 53** | **connected** |
| Mac `192.168.128.1`: 22, 80, 445, 5900, the `127.0.0.1` test service, a closed port | timeout |
| **Localhost alias `203.0.113.113`: 11434 (Ollama), both test services** | **connected** |
| `203.0.113.113:80` (nothing listening) | timeout |

Two things are exposed:

- **Every Mac service that listens on all interfaces** is reachable on the
  host-only address. That covers whatever the user runs on `0.0.0.0` and
  macOS's own AirPlay receiver.
- **Every Mac loopback service** is reachable through `203.0.113.113`. The
  packet-filter redirect behind the `host.docker.internal` alias
  (`docs/apple-container-dns.md`) also applies to the host-only network. D4
  needs the alias for the sidecar to reach services, so the hole is there
  exactly when services are in use.

Closing either hole means filtering on the Mac (pf rules for the host-only
network), which needs administrator rights the project never uses.

## S4. Apple topology

**Verdict: WORKAROUND.** The script printed BLOCKED, but three of its four reasons
were script artifacts, fixed since. Run `results/apple-container-20261116T223834Z.md`.

| Check | Result |
|---|---|
| One container on a NAT network (`psbx-spike-uplink`) and the internal network | works with two `--network` flags; `eth0` uplink with the default route, `eth1` internal |
| Gateway → Internet | works: S5 and S6 reached example.com through it. The first probe, seconds after start, timed out; the script now retries |
| Sandbox → gateway on the internal network | works: S5 and S6 reach Squid on `egress`. The "refused" came from a test listener that `container exec` killed when its session ended (Squid daemonizes and survived); the script now uses `setsid` |
| Sandbox → sandbox on the internal network | connected (lateral reachability, as on Colima) |
| `container run --add-host` | not supported. D7's fallback applies: the entrypoint pins `egress` from the session inputs |
| Network aliases / container-name DNS on the network | none; names don't resolve |
| `container run --sysctl` | not supported, but `net.ipv4.ip_forward` is already 0 in the guest |
| Gateway's internal address after stop/start, after recreation | **changes**: `.4` → `.7` → `.8` |
| `NET_ADMIN` sandbox with the default route via the gateway | 1.1.1.1:443, UDP DNS, `203.0.113.113`, `192.168.64.1:53` all time out |

Because the address changes on every restart, a sidecar restart cuts off running
sandboxes until they're relaunched, since their `egress` pin is fixed at launch.
Today's published endpoint stays stable across sidecar restarts.

## S5. Apple host path and streaming

**Verdict: PASS.** Run `results/apple-container-20261116T223834Z.md`.

- In the gateway, `host.docker.internal` resolves to `203.0.113.113`, and both
  Mac test services answer through it. The Mac sees the requests coming from the
  gateway's uplink address (`192.168.65.4`).
- Through Squid with the literal `203.0.113.113` as the peer:
  - NDJSON stream: first line 0.51 s, last 5.53 s, max skew 0.02 s;
  - SSE stream: first event 0.51 s, last 5.53 s, max skew 0.01 s;
  - D3's separation works: an absolute-URI GET on a service port is served by
    that service, `CONNECT` there gets 405, and undeclared ports are refused.
- **Policy finding:** the forward proxy let `http://203.0.113.113:18434/` and
  `http://host.docker.internal:18434/` through to the Mac's loopback service.
  The project's `squid_config.PRIVATE_DESTINATIONS` doesn't include
  `203.0.113.0/24`.
  - The spike's forward port allowed every public host. In the shipped policy,
    the hostname allowlist and the IP-literal denial already stop both requests.
  - The remaining path is an allowlisted name that resolves to `203.0.113.113`.
  - Add `203.0.113.0/24` to `PRIVATE_DESTINATIONS` in every mode (task 4.1, and
    hardening for today's Apple sidecar).

## S6. Attribution

**Colima: distinct per-sandbox addresses** (run
`results/colima-default-20261116T222640Z.md`). Two concurrent sandboxes on the
internal network appear in Squid's access log with their own addresses:

```
… TCP_MISS/404 GET http://example.com/spike-s6-a lport=3128 client=172.19.0.3 …
… TCP_MISS/404 GET http://example.com/spike-s6-b lport=3128 client=172.19.0.4 …
```

Sequential `--rm` containers reuse an address, so attribution needs the
sandbox's address recorded with the session's time window. Squid strips query
strings from its log by default (`strip_query_terms`).

**Apple `container`: distinct per-sandbox addresses** (run
`results/apple-container-20261116T223834Z.md`):

```
… TCP_MISS/404 GET http://example.com/spike-s6-a lport=3128 client=192.168.128.11 …
… TCP_MISS/404 GET http://example.com/spike-s6-b lport=3128 client=192.168.128.12 …
```
