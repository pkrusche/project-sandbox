# Runtimes

`--runtime` selects what runs a direct CLI session. Each runtime builds the same
generated image and mounts the same paths; they differ in the isolation boundary,
how the sandbox reaches host services, and which features they support.

```sh
project-sandbox --runtime docker --agent bash /path/to/repo python:3.12-slim
```

| Runtime | Host | Isolation | Selected by `auto` |
|---|---|---|---|
| `apple-container` | macOS | one VM per container | first choice on macOS |
| `docker` | macOS, Linux | container (Docker Desktop: shared VM) | after Apple `container` on macOS; first on Linux |
| `podman` | macOS, Linux | container, rootless only | last |
| `colima` | macOS | containers in a shared Colima VM | never; explicit only |
| `chroot` | Linux | none (layout check only) | never; explicit only |

`auto` picks the first runtime found on `PATH` without checking that it works;
`project-sandbox doctor` reports the pick and whether it is usable
([doctor](doctor.md)).

## Feature support

| | Apple `container` | Docker | Podman | Colima | chroot |
|---|---|---|---|---|---|
| Firewall egress (default) | yes | yes | yes | yes | no |
| `--egress sidecar` | yes (1.4.1+) | yes; not rootless Docker | yes | yes (private network) | no |
| Local Ollama, loopback `--agent-proxy` | host alias needed | yes | yes | `--egress sidecar` only, as a declared service | no |
| Proxy on the host's loopback (`--internet-proxy`, `--build-proxy`, `[egress.upstream]`) | yes | yes | yes | no; a proxy the VM can reach works | no |

Details of each row live with the feature: [egress sidecar](egress-sidecar.md),
[agent proxy](agent-proxy.md), [Internet proxy](internet-proxy.md). Restricted
VCS modes also differ by runtime; see [VCS access](vcs-access.md).

## Apple `container`

- The default on macOS. Each container runs in its own VM. Requires the `container`
  system service; direct runs start it when it is stopped.
- Host services (agent proxy, Ollama, Internet-proxy relay, sidecar) are reached
  through the `host.docker.internal` localhost DNS domain, which an administrator
  sets up once. project-sandbox verifies it and never runs `sudo`. See
  [Apple container host alias](apple-container-dns.md).
- Restricted VCS modes need directory metadata; commas in metadata mount paths are
  unsupported ([VCS access](vcs-access.md)).

## Docker

- Docker Desktop and OrbStack on macOS; Docker Engine on Linux. Rootless Docker
  is not supported with `--egress sidecar`.
