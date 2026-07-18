## ADDED Requirements

### Requirement: Template-owned installs precede user Dockerfile content
When rendering a Dockerfile from a user-supplied `--dockerfile`, the system SHALL place its own base-image-only instructions — the `apt-get` toolchain install, Node.js install, jj install, and per-agent `npm install -g` calls — immediately after the effective `FROM`/`ARG`/`USER root` block and before any instruction from the user's Dockerfile that follows its own final-stage `FROM`.

#### Scenario: Source edit does not invalidate template installs
- **WHEN** a user rebuilds a `--dockerfile` image after editing only source files referenced by a `COPY`/`RUN` step in their own Dockerfile
- **THEN** the rendered Dockerfile's apt-get, Node.js, jj, and npm agent-install layers appear before that user step, so Docker's build cache reuses those layers unchanged

#### Scenario: No user Dockerfile content precedes template installs
- **WHEN** a Dockerfile is rendered from any `--dockerfile` input
- **THEN** no `COPY`, `RUN`, or other instruction copied from the user's sanitized source text appears earlier in the output than the template's apt-get/Node.js/jj/npm-install blocks

### Requirement: Multi-stage user Dockerfiles keep named stages together with the final FROM
The system SHALL split the sanitized source Dockerfile text at the final stage's `FROM` instruction, placing that `FROM` and any earlier named build stages in the emitted "from part," and placing every instruction that follows the final `FROM` in the emitted "rest part."

#### Scenario: Multi-stage source Dockerfile
- **WHEN** the user's `--dockerfile` declares one or more earlier named stages (e.g., `FROM golang:1.22 AS builder`) followed by a final stage's `FROM`
- **THEN** the rendered Dockerfile keeps all named stages and the final stage's `FROM` together, ahead of the template's dependency installs, and any `COPY --from=<stage>` reference in the user's final-stage instructions continues to resolve correctly

#### Scenario: Single-stage source Dockerfile
- **WHEN** the user's `--dockerfile` has exactly one `FROM`
- **THEN** the "from part" is that single `FROM` (plus any preceding comments/ARGs in the same leading block) and the "rest part" is everything after it

### Requirement: User Dockerfile content still runs as root before the agent user exists
The system SHALL continue to run the spliced user Dockerfile content (the "rest part") as `root`, after the template's dependency installs and before the `agent` user, config-directory, and firewall/entrypoint setup.

#### Scenario: User content can install packages
- **WHEN** the user's Dockerfile's rest-part contains `RUN apt-get install` or similar privileged steps
- **THEN** those steps execute successfully because `USER root` is still in effect and the base apt tooling installed by the template is already present

#### Scenario: User content cannot assume the agent user exists
- **WHEN** the user's Dockerfile's rest-part references the `agent` user, its home directory, or `/project-sandbox-config`/`/project-sandbox-secrets` paths
- **THEN** those references are not yet valid at that point in the build, since `agent`-user creation and config-directory setup happen only after the rest-part splice

### Requirement: Sanitization and warning behavior is unchanged by the reorder
The system SHALL apply the same restricted-user-setup sanitization (`_remove_restricted_user_setup`), base-image compatibility warning, and `WORKDIR`-mismatch warning to the source Dockerfile regardless of the new from-part/rest-part split.

#### Scenario: Restricted user setup still stripped
- **WHEN** the user's Dockerfile contains `USER`, `useradd`, `groupadd`, or equivalent instructions targeting a non-root user
- **THEN** those instructions are still removed from the rendered output and a warning is still emitted, exactly as before the reorder

#### Scenario: Base image and WORKDIR warnings still fire
- **WHEN** the user's Dockerfile's final stage uses a non-apt base image, or sets a `WORKDIR` other than `/workspace` alongside local install commands
- **THEN** the corresponding warning is still emitted, using the same final-stage `FROM`/`WORKDIR` detection as before
