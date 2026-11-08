# Spec Delta

## Purpose

Defines how host-loopback services (local Ollama, loopback agent and Internet proxies) are forwarded to sandboxes on Docker Engine's bridge network. This covers how concurrent sessions share a forwarder and how long it lives.

## ADDED Requirements

### Requirement: Concurrent sessions share one bridge forwarder per service port
On Docker Engine, the system SHALL forward each host-loopback service through one forwarder per bridge gateway address and port, shared by every concurrent session that uses that service. A session SHALL reuse a live forwarder rather than fail because the address and port are already bound by it.

#### Scenario: Second session uses the same local service
- **WHEN** a session using local Ollama is running on Docker Engine and a second session using local Ollama starts
- **THEN** the second session starts without a bind error and both sandboxes reach Ollama at `host.docker.internal:11434`

#### Scenario: First session uses the service
- **WHEN** no forwarder exists for the gateway address and port and a session needs that service
- **THEN** the system starts a forwarder bound to exactly that gateway address and port before launching the container

#### Scenario: Different services in concurrent sessions
- **WHEN** one session uses local Ollama and a concurrent session uses a loopback agent proxy on another port
- **THEN** each port gets its own forwarder and neither session's startup is affected by the other

### Requirement: A shared forwarder outlives individual sessions while any session uses it
A forwarder SHALL keep serving while at least one session that uses it is running. The end of one session SHALL NOT interrupt another session's access to the service. This applies whether that session exited normally, was interrupted, failed to start its container, or was killed.

#### Scenario: Session that started the forwarder ends first
- **WHEN** two sessions share a forwarder and the session that started it exits
- **THEN** the remaining session's sandbox still reaches the service, including over connections it opens afterwards

#### Scenario: Session is killed
- **WHEN** a session sharing a forwarder is terminated with SIGKILL
- **THEN** the other sessions keep reaching the service, and the killed session no longer counts as a user of the forwarder

### Requirement: A forwarder stops once no session uses it
A forwarder SHALL stop listening and exit within a few seconds after the last session that uses it ends, however that session ended. A session that starts while a forwarder is stopping SHALL either attach to it before it stops or start a new forwarder. It SHALL NOT attach to a forwarder that then exits under it.

#### Scenario: Last session ends
- **WHEN** the only session using a forwarder exits
- **THEN** the forwarder releases the gateway address and port within a few seconds and no forwarding process remains

#### Scenario: Last session is killed
- **WHEN** the only session using a forwarder is terminated with SIGKILL
- **THEN** the forwarder still exits within a few seconds

#### Scenario: Session starts while the forwarder is stopping
- **WHEN** a session starts at the moment the forwarder has found no remaining users
- **THEN** the session ends up served by a running forwarder for its whole life

### Requirement: A forwarder serves exactly one upstream
A forwarder SHALL forward only to the host-loopback address and port it was started for. A session that needs the same gateway address and port with a different loopback address SHALL fail before launching its container. The error SHALL name both addresses.

#### Scenario: Upstream matches
- **WHEN** a live forwarder forwards to `127.0.0.1:<port>` and a new session needs `127.0.0.1:<port>`
- **THEN** the session reuses it

#### Scenario: Upstream differs
- **WHEN** a live forwarder forwards to `127.0.0.1:<port>` and a new session needs `[::1]:<port>`
- **THEN** startup fails before the container launches with an error naming both loopback addresses

#### Scenario: IPv6 loopback service
- **WHEN** a session needs a service that listens only on `[::1]:<port>`
- **THEN** the forwarder connects to `[::1]:<port>` and the sandbox reaches the service

### Requirement: A foreign listener on the forwarding address is refused
When the gateway address and port are held by a process other than a project-sandbox forwarder, startup SHALL fail before launching the container. It SHALL report that the forwarding proxy cannot bind, and that another process owns the address and port. The system SHALL NOT fall back to a different port or a wildcard address.

#### Scenario: Port taken by an unrelated process
- **WHEN** an unrelated process listens on `<gateway>:11434` and a session needs local Ollama
- **THEN** startup fails before the container launches with a "Cannot bind" error that says another process owns the address and port

### Requirement: Bridge forwarding needs no external tool
Forwarding host-loopback services on Docker Engine SHALL NOT require `socat` or any other host program beyond project-sandbox's own runtime.

#### Scenario: socat is not installed
- **WHEN** `socat` is absent from the host and a session on Docker Engine uses local Ollama
- **THEN** the session starts and the sandbox reaches Ollama

### Requirement: Forwarder state is private to the user
The forwarder's state SHALL live under the user's state directory, `$XDG_STATE_HOME/project-sandbox/`. Directories SHALL be readable only by the user, and so SHALL files. The system SHALL refuse to follow a symlink planted at any of those paths.

#### Scenario: State is created
- **WHEN** a forwarder is started for the first time
- **THEN** its state directory has mode `0700` and its files mode `0600` under `$XDG_STATE_HOME/project-sandbox/`

#### Scenario: Symlink at a state path
- **WHEN** a forwarder state file is a symlink
- **THEN** startup fails with an error naming that path instead of following it
