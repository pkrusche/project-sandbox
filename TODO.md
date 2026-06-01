# TODO — outstanding items

Tracked follow-ups from the codebase review. The P0 correctness bugs, the
actionable P2 polish items, and the P1 de-duplication refactors are already
done; what remains below was deliberately deferred, with the reason.

## Needs a Linux / `apt` + iptables environment to validate

### Firewall: allow all `resolv.conf` resolvers, not just the first
- **Where:** `src/project_sandbox/templates/init-firewall.sh.j2` — the `DNS4`/`DNS6`
  `awk` lines use `... {print $2; exit}`, so only the first IPv4 and first IPv6
  nameserver are pinned; the NAT-preservation `grep` and the `ACCEPT` rules use
  those single values.
- **Current state:** the docs (`README.md`) were updated to say "the first
  resolver" so they match the script. If a VM's `resolv.conf` lists multiple
  resolvers, the others are dropped (rare in the apple/container & devcontainer
  VMs, which typically have one).
- **To do:** collect all `nameserver` entries into lists and emit NAT/ACCEPT
  rules for each, then revert the README wording to "resolver(s)". This touches
  the network security boundary, so it must be exercised on a machine with
  iptables — do **not** ship it unverified.

### Verify `--timeout` actually tears down the apple/container VM
- **Where:** `src/project_sandbox/session.py` (`_terminate_process_group`).
- **Current state:** on timeout we now SIGTERM→SIGKILL the whole `container run`
  process group (not just the immediate child), which should let `--rm` clean up.
  README reflects this.
- **To do:** confirm on a host with apple/container that the guest VM is gone
  after a timeout. If the VM lingers, give the run a known name/id and
  `container stop`/`kill` it explicitly in the timeout path.

## Low priority / nice to have

### Worktree directory name collision
- **Where:** `src/project_sandbox/worktree.py` (`path_for`) maps a branch to a dir
  via `branch.replace("/", "-")`, so `feat/x` and `feat-x` resolve to the same
  worktree directory.
- **To do:** add a disambiguating suffix (e.g. a short branch-name hash) if this
  ever bites. Deferred as low-probability; not worth the churn now.

### Remove stale `.pycache-test/` directory
- **Where:** repo root. Leftover from an old manual `PYTHONPYCACHEPREFIX` compile
  run; it references modules that no longer exist (`config_claude`, `config_codex`,
  `launcher`). It is gitignored and nothing in the repo writes it.
- **To do:** `rm -rf .pycache-test` (pure cleanup; no code change needed).
