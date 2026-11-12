# Design

## Context

`firewall.render` renders `init-firewall.sh` from `init-firewall.sh.j2`. After `mount-session-firewall`, it is written to the session's private inputs directory and mounted read-only. In normal policy, the script:
- imports GitHub meta CIDRs as root, with `curl` and `jq`, when `--allow-github` is set;
- builds `DOMAINS` from the seven provider hosts (omitted with an agent proxy), the GitHub list, Copilot tenant hosts and `--extra-domain`;
- resolves each with `dig`, adds A/AAAA answers matching a bare regex to `hash:net` ipsets, and pins them in `/etc/hosts`;
- drops DNS, then accepts `--match-set allowed-ipv4 dst` with no protocol or port.

Local destinations (agent proxy, Internet proxy, Ollama) already go through the isolated Python endpoint helper, with exact address and port rules.

`egress_policy.PROVIDER_HOSTS` duplicates the seven hosts for the sidecar. `_allow_github` turns GitHub access on for `--allow-github` or `--agent copilot`. Sidecar mode refuses `--allow-github` and Copilot tenants.

## Goals / Non-Goals

**Goals:**
- Least-privilege egress per session in firewall mode.
- One provider table shared by both egress modes.

**Non-Goals:**
- Hostname-level enforcement in firewall mode. IP pinning stays IP pinning, and the docs say so.
- Per-session sidecar policies (ROADMAP: per-sandbox attribution).
- Sidecar hardening such as HTTPS-only (`sidecar-policy-hardening`).

## Decisions

### 1. One provider table

```python
PROVIDER_HOST_SETS = {
    "claude":   ("api.anthropic.com", "claude.ai", "code.claude.com", "platform.claude.com"),
    "codex":    ("api.openai.com", "auth.openai.com", "chatgpt.com"),
    "opencode": ANTHROPIC + OPENAI,   # configured provider families
    "pi":       ANTHROPIC + OPENAI,   # () with --pi-ollama
}
PROVIDER_HOSTS = union(PROVIDER_HOST_SETS.values())   # sidecar, and --agent bash
```

The initial values preserve today's hosts, split by family. Task 1.1 narrows them from captures: run each agent headless and through its login or refresh flow behind the sidecar, collect the access log, and keep only the hosts actually used. That likely removes `claude.ai`/`chatgpt.com` from headless sets, which are the CDN-fronted hosts.

If a session's set changes between headless and interactive (login flows), the table gains a second, per-mode column. The spec's per-agent rule doesn't change.

### 2. Domain resolution in the isolated helper, with classification

The bash `dig` loop is replaced by extending `_resolve_endpoints.py.j2`, which the firewall already runs under `python3 -I` from `/`. For each `(host, port, private_ok)` entry, the helper:
1. resolves A and AAAA with `socket.getaddrinfo` against the resolvers in `/etc/resolv.conf`, which is the same NSS path endpoints already use;
2. normalizes each address: IPv4-mapped IPv6 becomes IPv4, and a NAT64 `64:ff9b::/96` address yields its embedded IPv4;
3. if `private_ok` is false, drops any address whose normalized form is not `is_global`, printing a warning;
4. emits validated shell arrays of `addr,tcp:port` plus `/etc/hosts` lines, exactly as the endpoint helper already does, with no free-form text.

### 3. Address and port sets

- `ipset create allowed-ipv4 hash:ip,port family inet`, plus the IPv6 equivalent.
- Entries: `ipset add allowed-ipv4 <ip>,tcp:<port>`.
- The accept rule becomes `-m set --match-set allowed-ipv4 dst,dst -j ACCEPT`, placed after the DNS drops as today.
- The ESTABLISHED/RELATED rules and the final REJECT are unchanged.

### 4. CLI surface

- **`--extra-domain HOST[:PORT]`:** validated by the existing hostname regex plus a port from 1 to 65535. The default port is 443.
- **`--allow-private-destination HOST:PORT`:** repeatable, and the port is required. It's validated the same way, and resolved with `private_ok=True`. It's also rejected together with `--internet-proxy`, as `--extra-domain` already is.
- **`--allow-github`:** kept in `argparse`, and prints "deprecated and ignored; use `--extra-domain` for specific hosts". `_allow_github` and the template's GitHub block are deleted.
- **Copilot:** `--agent copilot` without `--egress sidecar` is refused in `_validate_copilot_args`, before any token resolution.
- **Copilot tenants:** `--copilot-host <tenant>.ghe.com` is still refused in sidecar mode, as today. In firewall mode it requires `--copilot-tenant-firewall`. With the flag, the firewall admits the tenant's Copilot hosts (the set `copilot_auth` already derives from the tenant name) on TCP 443, and nothing else of GitHub. The launch prints that the tenant's API host is reachable as a whole with the Copilot token. `--copilot-tenant-firewall` without a tenant host is a usage error.

## Risks / Trade-offs

- [Narrower sets break a flow that needs a host the captures missed, such as a rare refresh endpoint] → Captures cover the headless and login/refresh flows per agent. The firewall prints the admitted hosts at startup, and the docs describe `--extra-domain` as the escape hatch. Captures are re-run when agent versions are bumped (`scripts/update-pins.py` workflow).
- [Shared CDN addresses still admit unrelated sites on 443] → This is inherent to IP pinning, and documented. Narrowing hosts and ports reduces it, and sidecar mode is the hostname-level option.
- [Copilot users in firewall mode] → **BREAKING**. The error message gives the sidecar setup steps.
- [GHE.com tenant users] → They add one flag. Firewall mode can't limit the tenant API host to Copilot paths, which the flag's notice states. The exception is removed with the sidecar tenant profile (ROADMAP).
- [Corporate users whose extra domains resolve to private ranges] → A clear warning per dropped address, and `--allow-private-destination` with an explicit port.
- [project-sandbox-tui passes `--allow-github`] → It's still accepted, so nothing breaks. The TUI can drop the option in a later release.

## Migration Plan

- No data migration.
- Release notes list the breaking flags and replacements: Copilot requires the sidecar, or `--copilot-tenant-firewall` for GHE.com tenants; `--allow-github` is ignored; extra domains are 443-only unless a port is given; private answers need `--allow-private-destination`.
