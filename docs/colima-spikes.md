# Colima spikes

Technical spikes for the `add-colima-runtime` change. They were run on 2026-11-14 against a Colima `default` profile (Colima v0.10.3, Docker Engine 29.5.2, aarch64, `vmType: vz`, `mountType: virtiofs`, `mounts: []`), always with an explicit `docker --context colima`. The test images were Alpine-based: a plain Squid (`psbx-spike-squid`, `http_port 3128`, allow all) and a client with curl, dig, nc and iproute2. All spike containers, networks and images were removed afterwards.

| Spike | Verdict |
|---|---|
| A. `egress` alias on an internal network | PASS |
| B. Bypass checks | PASS, with one hardening step (WORKAROUND) |
| C. Build proxy | PASS for an address-reachable proxy; BLOCKED for a container-network-only proxy |
| D. Mounts | PASS (see below) |

## A. Alias on an internal network: PASS

```sh
docker network create --driver bridge psbx-egress-uplink
docker network create --driver bridge --internal psbx-sandbox
docker run -d --name psbx-spike-egress --network psbx-egress-uplink psbx-spike-squid
docker network connect --alias egress psbx-sandbox psbx-spike-egress
docker run --rm --network psbx-sandbox psbx-spike-client sh -c '
  getent hosts egress
  curl -sS -x http://egress:3128 -o /dev/null -w "%{http_code}\n" http://example.com/
  curl -sS -x http://egress:3128 -o /dev/null -w "%{http_code}\n" https://example.com/'
```

Observed: the sidecar got `172.18.0.2` on the uplink and `172.19.0.2` with alias `egress` on the internal network. `egress` resolved to `172.19.0.2` from the internal-only client, and the HTTP request and the HTTPS `CONNECT` both returned 200.

Attachment persistence of the sidecar (`docker network connect`):

| Operation | Attachments and alias after |
|---|---|
| `docker restart` | kept, proxy works |
| `docker stop` then `docker start` | kept, proxy works |
| `docker rm` then `docker run` | **lost**: only the uplink is attached and `egress` does not resolve |

So the attach step must run every time the sidecar container is created, and a launch must check that the sidecar is attached before it reads its address.

## B. Bypass checks: PASS with hardening

From a container on `psbx-sandbox` only (no `NET_ADMIN`):

| Probe | Result |
|---|---|
| TCP 1.1.1.1:443 and 8.8.8.8:53 | blocked |
| UDP DNS to 1.1.1.1 | blocked |
| Default resolver (127.0.0.11) for `example.com` and a tunnel-style name | SERVFAIL, so no external DNS |
| IPv6 2606:4700:4700::1111:443 | blocked |
| Internal network gateway, default bridge gateway (172.17.0.1) | blocked |
| A container on the default bridge | blocked |
| Sidecar's uplink address 172.18.0.2:3128 | blocked |
| 169.254.169.254 | blocked |

The sandbox's only route is its own `/16` and the embedded resolver answers container names on the same network only (`psbx-spike-egress` resolved, as expected).

The sidecar forwards by default: `net.ipv4.ip_forward` was `1` inside it, so only the lack of NAT for the internal subnet stopped a `NET_ADMIN` sandbox that set the sidecar as its default gateway. That is accidental protection. **Hardening: run the sidecar with `--sysctl net.ipv4.ip_forward=0`.** With it the value is `0` (checked), Squid still serves requests through both networks, and the `NET_ADMIN` default-gateway test is still blocked. IPv6 forwarding was already `0`.

## C. Build proxy: PASS for an address-reachable proxy, BLOCKED for a container-network-only proxy

The full intercepting-proxy fixture (`scripts/e2e-build-proxy.py`, which needs the external internet-proxy-locally sources) was not available, so TLS interception and CA installation were not tested. They are plain Docker `COPY` and build arguments and don't depend on the runtime. What was tested: whether a build in the Colima VM can use a proxy, with `docker build --build-arg http_proxy=... --build-arg https_proxy=...` of an image that runs `apk add jq`.

| Proxy location | Result |
|---|---|
| Squid on a container network only (`172.18.0.2:3128`) | **fails**: the fetches time out and Squid logs no request |
| Squid published on the VM bridge gateway (`172.17.0.1:3199`) | works: build succeeds and Squid logs `CONNECT dl-cdn.alpinelinux.org:443` |

BuildKit's `RUN` steps do not run on a user-defined bridge, and Docker isolates bridges from each other. A build can therefore reach a proxy by a VM-reachable address (a corporate proxy, or one published on the gateway), but not the private `egress` alias. The proxy hostname inside the VM for macOS services (`host.docker.internal` and `host.lima.internal`, both `192.168.5.2`) resolved, but nothing was tested through it, in line with the change's non-goals.

Consequence for the design: the sidecar image build and any sandbox image build use the existing build-proxy settings and need a VM-reachable address. The private network is for runtime traffic only.

## D. Mounts: PASS, with a staging-location workaround

