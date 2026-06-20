# TODO — outstanding items

Each item below carries a short **Problem** statement, an **Implementation
strategy** naming the real files/functions to touch, and a **Test** line for the
regression coverage to add (per CLAUDE.md, every fix ships with a test). File
references are `path:line` against the current tree.

## Firewall: verify multi-resolver rules on a real iptables host

**Problem.** Code is **complete**: `init-firewall.sh.j2` collects all IPv4/IPv6
nameservers via `mapfile` and emits NAT/ACCEPT rules per resolver (no more
`{print $2; exit}` for DNS), with a `127.0.0.11` fallback; README says
"resolver(s)";
`tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`
covers the rendered script. Unit tests are render-only by policy and do **not**
exercise live iptables.

**Implementation strategy.** No source change expected — this is a manual
validation gate. On a Linux host with `iptables`/`ip6tables` and a
`/etc/resolv.conf` containing multiple `nameserver` entries (mix IPv4 and IPv6),
run the rendered `.project-sandbox/init-firewall.sh` as root and confirm DNS to
every listed resolver is permitted and nothing else leaks. This is the network
security boundary, so treat it as unshipped until exercised live.

**Test.** Render coverage already exists; record the live-host result (resolvers
allowed, no other egress) in the PR description. If the run reveals a gap, add a
render regression in `tests/test_renderers.py` for the corrected rule shape.

## Repository review findings (2026-06-19) — resolved

All High, Medium, and Low items from the 2026-06-19 multi-agent review have
been implemented with regression tests (see git history). The only remaining
entry is the manual live-iptables validation gate above, which is a host
validation step rather than a code change.
