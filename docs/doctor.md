# Host Prerequisite Checks (`doctor`)

`project-sandbox doctor` inspects the host for everything direct runs depend
on and reports what is ready, what is degraded, and what blocks a session:

```bash
project-sandbox doctor          # human-readable report
project-sandbox doctor --json   # machine-readable report for scripts/CI
project-sandbox doctor --colima-profile work   # also check this Colima profile
```

The command is read-only. It looks up binaries on `PATH` and asks each tool for
its version or status; it never starts a runtime service, builds an image, or
writes files. Each probe times out after 10 seconds, so a hung daemon is
reported rather than blocking the command.

## What is checked

| Section | Check | Status when missing or broken |
| --- | --- | --- |
| Host | Python >= 3.11 | fail |
| Container runtimes | Apple `container` (macOS): version, system service, `host.docker.internal` DNS alias | info (service stopped is fine: direct runs start it) |
| | Docker: client on `PATH` and daemon reachable (`docker version`) | warn |
| | Podman: `podman info` succeeds and reports rootless (rootful Podman, including a rootful Podman machine, is unsupported and refused at startup) | warn |
| | `chroot` (Linux): `unshare --map-root-user --mount` works | warn |
| | `auto`: which runtime `--runtime auto` would select, and whether it works | fail |
| Version control | `git` on `PATH` and its version | fail |
| | global `user.name` / `user.email` | warn |
| | `jj` version and `jj workspace add --no-colocate` support (jj >= 0.46) | info if absent, warn if too old |
| Egress sidecar | For each reachable runtime: whether the global [egress sidecar](egress-sidecar.md) runs, and its published endpoint (one read-only `inspect`) | info |
| Colima | Shown when `colima` is on `PATH` or `--colima-profile` is given (default profile `project-sandbox`). The profile's Docker context exists, points at that profile's socket and its engine answers | info if the profile was never started, warn if stopped or not Colima's |
| | VM size: CPUs and memory against the sandbox default of 4 CPUs (Docker refuses a CPU limit above the VM's, so the default is capped to the VM) | info if the VM has fewer CPUs, with `colima start --cpu 4` to give sandboxes more |
| | Mounts: the current directory and the state directories the sandbox bind-mounts (`session-inputs`, `mounts`, `egress`, `vcs-masks`; not the host-only session records) are visible in the VM (one read-only `colima ssh` call) | warn, naming the path and either `project-sandbox colima add-root PATH` (a profile made by `colima init`) or a `colima start` command that keeps the profile's current exports (`--mount` replaces them) |
| | Networks: `psbx-egress-uplink` and `psbx-sandbox` exist with project-sandbox's labels | info if not created yet, warn for a same-named foreign network |
| | Exports: a profile that exports `$HOME` or an ancestor read-write; for a profile made by `colima init`, whether its exports are exactly its roots plus the four state subtrees (the same read-only `colima ssh` probe) | warn, naming `project-sandbox colima init` for a home export, and each unexpected or missing path for a drifted profile; a read-only home export is not a warning |
| | The profile's sidecar (see [Colima runtime](runtimes.md#colima)): running, and attached to `psbx-sandbox` as `egress` | info, warn if it is running but detached |
| | Each service in `[egress.services]` (see [Colima services](egress-sidecar.md#colima-services-through-the-sidecar)), probed from inside the running sidecar with one `nc -z` to the Mac's port | ok, warn naming a service whose Mac port doesn't answer, with `project-sandbox-egress service remove NAME` |

`--runtime auto` picks the first runtime found on `PATH` (Apple `container`,
then Docker, then Podman on macOS; Docker, then Podman on Linux) without
checking that it works. The `auto` line therefore fails when that pick is
unusable, for example Docker installed with its daemon stopped, and suggests a
working alternative such as `--runtime podman`. A broken runtime that `auto`
would not select is only a warning.

## Exit status and JSON

The exit status is `0` when no check failed and `1` otherwise; warnings do not
change it. `--json` prints:

```json
{
  "ok": true,
  "version": "0.3.0",
  "platform": "linux",
  "checks": [
    {"section": "runtimes", "name": "docker", "status": "ok",
     "detail": "28.1.1; daemon reachable", "hint": null}
  ]
}
```

`status` is one of `ok`, `warn`, `fail`, or `info` (optional component not
present).
