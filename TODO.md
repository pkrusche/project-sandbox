# TODO - outstanding items

## Model selection for batch mode

In unsupervised mode we should have a CLI switch to select a model. This model 
ID will be passed to the agent and used for all completions. This should work for
all three agents we support. To ensure users can find out the correct model names
to pass, the help text in the CLI should indicate which command to run to get the list of
model names for each agent.

## Firewall: verify multi-resolver rules on a real iptables host

Code is complete and the render path is covered by
`tests/test_renderers.py::test_firewall_collects_all_resolvers_not_just_first`.
The unit tests are render-only by policy and do not exercise live iptables.
Outstanding: run the rendered script on a host with iptables and multiple
`nameserver` entries in `resolv.conf`, then confirm allowlisted-domain
pre-resolution works across the resolver setup and post-firewall DNS egress does
not leak before treating this as shipped.
