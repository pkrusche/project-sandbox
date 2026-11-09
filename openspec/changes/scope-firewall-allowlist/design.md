# Design

## Context

`firewall.render` (`cli.py:763`) renders `init-firewall.sh` from `init-firewall.sh.j2`, and `Dockerfile.j2` `COPY`s it into the image (`:128`). In normal policy, the script:
- imports GitHub meta CIDRs as root, with `curl` and `jq`, when `--allow-github` is set (`:153–166`);
- builds `DOMAINS` from the seven provider hosts (omitted with an agent proxy), the GitHub list, Copilot tenant hosts and `--extra-domain` (`:168–203`);
- resolves each with `dig`, adds A/AAAA answers matching a bare regex to `hash:net` ipsets, and pins them in `/etc/hosts` (`:208–225`);
- drops DNS, then accepts `--match-set allowed-ipv4 dst` with no protocol or port (`:316–317`).

Local destinations (agent proxy, Internet proxy, Ollama) already go through the isolated Python endpoint helper, with exact address and port rules.

`egress_policy.PROVIDER_HOSTS` duplicates the seven hosts for the sidecar. `_allow_github` turns GitHub access on for `--allow-github` or `--agent copilot` (`cli.py:2812`). Sidecar mode refuses `--allow-github` and Copilot tenants.

## Goals / Non-Goals

**Goals:**
- Least-privilege egress per session in firewall mode, without per-agent images.
- One provider table shared by both egress modes.

**Non-Goals:**
- Hostname-level enforcement in firewall mode. IP pinning stays IP pinning, and the docs say so.
- Per-session sidecar policies (ROADMAP: per-sandbox attribution).
- Sidecar hardening such as HTTPS-only (`sidecar-policy-hardening`).

## Decisions

### 1. Mount the rendered firewall instead of baking it

- **Where it's written:** the per-session firewall script goes into `staging/<id>/firewall/init-firewall.sh` (`private-session-state`), mounted read-only at `/project-sandbox-firewall`.
- **Who runs it:** the root stage (`isolate-firewall-helpers`) runs `/project-sandbox-firewall/init-firewall.sh` with `env -i`, `cd /` and `python3 -I`. If the firewall is required and the file is missing, the root stage exits non-zero.
- **Image:** `Dockerfile.j2` drops the firewall `COPY`. The image keeps the tools the script needs (`iptables`, `ipset`, `dig`, `python3`). `jq` and `aggregate` stay only if something else needs them.

**Why this is safe:**
- The script comes from the host's private, per-session staging.
- It's mounted read-only, and runs before any agent process exists.
- User mounts can't target `/project-sandbox-*` (`validate-all-mount-sources`).

**Alternative considered:** keep baking the script, and accept one image per agent. Agent switches would mostly hit the layer cache, but each switch moves the shared tag, and every allowlist tweak still rebuilds. That also keeps network policy inside image identity, the source of TODO.md item 1. Rejected.

### 2. One provider table

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

### 3. Domain resolution in the isolated helper, with classification

The bash `dig` loop is replaced by extending `_resolve_endpoints.py.j2`, run under `python3 -I` as in change 1. For each `(host, port, private_ok)` entry, the helper:
1. resolves A and AAAA with `socket.getaddrinfo` against the resolvers in `/etc/resolv.conf`, which is the same NSS path endpoints already use;
2. normalizes each address: IPv4-mapped IPv6 becomes IPv4, and a NAT64 `64:ff9b::/96` address yields its embedded IPv4;
3. if `private_ok` is false, drops any address whose normalized form is not `is_global`, printing a warning;
4. emits validated shell arrays of `addr,tcp:port` plus `/etc/hosts` lines, exactly as the endpoint helper already does, with no free-form text.

### 4. Address and port sets

- `ipset create allowed-ipv4 hash:ip,port family inet`, plus the IPv6 equivalent.
- Entries: `ipset add allowed-ipv4 <ip>,tcp:<port>`.
- The accept rule becomes `-m set --match-set allowed-ipv4 dst,dst -j ACCEPT`, placed after the DNS drops as today.
- The ESTABLISHED/RELATED rules and the final REJECT are unchanged.

### 5. CLI surface

- **`--extra-domain HOST[:PORT]`:** validated by the existing hostname regex plus a port from 1 to 65535. The default port is 443.
- **`--allow-private-destination HOST:PORT`:** repeatable, and the port is required. It's validated the same way, and resolved with `private_ok=True`. It's also rejected together with `--internet-proxy`, as `--extra-domain` already is.
- **`--allow-github`:** kept in `argparse`, and prints "deprecated and ignored; use `--extra-domain` for specific hosts". `_allow_github` and the template's GitHub block are deleted.
- **Copilot:** `--agent copilot` without `--egress sidecar` is refused in `_validate_copilot_args`, before any token resolution. Tenant hosts are refused in every mode, until a sidecar tenant profile exists (ROADMAP).

## Risks / Trade-offs

- [Narrower sets break a flow that needs a host the captures missed, such as a rare refresh endpoint] → Captures cover the headless and login/refresh flows per agent. The firewall prints the admitted hosts at startup, and the docs describe `--extra-domain` as the escape hatch. Captures are re-run when agent versions are bumped (`scripts/update-pins.py` workflow).
- [Shared CDN addresses still admit unrelated sites on 443] → This is inherent to IP pinning, and documented. Narrowing hosts and ports reduces it, and sidecar mode is the hostname-level option.
- [Copilot users in firewall mode, and GHE.com tenant users] → **BREAKING**. The error message gives the sidecar setup steps. Tenants are tracked in ROADMAP.
- [Corporate users whose extra domains resolve to private ranges] → A clear warning per dropped address, and `--allow-private-destination` with an explicit port.
- [project-sandbox-tui passes `--allow-github`] → It's still accepted, so nothing breaks. The TUI can drop the option in a later release.

## Migration Plan

- No data migration.
- Images rebuild once, because the Dockerfile no longer contains the firewall `COPY`. After that, network changes never rebuild.
- Release notes list the breaking flags and replacements: Copilot requires the sidecar; `--allow-github` is ignored; extra domains are 443-only unless a port is given; private answers need `--allow-private-destination`.
