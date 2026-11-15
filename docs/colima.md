# Colima runtime

`--runtime colima` runs sandboxes in a [Colima](https://colima.run) VM through
its Docker engine. project-sandbox only **uses** the VM: it never creates,
starts, stops or reconfigures it, and it never changes your Docker context.

## Setup

Start a Colima profile with the Docker runtime yourself. The default profile
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
as its only export, and the change is saved to the profile's config. The message
therefore reads the VM's current exports and lists them all in the command it
prints:

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
`project-sandbox doctor` prints the same command for a directory it cannot see.
If `colima` is not on `PATH` the check is skipped with a warning, and Docker
reports a missing path when the sandbox starts. With `--vcs-access hidden`, the
mask directories are created under the state root
(`$XDG_STATE_HOME/project-sandbox/vcs-masks`) instead of `$TMPDIR` so that they
are visible.

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
| `colima init`, reset, mount management | not provided; you manage the VM |

Spike notes and measurements are in [colima-spikes.md](colima-spikes.md).
