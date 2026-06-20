# TODO — outstanding items

## Firewall: verify multi-resolver rules on a real iptables host
  
Code is **complete**: `init-firewall.sh.j2` now collects all IPv4/IPv6
nameservers via `mapfile` while preserving resolver NAT rules (no more
`{print $2; exit}` for DNS), with a `127.0.0.11` fallback; README documents the
pre-resolve-then-block DNS behavior; `tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`
covers the rendered script. The unit tests are render-only by policy and do
**not** exercise live iptables. Outstanding: run the rendered script on a host
with iptables (multiple `nameserver` entries in `resolv.conf`) and confirm
allowlisted-domain pre-resolution works across the resolver setup and that
post-firewall DNS egress does not leak before treating this as shipped — it is
the network security boundary.