Run with `scratchpad/spike-d.sh` (every `docker` call used `--context colima`, and the script refuses any context whose endpoint is not under `~/.colima`). The profile's `colima.yaml` has `mounts: []`, but the VM's effective Lima config mounts the host home (`~`) writable over virtiofs: inside the VM, `lima-... on /Users/peter type virtiofs (rw,relatime)`. So an empty `mounts` list does **not** mean no mounts. The default exports all of `$HOME` read-write and nothing else.

| Check (`--mount type=bind`, VZ driver) | Result |
|---|---|
| Read-write workspace at its host path: read, symlink, create, delete | PASS; writes appear on the host and nothing is left over |
| Read-only mount | PASS; writes fail with `Read-only file system` |
| Path outside `$HOME` (`/private/tmp/...`), read-only and read-write | **fails**: `invalid mount config for type "bind": bind source path does not exist` |
| Missing host source | fails the same way; Docker does not create the path |
| Git inside the container: worktree at its host path plus the main `.git` mounted separately | PASS; branch, status and a commit made in the container are visible on the host |

Notes:

- `/private/tmp` (and so macOS `$TMPDIR`, `/var/folders/...`) exists on the Mac but not in the VM. Anything project-sandbox stages outside `$HOME` fails at `docker run` time. `vcs_access.new_mask_dir()` uses `tempfile.mkdtemp`, and that directory is mounted into the container for hidden-VCS mode, so on Colima it must be created under the state directory (`$XDG_STATE_HOME/project-sandbox`, under `$HOME`) instead.
- `--mount` fails loudly on a missing source, but `-v SRC:DST` silently creates an empty directory in the VM. Project-sandbox builds mounts with `--mount` (`_mount_arg` in `container_cli.py`), so the loud form is the one in use.
- `-v SRC:SRC:ro` was intermittent for the workspace and for another directory: most runs saw `No such file or directory` at the mount point and an empty listing, while `--mount ... readonly` and rw `-v` worked every time. The cause is unknown and does not affect project-sandbox. It may deserve its own look.
- Only the VZ driver and virtiofs were tested; QEMU was not available.

## E. Hardened profile (explicit mounts): PASS, with a mount-hint bug found

Run 2026-11-15 on Colima v0.10.3, Docker 29.5.2, VZ/virtiofs, with a throwaway
profile `psbx-spike` (2 CPUs, 2 GiB, 20 GiB), deleted afterwards. Created with:

```sh
S=$HOME/.local/state/project-sandbox
colima start --profile psbx-spike --runtime docker --vm-type vz --mount-type virtiofs \
  --cpu 2 --memory 2 --disk 20 --activate=false --ssh-config=false \
  --mount <workspace-root>:w --mount $S/session-inputs:w --mount $S/mounts:w \
  --mount $S/egress:w --mount $S/vcs-masks:w
```

| Check | Result |
|---|---|
| Profile creation | 22 s, disk image already cached |
| Mounts inside the VM | exactly the five requested; no implicit `/tmp/colima`, no home mount |
| `~/.ssh`, `~/Documents`, `~/.aws`, `sessions/`, `locks/` | not visible (existence tests only) |
| Active Docker context | unchanged (`--activate=false` works); `colima-psbx-spike` context created |
| `--ssh-config=false` | still wrote 2 lines to Colima's own `~/.colima/ssh_config`; removed again by `colima delete` |
| Real `--egress sidecar` launch (real state directories, no XDG override) | PASS: images built, sidecar started from the real `egress/` directory, session inputs mounted and removed, `api.anthropic.com` reached through Squid |
| `scripts/e2e-colima.py` against the profile | 44 passed, 0 failed |
| `doctor --colima-profile psbx-spike` | warned about `sessions/` (see below) |

What the sandbox needs exported is only the bind-mount sources: the workspace
roots, and under `~/.local/state/project-sandbox` the `session-inputs`, `mounts`,
`egress` and `vcs-masks` subtrees. `docker build` sends its context over the API, so
builds need no mount. `sessions/` (host-side records), `locks/`, `projects/` and
`local-forward/` are never bind-mounted.

**`colima start --mount` replaces the profile's mounts; it does not add to them.**
Restarting `psbx-spike` with `--mount /private/tmp:w` left `/private/tmp` as the
only export, and the change persisted to the saved config (`--save-config`
defaults to true). The mount hint printed by the launch preflight and `doctor`
(`colima start --profile P --mount X:w`) is therefore unsafe: on a hardened profile
it drops every export, and on a default profile it replaces the implicit home
mount. The hint has to list the profile's existing exports as well.

`doctor` and the VCS mask directory use `observability.state_dir()`, which is the
session-records directory (`.../project-sandbox/sessions`), not the state root.
`doctor` therefore warns that a correct hardened profile cannot see `sessions/`;
it should check the directories the sandbox actually mounts.

Not checked: `~/.ssh/config` itself (a deny rule blocked the `stat`; compare it
yourself, see the change notes), the QEMU driver, TLS interception through a parent
proxy.


