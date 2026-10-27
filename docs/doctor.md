# Host Prerequisite Checks (`doctor`)

`project-sandbox doctor` inspects the host for everything direct runs depend
on and reports what is ready, what is degraded, and what blocks a session:

```bash
project-sandbox doctor          # human-readable report
project-sandbox doctor --json   # machine-readable report for scripts/CI
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
| Networking tools | `socat` (Linux; needed for `--pi-ollama` and host-local proxies on bridge networks) | info |
| Version control | `git` on `PATH` and its version | fail |
| | global `user.name` / `user.email` | warn |
| | `jj` version and `jj workspace add --no-colocate` support (jj >= 0.46) | info if absent, warn if too old |

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
