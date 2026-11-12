# Proposal

## Why

This line already parses `--mount` with a real CSV grammar, makes security
checks fail closed on mount forms whose host exposure is unclear, refuses
volumes in sidecar mode, and protects descendants of the sidecar state
directory (`ed02bb0`). Four gaps from the `sec-fixing-claude` branch's
`validate-all-mount-sources` draft remain:

- **Protected locations are checked only in sidecar mode.** A firewall-mode
  session can still mount the egress policy, the sidecar state (with the CA
  private key) or project-sandbox's own state root (history, masks, staged
  session inputs).
- **Credential directories can be mounted.** `--mount source=~/.ssh,…` or an
  agent's config directory bypasses per-agent credential forwarding.
- **Targets only need to be absolute.** A writable source mounted over
  `/usr/local/bin` or `/etc` puts agent-controlled files where root startup
  runs them.
- **Sources inside the project can resolve outside it,** through a symlink an
  earlier session planted.

## What Changes

- Every bind source, in every egress mode, is refused when it is, contains or
  lies inside the egress policy directory, the egress state directory or the
  project-sandbox state root.
- Every bind source is refused when it is, contains or lies inside a credential
  directory (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gh`,
  `~/.password-store`, the agents' host config directories). No override
  (decision recorded on the branch, 2026-11-09).
- Targets under `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/root`,
  `/proc`, `/sys`, `/dev`, `/run`, `/project-sandbox-*` and `/workspace` itself
  are refused; project-sandbox's masks stay after user mounts.
- A source lexically inside the project or workspace that resolves outside it
  is refused; the resolved path is what the runtime receives.
- **BREAKING:** `--mount` values hitting these rules now fail with an error
  naming the rule.
- Not ported: the branch's tagged mount model and comma-refusing encoder (this
  line chose CSV quoting and round-trip checks), and the firewall-mode volume
  warning.

## Capabilities

### New Capabilities
- `mount-validation`: the source and target rules every `--mount` value must
  pass in every egress mode.

### Modified Capabilities

None. The sidecar requirement "Policy lives on the host outside the sandbox's
reach" stays as it is; this capability adds the all-modes rule beside it.

## Impact

- Code: `mount_options.py` (target rules), `cli.py` (all-mode source checks,
  credential list), `egress_policy.check_location` reuse, `devcontainer.py`
  (same checks for devcontainer mounts).
- Docs: `docs/usage.md` (`--mount` rules), `docs/security.md`.
- Interplay: `derive-vcs-mounts-from-host` reuses the protected-location and
  credential lists for pointer-derived metadata mounts.
