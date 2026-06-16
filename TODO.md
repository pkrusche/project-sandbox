# TODO — outstanding items

## CLI option to generate a uv + Python Dockerfile automatically

Add a `--python-uv` flag (or similar) that generates a suitable base `Dockerfile`
for a Python/uv project without requiring one to be present in the repo.

- **Behaviour:** when passed, project-sandbox synthesises a `Dockerfile` equivalent
  to the pattern documented in `README.md` (Python slim base, uv binary copied from
  the official image, dependency-cache warming step, `ENV UV_CACHE_DIR`). The
  synthesised file is written to `.project-sandbox/Dockerfile.base` and passed
  internally as `--dockerfile`.
- **Inputs:** accept `--python VERSION` (default `3.11`) to control the base image
  tag so the user does not have to write the file just to change the Python version.
- **Edge cases:** warn (don't fail) if `pyproject.toml` / `uv.lock` are absent — the
  cache-warming step is skipped and only Python + uv are installed.
- **Why deferred:** the current `--dockerfile` path already covers the use case; the
  synthesised variant is a convenience shortcut that needs its own tests and a
  decision on how it interacts with `--dockerfile` and `base_image`.


## Keep persistent bash and session history

In the devcontainer & interactive (but not the unsupervised / batch) sessions, we 
should retain bash & prompt history for our agents. This can be accomplished by
mounting the corresponding files to a location in the .project-sandbox folder 
in the project.

## Firewall: allow all `resolv.conf` resolvers, not just the first
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

## Verify `--timeout` actually tears down the apple/container VM
- **Where:** `src/project_sandbox/session.py` (`_terminate_process_group`).
- **Current state:** on timeout we now SIGTERM→SIGKILL the whole `container run`
  process group (not just the immediate child), which should let `--rm` clean up.
  README reflects this.
- **To do:** confirm on a host with apple/container that the guest VM is gone
  after a timeout. If the VM lingers, give the run a known name/id and
  `container stop`/`kill` it explicitly in the timeout path.

## Worktree directory name collision
- **Where:** `src/project_sandbox/worktree.py` (`path_for`) maps a branch to a dir
  via `branch.replace("/", "-")`, so `feat/x` and `feat-x` resolve to the same
  worktree directory.
- **To do:** add a disambiguating suffix (e.g. a short branch-name hash) if this
  ever bites. Deferred as low-probability; not worth the churn now. (Note: the
  stale-directory `setup` fix above makes this collision fail loudly instead of
  silently reusing the wrong worktree, which removes most of the risk.)
