# Spec Delta

## ADDED Requirements

### Requirement: Removing a host can terminate existing tunnels
`allow remove HOST… --terminate` SHALL remove the hosts and then restart the running sidecar's proxy, closing every existing connection. Before restarting while sandboxes are running, it SHALL ask for confirmation, unless `--yes` is given. Without `--terminate`, `allow remove` SHALL state that existing connections to the removed hosts stay open until they close or reach the tunnel lifetime.

#### Scenario: Remove without terminate
- **WHEN** the user runs `project-sandbox-egress allow remove pypi.org`
- **THEN** new connections to `pypi.org` are denied, and the output states that existing tunnels stay open until they close or reach `tunnel_lifetime`

#### Scenario: Remove with terminate
- **WHEN** the user runs `project-sandbox-egress allow remove pypi.org --terminate --yes`
- **THEN** the proxy restarts, and no connection to `pypi.org` remains open

#### Scenario: Confirmation while sessions run
- **WHEN** sidecar-mode sessions are running and the user runs `allow remove pypi.org --terminate` without `--yes`
- **THEN** the command asks for confirmation before restarting, and makes no change if the user declines
