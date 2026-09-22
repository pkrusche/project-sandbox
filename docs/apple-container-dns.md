# Apple container host alias

Agent-proxy, Internet-proxy, and Ollama forwarding all reach host-loopback
services through one shared name, `host.docker.internal`. On Docker Desktop and
rootless Podman the runtime provides that name natively, and on Linux bridge
runtimes project-sandbox starts a `socat` listener for it. Apple `container`
has neither path: it depends on an administrator-managed localhost DNS domain
plus the packet-filter redirect that domain installs. project-sandbox verifies
the mapping but never invokes `sudo` and never creates or removes host-wide
DNS/PF configuration.

## One-time setup

```bash
sudo container system dns create host.docker.internal --localhost 203.0.113.113
container system stop && container system start
```

The restart is not optional. Creating the domain changes DNS and PF state that
the running container system has already read; until it is restarted, the
runtime keeps the old networking and the redirect does not take effect. The
change can also disrupt ordinary container Internet access before the restart
and might disable Private Relay.

Confirm the domain is registered:

```bash
sudo container system dns list
```

## Recovering a stale alias

A registered alias can stop redirecting while still resolving — most often
after a container system restart, a macOS update, or an `mDNSResponder`
restart that dropped the domain registration. Re-running `dns create` does not
fix this: the domain already exists, so the create is a no-op or an error, and
the setup hints printed at failure time are misleading in exactly this case.

Delete the domain, re-create it, and restart the container system:

```bash
sudo container system dns delete host.docker.internal
sudo container system dns create host.docker.internal --localhost 203.0.113.113
container system stop && container system start
```

If the domain still does not take effect, flush the resolver and repeat. The
`container` CLI itself falls back to this when it cannot restart the resolver:

```bash
sudo killall -HUP mDNSResponder
```

## Recognizing the symptom

A stale alias shows up as a **connection timeout**, not a connection refusal.
`host.docker.internal` resolves to `203.0.113.113`, which is a TEST-NET-3
documentation address: with the PF redirect missing, the address still looks
routable, so SYNs are blackholed rather than rejected and the client hangs
until its own timeout.

This is diagnostically useful, because the sandbox firewall fails fast. The
generated `init-firewall.sh` terminates both chains with
`REJECT --reject-with icmp-admin-prohibited`, so a firewall block surfaces
immediately as "connection refused". A hang means packets left the container
and nothing answered.

```bash
# Inside the sandbox
getent ahostsv4 host.docker.internal
curl -v --connect-timeout 3 http://host.docker.internal:4000/v1/models
```

`curl -v` separates the two remaining cases: a stall after `Trying
203.0.113.113` is the stale alias described above, while a stall before it is a
DNS problem. Outbound port 53 is dropped silently (`-j DROP`, not `REJECT`) to
close the DNS-tunnel channel, and allowlisted names are pinned into
`/etc/hosts` instead, so any client that bypasses NSS and queries a resolver
directly will hang for its full resolver timeout.

Note that a session which started successfully with `--agent-proxy` had
working connectivity at the time: firewall initialization probes the proxy
after the final rules are installed and fails the container start if it is
unreachable. A timeout appearing later in that session points at the gateway
stopping, host PF state being rebuilt mid-session, or a slow upstream request
rather than at the alias.

## Related

- [Agent proxy](agent-proxy.md)
- [Internet proxy](internet-proxy.md)
- [Usage guide](usage.md) for `--pi-ollama`
- [Security model](security.md) for the firewall behavior referenced above
