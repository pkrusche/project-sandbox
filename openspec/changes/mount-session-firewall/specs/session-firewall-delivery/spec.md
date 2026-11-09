# Spec Delta

## Purpose

Defines how a firewall-mode or sidecar-mode session's firewall policy reaches the sandbox: as a per-session, read-only runtime input that privileged setup runs, never as part of the image.

## ADDED Requirements

### Requirement: The firewall policy is a per-session runtime input
The system SHALL render each session's firewall script into that session's private staging, and mount it read-only into the container. Privileged setup SHALL run the mounted script. Images SHALL NOT contain a firewall script or any network allowlist.

#### Scenario: Changing network options does not rebuild
- **WHEN** a session is followed by another for the same project that differs only in `--extra-domain`, proxy or Ollama options
- **THEN** the second session reuses the image, and its firewall reflects its own options

#### Scenario: Script is read-only to the agent
- **WHEN** a process in a session tries to change `/project-sandbox-firewall/init-firewall.sh`
- **THEN** the write fails

#### Scenario: Image built without agent options
- **WHEN** an image was built with `--build-only --pi-ollama`, and `--agent pi --pi-ollama --no-build` is launched
- **THEN** the session starts, and Ollama is reachable on its port-scoped rule

### Requirement: A session without its firewall does not start
Unless the host started the container with the firewall explicitly disabled, privileged setup SHALL require the mounted script. If it is absent, the container SHALL exit non-zero before the session command starts, printing the reason.

#### Scenario: Missing script
- **WHEN** the firewall is enabled and `/project-sandbox-firewall/init-firewall.sh` is not mounted
- **THEN** the container exits non-zero before the session command starts

#### Scenario: Firewall disabled by the user
- **WHEN** a session is launched with `--no-firewall`
- **THEN** no script is mounted, no firewall rules are installed, and the session starts

### Requirement: Images with a built-in firewall are refused
The system SHALL NOT launch a session from an image that still contains a built-in firewall script. Such an image SHALL be rebuilt, or the launch SHALL be refused with a rebuild instruction.

#### Scenario: --no-build with an older image
- **WHEN** `--no-build` names an image built before the firewall became a runtime input
- **THEN** the launch fails before starting a container and tells the user to rebuild
