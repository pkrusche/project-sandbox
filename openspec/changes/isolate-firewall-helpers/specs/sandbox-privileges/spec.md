# Spec Delta

## Purpose

Defines which code runs with root or elevated capabilities inside a sandbox container, when it runs, what it may read, and how the agent session is kept from regaining those privileges.

## ADDED Requirements

### Requirement: Privileged setup completes before agent code runs
An agent container SHALL run its privileged setup, including firewall initialization and trust-store updates, as root before any agent, shell or user command starts. If privileged setup fails, the container SHALL exit non-zero without starting the session command.

#### Scenario: Firewall initialized before the agent starts
- **WHEN** a sandbox session starts with the firewall enabled
- **THEN** the firewall rules are installed before the session command (agent, shell or headless run) starts

#### Scenario: Firewall failure aborts the session
- **WHEN** firewall initialization fails
- **THEN** the container exits non-zero, prints the failure, and the session command never starts

#### Scenario: Firewall disabled
- **WHEN** a session starts with `--no-firewall`
- **THEN** no firewall rules are installed, and the session still drops privileges as defined below

### Requirement: Agent sessions cannot regain elevated privileges
After privileged setup, an agent container SHALL run the session command as the non-root `agent` user, with `no_new_privs` set, an empty capability bounding set, and no inheritable or ambient capabilities. Agent images SHALL NOT grant the `agent` user any `sudo` rule.

#### Scenario: sudo is unavailable
- **WHEN** a process in an agent session runs `sudo -n /usr/local/bin/project-sandbox-init-firewall` or `sudo -n true`
- **THEN** the command fails and nothing runs as root

#### Scenario: Privilege state of the session
- **WHEN** a process in an agent session reads `/proc/self/status`
- **THEN** it reports `NoNewPrivs: 1`, a zero `CapBnd`, and a non-root UID

#### Scenario: Firewall rules cannot be changed
- **WHEN** a process in an agent session attempts to flush or modify the `iptables` or `ip6tables` rules
- **THEN** the attempt fails and the installed rules are unchanged

#### Scenario: exec into a running container
- **WHEN** a host user runs the runtime's `exec` against a running agent container without specifying a user
- **THEN** the command runs as the `agent` user, not root

### Requirement: Privileged helpers do not load workspace code or configuration
Code that runs as root in a sandbox container SHALL NOT import, source or execute files from `/workspace`, the current directory or the `agent` user's home. It SHALL NOT read tool configuration from those locations either. It SHALL ignore interpreter-influencing environment variables. This SHALL hold even when these locations contain files planted by the repository or by an earlier session.

#### Scenario: Shadow Python modules in the workspace
- **WHEN** `/workspace` contains `ipaddress.py`, `socket.py`, `shlex.py`, `ssl.py` or `pathlib.py`, and a session starts with local or remote proxy destinations that exercise the firewall's Python helpers
- **THEN** none of those files execute, and firewall initialization uses the standard library modules

#### Scenario: Interpreter environment variables
- **WHEN** privileged setup runs with `BASH_ENV`, `ENV`, `PYTHONPATH`, `PYTHONSTARTUP` or `PYTHONHOME` present in the container environment
- **THEN** none of them affect which code privileged setup executes

#### Scenario: Agent-writable tool configuration
- **WHEN** privileged setup runs `curl` while `/workspace` or `/home/agent` contains a `.curlrc` that sets an output file or extra options
- **THEN** that `.curlrc` is not read, and privileged setup writes no file it names

#### Scenario: Devcontainer firewall
- **WHEN** a devcontainer's post-start command runs the firewall script through `sudo`, with shadow modules in `/workspace` and a `.curlrc` in `/home/agent`
- **THEN** none of those files are executed or read, and the script runs with `HOME` set to root's home

### Requirement: Images without the privilege-drop entrypoint are refused
The system SHALL launch an agent session only from an image built with the privilege-drop entrypoint. An existing image that lacks it SHALL be rebuilt, or the launch SHALL be refused.

#### Scenario: Cached image from an earlier version
- **WHEN** a launch would reuse an image built before this change
- **THEN** the build cache treats the image as stale and rebuilds it

#### Scenario: --no-build with an old image
- **WHEN** `--no-build` names an image that lacks the privilege-drop entrypoint
- **THEN** the launch fails before starting a container and tells the user to rebuild without `--no-build`

### Requirement: Devcontainer firewall uses a narrow sudo rule
Until the devcontainer stops initializing a firewall, the devcontainer image SHALL be the only image that contains a sudoers rule. That rule SHALL permit only `/usr/local/bin/project-sandbox-init-firewall`, with `sudo`'s environment reset, `secure_path` and `HOME` set to the target user's home.

#### Scenario: Agent image vs devcontainer image
- **WHEN** both the agent image and the devcontainer image are rendered for a project
- **THEN** only the devcontainer Dockerfile installs `/etc/sudoers.d/agent-firewall`
