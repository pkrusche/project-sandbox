# Colima runtime

`--runtime colima` runs sandboxes in a [Colima](https://colima.run) VM through
its Docker engine. A launch only **uses** the VM: it never creates, starts,
stops or reconfigures it, and it never changes your Docker context. Two explicit
commands, `colima init` and `colima add-root`, create and extend a dedicated
profile; see [Dedicated profile](#dedicated-profile).

## Setup

Create the dedicated profile (recommended, it does not export your home
directory; see [Dedicated profile](#dedicated-profile)):

```sh
project-sandbox colima init --workspace-root ~/src
```

or start a Colima profile with the Docker runtime yourself. The default profile
name is `project-sandbox`:

```sh
colima start --profile project-sandbox
```

Then run a sandbox:

```sh
project-sandbox --runtime colima --agent claude . python:3.12-slim
```

Check the setup, read-only, with `project-sandbox doctor --colima-profile NAME`
(see [doctor](doctor.md)). A default Colima profile has 2 CPUs, fewer than the
sandbox default of 4, and Docker refuses a CPU limit above the VM's; pass
`--cpus 2`, or start the profile with `--cpu 4`.

Use another profile with `--colima-profile NAME`; it must already be running
(`colima start --profile NAME`). `--runtime auto` never picks Colima, and
`--colima-profile` is rejected with any other runtime.

## How the VM is targeted

Every Docker command, including builds, image checks and cleanup, is run as
`docker --context colima-<profile> ...` (`colima` for the profile named
`default`). An explicit `--context` wins over `DOCKER_HOST` and `DOCKER_CONTEXT`,
and project-sandbox never runs `docker context use`, so your current context is
untouched. `--dry-run` prints these commands and starts nothing.

Before any build or container work a launch checks, read-only, that:

- the profile's Docker context exists,
- its endpoint is that profile's Colima socket (`.../<profile>/docker.sock`),
  not some other engine that happens to carry the name, and
- the engine answers.

A stopped or missing profile fails with `colima start --profile NAME`.

## Mounts

Docker can only bind-mount paths that the VM exports. **The default profile
exports your whole home directory read-write and nothing else.** (An empty
`mounts:` list in `colima.yaml` does not change that.) Two consequences:

- A project outside `$HOME`, or any mount source in `/private/tmp` or
  `$TMPDIR`, is not visible and `docker run` fails with `bind source path does
  not exist`.
- Everything under `$HOME` is visible to the VM and to any container that is
  asked to mount it. project-sandbox mounts only what a launch needs.

Before the sandbox starts, a launch asks the VM, with one read-only
`colima ssh` call, whether each host path it mounts (the workspace, generated
session inputs, credentials, `--mount` values and the repository's `.git` for
`--branch`) exists on a host-backed mount. Anything missing is reported with a
command that fixes it.

**`colima start --mount` replaces the profile's mounts; it does not add to
them.** Restarting a profile with `--mount /private/tmp:w` leaves `/private/tmp`
as its only export, and the change is saved to the profile's config. For a
profile made with `colima init`, the message therefore names
`project-sandbox colima add-root PATH`, which keeps the current exports (see
below). For a profile you made by hand, the message reads the VM's current
exports and lists them all in the command it prints:

```text
These host paths are not visible inside Colima profile 'project-sandbox', so the sandbox cannot mount them:
  /private/tmp
`colima start --mount` REPLACES the profile's mounts instead of adding to them, so the command must list every path the VM should export, not only the new one.
The VM currently exports: /Users/you (read-write)
  colima stop --profile project-sandbox
  colima start --profile project-sandbox --mount /Users/you:w --mount /private/tmp:w
```

If the current exports cannot be read, the message says so and tells you to list
them with `colima ssh --profile NAME -- findmnt -t virtiofs` instead of guessing.
`project-sandbox doctor` prints the same advice for a directory it cannot see.
If `colima` is not on `PATH` the check is skipped with a warning, and Docker
reports a missing path when the sandbox starts. With `--vcs-access hidden`, the
mask directories are created under the state root
(`$XDG_STATE_HOME/project-sandbox/vcs-masks`) instead of `$TMPDIR` so that they
are visible.

## Dedicated profile

`project-sandbox colima init` creates a profile that exports **only** what the
sandbox bind-mounts, instead of your home directory:

```sh
project-sandbox colima init --workspace-root ~/src
project-sandbox colima init --workspace-root ~/src --workspace-root ~/work --cpu 4 --memory 8
project-sandbox colima init --workspace-root ~/src --dry-run   # print the command, run nothing
```

What the profile exports, all read-write:

- each `--workspace-root` (repeatable, at least one), resolved through symlinks;
- the four state subtrees the sandbox mounts, under
  `$XDG_STATE_HOME/project-sandbox`: `session-inputs`, `mounts`, `egress` and
  `vcs-masks`. They are created first (mode 0700), because a mount source must
  exist.

It does not export your home directory, `~/.ssh`, `~/.aws`, the session records
or the locks. Be aware that `session-inputs` holds credentials staged for the
life of a session and `egress` holds the sidecar CA key, and the VM's root can
read both.

| Option | Meaning |
|---|---|
| `--workspace-root PATH` | directory to export (required, repeatable) |
| `--colima-profile NAME` | profile name (default `project-sandbox`) |
| `--cpu N`, `--memory GiB`, `--disk GiB` | size; default 4 CPUs and 8 GiB so a default launch is not refused by Docker; the disk keeps Colima's default |
| `--yes` | skip the confirmation (required without a terminal) |
| `--dry-run` | print the command, create nothing, run nothing |

`init` prints the exact `colima start` command and asks for confirmation. The
command always passes `--activate=false` (your active Docker context is never
switched) and `--ssh-config=false` (your `~/.ssh/config` is not changed; Colima
still writes its own `~/.colima/ssh_config`, which `colima delete` removes). It
reads `colima start --help` first and stops if one of the flags it relies on is
gone. It refuses a profile name that already exists, running or stopped, and
never stops, restarts or deletes one.

A root is refused when it is not an existing directory, is your home directory or
an ancestor of it, is a system directory (`/`, `/Users`, `/System`, `/Library`,
`/private`, `/var`, `/etc`), is or contains a credential location (`~/.ssh`,
`~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.docker`), overlaps the sandbox
state directory, or contains `:` or `,`. A symlinked `XDG_STATE_HOME` is refused
too, because the export and the sandbox's mount source would not match; `init`
names the resolved directory to use. Symlinks inside an exported root that point
outside it do not resolve in the VM.

The chosen roots are recorded in
`$XDG_STATE_HOME/project-sandbox/colima-profiles/<profile>.json` (mode 0600,
paths only). That directory is never exported. `doctor` uses it to tell your
roots from unexpected exports.

**Moving from `default`.** A new profile is a separate engine: images and
volumes do not carry over, so the first launch builds again. Run
`project-sandbox --runtime colima ...` as before. The old profile can stay or be
removed with Colima's own commands; rollback of the new one is
`colima delete --profile project-sandbox`.

### Adding a root

```sh
project-sandbox colima add-root ~/more-projects
```

`add-root` reads the profile's current exports from the running VM, validates the
new root with the rules above, and after confirmation runs `colima stop` and
`colima start` with **every current export (and its access mode) plus the new
root**, again with `--activate=false --ssh-config=false`. It never passes the new
path alone. It stops without changing anything when the exports cannot be read
(for example the VM is stopped) or the path is already exported. The restart ends
running sandboxes, so it refuses while any project-sandbox container (the egress
sidecar excluded) runs in the profile; `--force` restarts anyway. `--yes`,
`--dry-run` and `--colima-profile` work as for `init`. Removing a root is not
provided.

`doctor` warns about any profile that exports your home directory read-write
(naming `colima init`) and, for a profile with a record, about exports that are
not a root or a state subtree and roots that are not exported. See
[doctor](doctor.md). Launches are never refused because of what a profile
exports.

## Egress

`--egress firewall` (the default) behaves as on Docker Engine: the sandbox runs
on the profile's default bridge with the same firewall. The private-network
isolation below applies to `--egress sidecar` only.

### `--egress sidecar`

The Squid sidecar and the sandboxes run in the same VM, so the sandbox reaches
Squid over a private Docker network instead of through a port published on the
Mac or on the VM's bridge gateway. Nothing about that hop depends on macOS
networking, which is what application firewalls and endpoint-security software
tend to interfere with.

```text
        psbx-egress-uplink (bridge)         psbx-sandbox (--internal bridge)
                 │                                     │
                 └───────► project-sandbox-egress ◄────┘   alias: egress
                            (Squid, no published port)
                                     │                          ▲
                                     ▼                          │
                          Internet / parent proxy      sandboxes: HTTP(S)_PROXY=http://egress:3128
```

- Two labelled networks are created in the profile's VM on first use and reused:
  `psbx-egress-uplink`, an ordinary bridge only the sidecar uses to reach the
  Internet or a parent proxy, and `psbx-sandbox`, an `--internal` bridge with no
  route out.
- The sidecar is created on the uplink and attached to `psbx-sandbox` with the
  alias `egress`. It is started with `net.ipv4.ip_forward=0`, so it cannot act as
  an IP router between the two networks.
- A sandbox joins `psbx-sandbox` only, with `egress` pinned to the sidecar's
  address (`--add-host`), so the sandbox firewall admits that one address on port
  3128 and no DNS. A process that ignores the proxy variables has no route out.
- The listener is always `egress:3128`. `[egress.sidecar] port` is an error on
  Colima when set to anything but its default, because nothing is published.

```sh
project-sandbox --runtime colima --egress sidecar --agent claude . python:3.12-slim
project-sandbox-egress up      --runtime colima          # also: status, log, restart, down
project-sandbox-egress status  --runtime colima --colima-profile work
```

Each profile has its own sidecar, CA, state directory
(`$XDG_STATE_HOME/project-sandbox/egress/colima-<profile>`) and lock, so
`down` on one profile leaves another running.

**Shared network.** All sandboxes of a profile share `psbx-sandbox` and can reach
one another, and the VM's Linux kernel. This is not per-sandbox isolation like
Apple `container`. It does not give a sandbox any route to the Internet, the VM's
bridge gateway or the uplink side of the sidecar, which was checked from a
sandbox-network container (see [colima-spikes.md](colima-spikes.md)).

**Restarts.** Attachments survive `docker restart` and stop/start of the
sidecar, but not removal and recreation. Every creation attaches the alias again.
A launch reads the sidecar's address fresh and refuses to start a sandbox when
the sidecar is not attached (`project-sandbox-egress restart` repairs it);
`project-sandbox-egress status` reports a missing attachment. Sandboxes already
running when the sidecar is recreated keep the old address and fail closed.

**Logs.** `project-sandbox-egress log --denied` lists the denials with each
sandbox's own address on `psbx-sandbox`.

**Cleanup.** `project-sandbox-egress down` removes the container, its CA and its
state, and keeps the two networks (they cost nothing while idle). To remove them
and the sidecar image too, when nothing is using them:

```sh
docker --context colima-project-sandbox network rm psbx-sandbox psbx-egress-uplink
docker --context colima-project-sandbox rmi project-sandbox-egress-sidecar:<tag>
```

A network with one of these names that does not carry project-sandbox's labels
is never used or removed; the launch stops and names it.

### Builds

Image builds (the sandbox image and the sidecar image) run in the VM and cannot
reach the private network. `--build-proxy` therefore needs an address the VM can
reach, such as a corporate proxy; a proxy on the Mac's loopback is refused.

## Not supported on Colima

Anything that needs a path from the sandbox to macOS is refused before any
container work, with a message naming the feature. Nothing is emulated by opening
the sandbox's network.

| Feature | On Colima |
|---|---|
| Local Ollama (`--pi-ollama`) | unsupported |
| `--agent-proxy` to a gateway on the Mac's loopback | unsupported; a remote gateway the VM can reach works |
| `--internet-proxy` on the Mac's loopback (firewall mode) | unsupported; a remote proxy works |
| `[egress.upstream] url` on the Mac's loopback | unsupported; a parent the VM can reach works |
| `--build-proxy` on the Mac's loopback | unsupported; a proxy the VM can reach works |
| `--egress sidecar` with a non-default `[egress.sidecar] port` | error |
| `colima start/stop/delete`, removing a root, reset | not provided beyond `colima init` and `add-root`; you manage the VM |

Spike notes and measurements are in [colima-spikes.md](colima-spikes.md).