- Docker Engine reaches host services through a shared forwarder bound to the
  default bridge gateway; the sidecar publishes on that gateway too
  ([egress sidecar](egress-sidecar.md#linux-hosts)).
- Generated devcontainers target Docker-compatible runtimes.

## Podman

- Rootless only. `--runtime podman` (and `auto` when it picks Podman) refuses to
  start when `podman info` reports a rootful setup, including running as root or
  a machine set with `--rootful`. Runs pass `--userns=keep-id` so the sandbox
  user can write the workspace.
- With pasta (the Linux default) builds and sessions use their own address,
  `198.18.0.2/24`, and each session forwards only its planned ports to host
  loopback, so proxies on the host's LAN address stay reachable. The firewall
  needs `NET_ADMIN` and `NET_RAW`.
- Details: [security model](security.md#network-firewall),
  [egress sidecar](egress-sidecar.md#linux-hosts).

## chroot

A Linux layout-inspection mode, not a sandbox. It uses rootless
`unshare --map-root-user --mount` to bind the normal mount set into a temporary
chroot and opens Bash (interactive, or headless with `--prompt`). It builds no
image, sets up no firewall, installs no agent CLIs and is no security boundary. It
needs unprivileged user namespaces, which some hardened or nested environments
restrict; CI uses `--runtime docker` for that reason.

## Colima

`--runtime colima` runs sandboxes in a [Colima](https://colima.run) VM through its
Docker engine. A launch only **uses** the VM: it never creates, starts, stops or
reconfigures it and never changes your Docker context. Only the explicit
`project-sandbox colima init` and `colima add-root` commands create and extend a
profile.

### Setup

```sh
project-sandbox colima init --workspace-root ~/src     # recommended: dedicated profile
project-sandbox --runtime colima --agent claude . python:3.12-slim
```

Or start a profile yourself (default name `project-sandbox`):
`colima start --profile project-sandbox`. Use another profile with
`--colima-profile NAME`; it must already be running. `--colima-profile` is
rejected with any other runtime. Check the setup with
`project-sandbox doctor --colima-profile NAME` ([doctor](doctor.md)).

A default Colima VM has 2 CPUs and Docker refuses a CPU limit above the VM's.
Without `--cpus`, a sandbox asks for 4 CPUs, capped to the CPUs the Docker
engine reports (`docker info`), and says so when it caps. For more, start the
profile with `--cpu 4`. `colima init` uses 4 CPUs and 8 GiB.

### How the VM is targeted

Every Docker command, builds and cleanup included, runs as
`docker --context colima-<profile> ...` (`colima` for the profile `default`). An
explicit `--context` beats `DOCKER_HOST` and `DOCKER_CONTEXT`, and
`docker context use` is never run. Before any container work a launch checks,
read-only, that the context exists, that its endpoint is that profile's socket
(`.../<profile>/docker.sock`) and that the engine answers. `--dry-run` prints the
commands and starts nothing.

### Mounts

Docker can bind-mount only paths the VM exports. **The default profile exports
your whole home directory read-write and nothing else**, whatever `mounts:` says
in `colima.yaml`. A project outside `$HOME`, or a source in `/private/tmp` or
`$TMPDIR`, is not visible and `docker run` fails with `bind source path does not
exist`.

A launch asks the VM, with one read-only `colima ssh` call, whether every host
path it mounts sits on a host-backed mount, and reports anything missing with a
fix. If `colima` is not on `PATH` the check is skipped with a warning. With
`--vcs-access hidden` the mask directories are created under the state root
(`vcs-masks`) so they are visible.

**`colima start --mount` replaces the profile's mounts; it does not add to them.**
For a profile from `colima init` the message names
`project-sandbox colima add-root PATH`. For a hand-made profile it reads the VM's
current exports and lists them all in the command it prints. If they cannot be
read it says so and points at `colima ssh --profile NAME -- findmnt -t virtiofs`.

### Dedicated profile

`colima init` creates a profile that exports **only** what the sandbox mounts:

```sh
project-sandbox colima init --workspace-root ~/src [--workspace-root ~/work] [--cpu 4 --memory 8]
project-sandbox colima init --workspace-root ~/src --dry-run   # print the command, run nothing
```

Exported read-write: each `--workspace-root` (required, repeatable, resolved
through symlinks) and four subtrees of `$XDG_STATE_HOME/project-sandbox`:
`session-inputs`, `mounts`, `egress` and `vcs-masks` (created first, mode 0700).
Not exported: your home directory, `~/.ssh`, `~/.aws`, session records, locks.
`session-inputs` holds credentials staged for a session and `egress` holds the
sidecar CA key; the VM's root can read both.

| Option | Meaning |
|---|---|
| `--workspace-root PATH` | directory to export (required, repeatable) |
| `--colima-profile NAME` | profile name (default `project-sandbox`) |
| `--cpu N`, `--memory GiB`, `--disk GiB` | size; default 4 CPUs, 8 GiB, Colima's disk default |
| `--yes` | skip the confirmation (required without a terminal) |
| `--dry-run` | print the command; create and run nothing |

`init` prints the exact `colima start` command and asks to confirm. It always
passes `--activate=false` (your Docker context is not switched) and
`--ssh-config=false` (your `~/.ssh/config` is untouched; Colima still writes its
own `~/.colima/ssh_config`, removed by `colima delete`). It refuses a profile name
that already exists and never stops, restarts or deletes one.

A root is refused when it is not an existing directory, is your home directory or
an ancestor, is a system directory (`/`, `/Users`, `/System`, `/Library`,
`/private`, `/var`, `/etc`), is or contains a credential location (`~/.ssh`,
`~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.docker`), overlaps the sandbox state
directory, or contains `:` or `,`. A symlinked `XDG_STATE_HOME` is refused because
the export and the mount source would differ; `init` names the resolved directory
to use. Symlinks inside a root that point outside it do not resolve in the VM.

Roots are recorded, paths only, in
`$XDG_STATE_HOME/project-sandbox/colima-profiles/<profile>.json` (mode 0600, never
exported) so `doctor` can tell expected exports from unexpected ones.

**Moving from `default`.** A new profile is a separate engine, so images and
volumes do not carry over and the first launch builds again. Roll back with
`colima delete --profile project-sandbox`.

**Adding a root.** `project-sandbox colima add-root ~/more` reads the current
exports from the running VM, validates the new root, and after confirmation runs
`colima stop` and `colima start` with every current export (and its access mode)
plus the new one. It never passes the new path alone, and changes nothing when the
exports cannot be read or the path is already exported. The restart ends running
sandboxes, so it refuses while project-sandbox containers (the sidecar excluded)
run in the profile unless `--force`. `--yes`, `--dry-run` and `--colima-profile`
work as for `init`. Removing a root is not provided.

`doctor` warns about a profile that exports your home directory read-write and,
for a profile with a record, about unexpected exports and unexported roots.
Launches are never refused because of what a profile exports.

### Egress

`--egress firewall` (default) behaves as on Docker Engine, on the profile's default
bridge. The private network below applies to `--egress sidecar` only.

The Squid sidecar and the sandboxes share the VM, so the sandbox reaches Squid over
a private Docker network instead of a published port:

```text
   psbx-egress-uplink (bridge)         psbx-sandbox (--internal bridge)
            │                                     │
            └───────► project-sandbox-egress ◄────┘   alias: egress
                       (Squid, no published port)
                                │                          ▲
                                ▼                          │
                     Internet / parent proxy      sandboxes: HTTP(S)_PROXY=http://egress:3128
```

- Two labelled networks are created in the VM on first use:
  - `psbx-egress-uplink`, an ordinary bridge that only the sidecar uses to reach out;
  - `psbx-sandbox`, an `--internal` bridge with no route out, created with
    `-o com.docker.network.bridge.gateway_mode_ipv4=isolated`. The VM gets no
    address on it, so a sandbox can't reach the VM's own listeners (its sshd, or
    a port another container publishes in the VM). This needs Docker Engine 28 or
    later in the VM: if the engine is older, the first launch says so and names
    `colima update --profile <profile>`.
- The sidecar is created on the uplink, attached to `psbx-sandbox` as `egress`,
  then started. It runs with `net.ipv4.ip_forward=0`, so it can't route between
  the networks, and Squid listens only on its `psbx-sandbox` address.
- A sandbox joins `psbx-sandbox` only, with `egress` pinned to the sidecar's
  address (`--add-host`) and no usable DNS. A process that ignores the proxy
  variables has no route out. Its own firewall, which admits only the
  sidecar's address on 3128 and the declared service ports, is defense in
  depth: off by default, since Colima's containment is verified, and turned
  on with `--sandbox-firewall` (see
  [Sandbox firewall](egress-sidecar.md#sandbox-firewall)).
- Mac services reach the sandbox only as ports on the sidecar: local Ollama at
  `http://egress:11434/v1` and a loopback `--agent-proxy` at
  `http://egress:<port>/…`, each declared once with
  `project-sandbox-egress service add` (see
  [Colima services](egress-sidecar.md#colima-services-through-the-sidecar)). A
  launch that needs an undeclared one fails before any container work and
  prints the command. A remote `--agent-proxy` goes through the forward proxy,
  so its host must be allowlisted and use port 443 (HTTPS) or 80 (HTTP). The
  sandbox gets no `host.docker.internal` or `host.lima.internal`.
- The listener is always `egress:3128`; a non-default `[egress.sidecar] port` is an
  error.

```sh
project-sandbox --runtime colima --egress sidecar --agent claude . python:3.12-slim
project-sandbox-egress up --runtime colima        # also: status, log, restart, down
```

Each profile has its own sidecar, CA, state directory
(`$XDG_STATE_HOME/project-sandbox/egress/colima-<profile>`) and lock.

- **Shared network.** Sandboxes of a profile can reach one another; this is not
  per-sandbox isolation like Apple `container`. They have no route to the
  Internet, the Mac, the VM itself, the VM's bridge gateways or the sidecar's
  uplink side.
- **Upgrading.** A `psbx-sandbox` created by an older version lacks the gateway
  mode. The next launch, `project-sandbox-egress up` or `restart` replaces it
  once no sandbox is attached; until then it refuses, naming the attached
  sandboxes. `project-sandbox doctor` and `project-sandbox-egress status` report
  such a network.
- **Restarts.** Attachments survive `docker restart` and stop/start, not removal
  and recreation. A launch reads the sidecar's address fresh and refuses to start
  when it is detached (`project-sandbox-egress restart` repairs it). Running
  sandboxes keep the old address and fail closed.
- **Cleanup.** `project-sandbox-egress down` removes the container, CA and state
  but keeps the two networks. To remove those and the image when idle:
  `docker --context colima-<profile> network rm psbx-sandbox psbx-egress-uplink`
  and `docker --context colima-<profile> rmi project-sandbox-egress-sidecar:<tag>`.
  A same-named network without project-sandbox's labels is never used or removed.
- **Builds.** Image builds run in the VM and cannot reach the private network, so
  `--build-proxy` needs an address the VM can reach.

### Not supported on Colima

Anything needing a path from the sandbox to macOS is refused before any container
work, naming the feature. Nothing is emulated by opening the sandbox's network:

- in firewall mode: local Ollama (`--pi-ollama`) and `--agent-proxy` on the Mac's
  loopback (the error names `--egress sidecar`, which supports both), and
  `--internet-proxy` on the Mac's loopback;
- in every mode: `[egress.upstream] url` and `--build-proxy` on the Mac's
  loopback (a remote or VM-reachable address works);
- `--egress sidecar` with a non-default `[egress.sidecar] port`.

`colima start/stop/delete`, removing a root and reset are yours to run.

The measurements behind these choices are kept with the archived change
`openspec/changes/archive/2026-11-15-add-colima-hardened-profile/spikes.md`.
