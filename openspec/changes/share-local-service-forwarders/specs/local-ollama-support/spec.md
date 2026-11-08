# Spec Delta

## MODIFIED Requirements

### Requirement: Ollama remains bound to host loopback
When `--pi-ollama` is set, the system SHALL reach an Ollama server listening on `127.0.0.1:11434` through a verified runtime-specific forwarding path. The system SHALL prefer a runtime-native loopback-forwarding mechanism and SHALL use the managed bridge forwarder (see `local-service-forwarding`) only when the selected local Linux bridge runtime lacks a native mechanism. It SHALL NOT bind any listener to `0.0.0.0` or require Ollama to bind beyond loopback.

#### Scenario: Loopback-bound Ollama is reachable
- **WHEN** Ollama is listening on `127.0.0.1:11434` and the user starts a sandbox with `--agent pi --pi-ollama`
- **THEN** the sandbox reaches Ollama through the selected forwarding path without changing Ollama's bind address

#### Scenario: Runtime-native forwarding is available
- **WHEN** the selected runtime provides a verified native mapping from the container to host loopback
- **THEN** the system uses that mapping without starting a bridge forwarder

#### Scenario: Apple localhost DNS is not preconfigured
- **WHEN** Apple `container` is selected and `host.docker.internal` has not been configured with the runtime's localhost DNS facility
- **THEN** startup fails without invoking `sudo` or changing host networking and prints `sudo container system dns create host.docker.internal --localhost 203.0.113.113`, warns that the change can disrupt container Internet access and might disable Private Relay, and instructs the user to restart the container system afterward

#### Scenario: Local Linux bridge fallback is required
- **WHEN** the selected runtime uses a local Linux bridge whose host bridge address is bindable and no native loopback mapping is available
- **THEN** the system uses a bridge forwarder bound on that exact bridge address and forwarding to `127.0.0.1:11434`

#### Scenario: No safe forwarding path
- **WHEN** the selected runtime mode provides neither verified native forwarding nor a safe, host-bindable bridge address
- **THEN** startup fails with a clear unsupported-mode error and does not fall back to a wildcard listener

### Requirement: Ollama forwarding resources have a bounded lifecycle
The system SHALL verify forwarding before starting the sandbox, track resources it creates, detect setup failure, and release only its owned forwarding resources when the sandbox exits or container startup fails. It SHALL NOT create or remove Apple `container` system DNS mappings or invoke `sudo`. When the selected adapter uses the shared bridge forwarder, the session SHALL release its use of that forwarder when it ends. The forwarder's own lifetime is governed by `local-service-forwarding`.

#### Scenario: socat is unavailable
- **WHEN** `socat` is not installed on the host and the selected adapter uses a Linux bridge
- **THEN** startup proceeds without it, because the bridge forwarder needs no external tool

#### Scenario: Proxy cannot listen
- **WHEN** the selected runtime-private address and port cannot be bound and are not held by a compatible project-sandbox forwarder
- **THEN** startup fails before launching the container and reports the proxy failure

#### Scenario: Native forwarding setup fails
- **WHEN** the selected runtime's native mapping cannot be verified or established
- **THEN** startup fails before launching the container and reports the runtime-specific remediation

#### Scenario: Sandbox run ends
- **WHEN** the sandbox container exits normally, is interrupted, or fails to start
- **THEN** owned native-forwarding resources are removed and the session's use of any shared bridge forwarder is released
