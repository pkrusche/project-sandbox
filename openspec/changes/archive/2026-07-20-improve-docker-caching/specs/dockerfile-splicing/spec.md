## ADDED Requirements

### Requirement: Custom Dockerfiles use an inherited dependency stage
When rendering a user-supplied `--dockerfile`, the system SHALL place sandbox-owned apt, Node.js, jj, and npm installs in a generated stage and SHALL make the final source stage inherit those installed dependencies.

#### Scenario: Single-stage Dockerfile
- **WHEN** the source has one unnamed stage and no `prefix` stage
- **THEN** the generated dependency stage uses the source `FROM` base and options, and an inherited final stage contains the source stage body

#### Scenario: Multi-stage Dockerfile without prefix
- **WHEN** the source has multiple stages and none is named `prefix`
- **THEN** all stages before the final stage remain intact, while the final stage is split into the dependency stage and an inherited postfix stage preserving its optional alias

#### Scenario: Source edits retain dependency cache
- **WHEN** only files used by final-stage source `COPY` or `RUN` instructions change
- **THEN** sandbox dependency instructions precede those instructions in an ancestor stage and remain cacheable

### Requirement: Prefix stage configures sandbox network installs
The system SHALL recognize a source stage named `prefix` case-insensitively and SHALL run the generated dependency stage from the completed prefix stage.

#### Scenario: Certificate and proxy prefix
- **WHEN** a prefix stage copies a corporate CA, updates certificate trust, or sets proxy/mirror configuration
- **THEN** those instructions execute before sandbox-owned apt, curl, and npm network operations and their resulting state is inherited

#### Scenario: Mixed-case prefix name
- **WHEN** the source declares `AS PREFIX` or another casing of `prefix`
- **THEN** the analyzer recognizes it while preserving the actual stage spelling in generated references

#### Scenario: Transitive final-stage inheritance
- **WHEN** the final source stage descends from prefix through one or more named stages
- **THEN** only the first inheritance edge on that final-stage path is rewritten to inherit the dependency stage, and the remaining chain carries the dependencies to the final image

#### Scenario: Prefix is the final source stage
- **WHEN** the prefix stage is also the last source stage
- **THEN** the system emits a generated final stage inheriting the dependency stage before appending agent and firewall setup

### Requirement: Invalid prefix graphs fail clearly
The system SHALL reject prefix declarations that cannot unambiguously configure the final image.

#### Scenario: Duplicate prefix declarations
- **WHEN** more than one stage is named `prefix` under case-insensitive comparison
- **THEN** rendering fails with an actionable duplicate-prefix error

#### Scenario: Disconnected prefix
- **WHEN** a prefix stage exists but is not an ancestor of the final source stage
- **THEN** rendering fails with an error explaining that the final stage must inherit from prefix

#### Scenario: Numeric stage reference shifted by prefix insertion
- **WHEN** a prefix stage is not the final source stage and any instruction references a build stage by numeric index (`--from=<N>` or `--mount=...,from=<N>`) that is greater than prefix's own index
- **THEN** rendering fails with an error explaining that inserting the dependency stage after prefix would shift that index, and that a named stage reference should be used instead

### Requirement: Source Dockerfile structure is preserved
The system SHALL preserve source semantics outside the selected inheritance edge, including parser directives, global arguments, `FROM` options, stage aliases, unrelated stage branches, and cross-stage copy references.

#### Scenario: Unrelated build branch
- **WHEN** a source contains a stage that is not on the final stage's prefix inheritance path
- **THEN** its `FROM` instruction and body remain unchanged

#### Scenario: Generated name collision
- **WHEN** a source stage alias, or an unaliased source `FROM` base token, already matches the preferred internal dependency-stage name
- **THEN** the system selects a deterministic unused suffixed name

#### Scenario: FROM options and global arguments
- **WHEN** a selected source `FROM` uses options or a base expanded from a global `ARG`
- **THEN** those declarations and options remain effective in the generated dependency stage

### Requirement: Sanitization, warnings, and final setup remain effective
The system SHALL retain restricted-user sanitization and source warnings, resolve base compatibility through stage inheritance, and append agent/config/firewall setup only to the inherited final stage.

#### Scenario: Alias-based apt warning
- **WHEN** the selected stage ultimately inherits an external non-Debian image through one or more aliases
- **THEN** the existing apt-compatibility warning identifies that external image

#### Scenario: Final sandbox setup
- **WHEN** rendering succeeds
- **THEN** source postfix instructions run as root before agent creation, and generated firewall copies, `USER agent`, `/workspace`, entrypoint, and command remain in the final image
