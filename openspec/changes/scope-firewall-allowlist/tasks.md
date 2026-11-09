# Tasks

## 1. Provider host captures

- [ ] 1.1 For Claude, Codex, OpenCode and Pi, run one headless session and one login/token-refresh flow behind the egress sidecar with an allowlist of all provider hosts. Collect the hosts each one used from `project-sandbox-egress log`, and add a capture script alongside `scripts/e2e-copilot-egress.py`. Record the per-agent (and, if needed, per-mode) sets in design.md Decision 2. Verify that a headless session of each agent succeeds with only its captured set.

## 2. Runtime firewall delivery

- [ ] 2.1 Render the firewall into the session's private staging, mount it read-only at `/project-sandbox-firewall`, and have the root stage run it from there, failing closed when it's missing. Remove the firewall `COPY` from `Dockerfile.j2`. Verify with:
  - renderer tests (no firewall in the Dockerfile; root stage path);
  - argv tests (read-only mount present whenever the firewall is enabled);
  - a CLI test that switching `--agent` between two launches reuses the image.
- [ ] 2.2 Add an opt-in container test (`PROJECT_SANDBOX_CONTAINER_TESTS=1`) that starts a session with the firewall mount removed, and asserts a non-zero exit before the session command. Verify on Docker and Apple `container`.

## 3. Scoped hosts, ports and classification

- [ ] 3.1 Add `PROVIDER_HOST_SETS` and derive `PROVIDER_HOSTS` from it. Pass the agent's set, the union for `bash`, and none with an agent proxy or `--pi-ollama` to the renderer. Verify with renderer tests per agent and mode, and with an `egress_policy` test that the sidecar's provider set equals the union.
- [ ] 3.2 Extend the isolated resolver helper for domains, with address normalization and `is_global` classification (design Decision 3). Switch the ipsets to `hash:ip,port` with `dst,dst` matching. Verify with:
  - helper unit tests for loopback, RFC 1918, link-local, metadata, CGNAT, ULA, documentation, mapped, NAT64-embedded and global addresses;
  - renderer tests on the set type and accept rule;
  - an opt-in container test showing a pinned provider address is reachable on 443 and rejected on 80.
- [ ] 3.3 Add the `--extra-domain HOST[:PORT]` parsing and `--allow-private-destination HOST:PORT`, including the conflict with `--internet-proxy`. Verify with CLI validation tests and renderer tests for both.

## 4. GitHub and Copilot

- [ ] 4.1 Delete `_allow_github`, the template's GitHub block and the meta CIDR import. Make `--allow-github` print the deprecation warning and do nothing. Verify with a CLI test that the TUI-style argv (`--allow-github --extra-domain=… --agent=claude`) launches with the warning and no GitHub hosts in the rendered firewall.
- [ ] 4.2 Refuse `--agent copilot` without `--egress sidecar`, and refuse tenant `--copilot-host` values, before any token lookup. Update the Pi and OpenCode provider warnings to name only `--extra-domain`. Verify with CLI tests for each refusal (asserting no `pass`/`gh` call) and the warning text.

## 5. Docs

- [ ] 5.1 Update `docs/security.md` (per-agent sets, port scoping, classification, IP-pinning limits on shared CDNs, the sidecar's union semantics, runtime firewall delivery), `docs/usage.md` (new and deprecated flags), and the Copilot docs (sidecar only, tenants pending). Update TODO.md's networking table rows (firewall-mode identity, private addresses, GitHub breadth, default providers), and add the GHE.com sidecar tenant profile to ROADMAP. Verify that `uv run pytest -q` and `uv run python -m compileall -q src tests` pass.