## F. Remaining questions for `colima init` / `add-root`: PASS

Run 2026-11-15 on Colima v0.10.3, Docker 29.5.2, VZ/virtiofs, with a throwaway
profile `psbx-spike` (2 CPUs, 2 GiB, 20 GiB) created by `project-sandbox colima
init --workspace-root ~/psbx-spike-work --colima-profile psbx-spike --cpu 2
--memory 2 --disk 20 --yes`. The active Docker context was `colima` before and
after every step.

### F1. Restart keeps the size and replaces the exports: PASS

`colima stop`, then `colima start --profile psbx-spike <the five --mount flags>
--activate=false --ssh-config=false` (no size flags):

| Check | Result |
|---|---|
| `colima list` after the restart | `2` CPUs, `2GiB`, `20GiB`: the saved size is kept |
| `findmnt -t virtiofs` | exactly the five exports of the list, all `rw`; the order differs from the command line |
| `docker context show` | `colima`, unchanged |

So `add-root` does not need to pass `--cpu`, `--memory` or `--disk`.

### F2. Symlinked state home: PASS, confirms design D3

`~/psbx-spike-link` is a symlink to `~/psbx-spike-real`; `real/f` contains `hi`.
A `docker run --mount type=bind,source=...` then reads `/m/f`:

| Exported | Mount source | Result |
|---|---|---|
| the resolved path | resolved path | `hi` |
| the resolved path | the symlink | `No such file or directory`: an **empty** directory was mounted |
| the symlink | the symlink | `hi` |
| the symlink | resolved path | `No such file or directory` (empty) |

The export has to match the mount source string exactly, so D3's refusal of a
symlinked `XDG_STATE_HOME` stands. The empty result (instead of an error) comes
from a stale directory: a mount point that was once exported stays behind in the
VM as an empty directory after the export is removed, so `docker run --mount`
finds a source and mounts nothing. The launch preflight is not fooled, because it
requires the path to sit on a `virtiofs` mount, not only to exist.

### F3. Running-sandbox detection: PASS

Three containers were started: `project-sandbox-spike-1`,
`project-sandbox-egress` and `other-thing`.

```text
$ docker --context colima-psbx-spike ps --filter name=project-sandbox- --format '{{.Names}}'
project-sandbox-egress
project-sandbox-spike-1
```

The filter finds the sandbox and the sidecar and not `other-thing`; the code
removes `project-sandbox-egress` by name. After `colima stop` / `colima start`
the same command printed nothing and `docker ps -a` showed all three as `Exited
(137)`: a restart ends them.

### F4. Credential locations outside the exports: PASS, with a trap

With `~/.ssh` and `~/.aws` not exported:

| Command | Result |
|---|---|
| `docker run --mount type=bind,source=$HOME/.ssh,target=/m ...` | exit 125, `invalid mount config for type "bind": bind source path does not exist` (same for `.aws`) |
| `docker run -v $HOME/.ssh:/m ...` | exit 0 and an **empty** directory at `/m`: Docker creates `/Users/you/.ssh` in the VM, owned by root |

The host's `~/.ssh` and `~/.aws` were untouched; the created directories exist
only on the VM's disk. The sandbox always uses `--mount`, so an unexported
credential directory fails loudly. A person running `docker -v` against the VM
gets an empty directory, not the host's files.

### F5. Documented commands run as written, and the `hardened` end-to-end check

Run 2026-11-15 on a throwaway profile `psbx-doc` (2 CPUs, 2 GiB, 20 GiB), deleted
afterwards; `docs/colima.md` shows these commands.

| Command | Result |
|---|---|
| `colima init --workspace-root DIR --dry-run` | printed the command (4 CPUs, 8 GiB, `--activate=false --ssh-config=false`); nothing created |
| `colima init --workspace-root ~` | refused: "is the home directory or contains it" |
| `colima init --workspace-root DIR --cpu 2 --memory 2 --disk 20 --yes` | profile created; exports exactly `DIR` and the four state subtrees |
| `doctor --colima-profile psbx-doc` | `exports: dedicated`; `mounts` warned that the repository (not exported) is not visible and named `colima add-root /path --colima-profile psbx-doc` |
| `colima add-root DIR2 --colima-profile psbx-doc --yes` | restart passed all five current exports plus `DIR2`; the VM then exported six paths, all `rw`; the record lists both roots |
| the same `add-root` again | "already exported ... nothing changed" |

`scripts/e2e-colima.py --checks hardened` (profile `psbx-hardened-e2e`, 4 CPUs,
6 GiB, 30 GiB): 7 passed, 0 failed. `init` succeeded; the launch and recover
checks passed against it (34 of 34); the exports were exactly the workspace
root and the four state subtrees; `~/.ssh`, `~/.aws` and `sessions/` were not on
a host-backed mount in the VM; the active Docker context (`colima`) and the
`default` profile were unchanged; and the profile and its `colima-psbx-hardened-e2e`
context were gone afterwards.
