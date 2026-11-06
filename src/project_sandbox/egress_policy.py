"""Host-side egress policy for ``--egress sidecar``.

``$XDG_CONFIG_HOME/project-sandbox/egress.toml`` is the only policy of the
global sidecar (global-egress-sidecar-macos, design.md Decision 9). It holds the
allowlist, the Copilot profile, the upstream parent proxy and its CA, the CA
lifetime, the sidecar's resource limits and the local-service ports it
forwards. Every agent provider host is always allowed on top of ``allow``.

GitHub repository access is blocked in sidecar mode: repository hosts cannot be
allowlisted, and per-repository rules are on the roadmap (ROADMAP.md, "Smart
GitHub repository access"). The file must live outside anything the sandbox can
write, so an agent cannot widen the policy that governs it, now or in a later
session.

Every hostname is validated strictly here because it is later inserted into
``squid.conf``. Edits (``project-sandbox-egress allow|forward``) go through
tomlkit so comments survive, and are re-validated before the file is replaced.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError as TomlkitParseError

from . import proxy_endpoint

POLICY_FILENAME = "egress.toml"
# The sidecar's proxy port; mirrors squid_config.LISTEN_PORT (which imports us).
SIDECAR_PROXY_PORT = 3128
MIN_FORWARD_PORT = 1024
DEFAULT_CA_LIFETIME_DAYS = 7
MAX_CA_LIFETIME_DAYS = 30
DEFAULT_MEMORY = "256M"
DEFAULT_CPUS = 1
_MEMORY_RE = re.compile(r"^[1-9][0-9]{0,5}[KMG]$")


class EgressPolicyError(ValueError):
    """The egress policy file or merged flags are invalid."""


# Same strict shape as firewall._HOSTNAME_RE, minus the optional trailing dot
# (hosts are normalised before matching).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)

# Agent provider hosts admitted when no agent gateway is in use, exactly as the
# firewall-mode DOMAINS list in init-firewall.sh.j2.
PROVIDER_HOSTS: tuple[str, ...] = (
    "api.anthropic.com",
    "claude.ai",
    "code.claude.com",
    "platform.claude.com",
    "api.openai.com",
    "auth.openai.com",
    "chatgpt.com",
)

# Copilot CLI service hosts (Decision 5), always spliced. Task 1.3 captured an
# individual-plan session using api.individual.* for models and chat; the
# business and enterprise hosts are its plan counterparts (not yet captured).
# Telemetry is blocked on purpose: the CLI also posts to
# telemetry.<plan>.githubcopilot.com, which is deliberately not admitted (no
# telemetry leaves the sandbox; the CLI works without it). The IDE-extension
# hosts on GitHub's published allowlist (copilot-proxy/origin-tracker/
# copilot-telemetry.githubusercontent.com, collector.github.com,
# default.exp-tas.com) were unused and are left out.
COPILOT_HOSTS: tuple[str, ...] = (
    "api.individual.githubcopilot.com",
    "api.business.githubcopilot.com",
    "api.enterprise.githubcopilot.com",
)
# GitHub hosts that serve repository content, git, archives, gists or
# repository-scoped APIs. Sidecar mode blocks them: they cannot be allowlisted,
# directly or through a covering wildcard (design.md Decision 4).
GITHUB_REPOSITORY_HOSTS: tuple[str, ...] = (
    "github.com",
    "api.github.com",
    "uploads.github.com",
    "codeload.github.com",
    "gist.github.com",
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "media.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-cloud.githubusercontent.com",
    "release-assets.githubusercontent.com",
)
# The Copilot CLI needs api.github.com for its entitlement check
# (GET /copilot_internal/user; also /copilot_internal/managed_settings). Task
# 1.3: without the host the CLI exits 1. The host is therefore intercepted and
# only COPILOT_API_PATHS pass, so the repository API stays blocked
# (design.md Decision 5).
COPILOT_API_HOST = "api.github.com"
# How the Copilot profile treats api.github.com: "paths" intercepts it and
# admits only COPILOT_API_PATHS; "deny" leaves it out entirely (kept so the
# copilot-egress e2e can re-check it after CLI updates).
COPILOT_API_ACCESS = "paths"
# api.github.com paths the Copilot CLI needs (Squid urlpath_regex,
# case-insensitive). The CLI's update check, GET /repos/github/copilot-cli/
# releases, is repository API and stays denied; the CLI works without it.
COPILOT_API_PATHS: tuple[str, ...] = (r"^/copilot_internal/",)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return True


def normalize_host_pattern(value: object, *, source: str) -> str:
    """Return a lowercase hostname or ``*.``-wildcard pattern, or raise.

    Wildcards are only accepted as a single leading ``*.`` label and must
    cover at least a two-label domain, so ``*.com`` is rejected. IP literals
    are rejected because the sidecar denies IP-literal destinations.
    """
    if not isinstance(value, str):
        raise EgressPolicyError(
            f"Invalid host in {source}: expected a string, got {value!r}."
        )
    raw = value.strip()
    host = raw.lower().rstrip(".")
    wildcard = host.startswith("*.")
    base = host[2:] if wildcard else host
    if not base or not _HOSTNAME_RE.fullmatch(base):
        raise EgressPolicyError(
            f"Invalid host {value!r} in {source}: expected a hostname of letters, "
            "digits, hyphens and dots, optionally prefixed with '*.'."
        )
    if _is_ip_literal(base) or all(label.isdigit() for label in base.split(".")):
        raise EgressPolicyError(
            f"Invalid host {value!r} in {source}: IP literals are not allowed; "
            "the sidecar filters by hostname."
        )
    if wildcard and "." not in base:
        raise EgressPolicyError(
            f"Invalid host {value!r} in {source}: a wildcard must cover a domain "
            "with at least two labels."
        )
    return f"*.{base}" if wildcard else base


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyFile:
    """The parsed contents of an egress policy TOML file."""

    path: Path | None = None
    allow: tuple[str, ...] = ()
    copilot: bool = False
    # [egress.upstream]: the parent proxy URL and CA bundle paths (read at
    # render time; relative paths are relative to the policy file).
    upstream_url: str | None = None
    upstream_ca_cert: tuple[Path, ...] = ()
    # [egress.ca]
    ca_lifetime_days: int = DEFAULT_CA_LIFETIME_DAYS
    # [egress.sidecar]
    memory: str = DEFAULT_MEMORY
    cpus: int = DEFAULT_CPUS
    forward_ports: tuple[int, ...] = ()

    @property
    def upstream(self) -> proxy_endpoint.ProxyEndpoint | None:
        if self.upstream_url is None:
            return None
        return _upstream_endpoint(self.upstream_url, path=self.path)

    def upstream_ca_bytes(self) -> bytes:
        """The concatenated upstream CA bundle, or ``b""`` when none is set."""
        chunks = []
        for ca_path in self.upstream_ca_cert:
            try:
                chunks.append(ca_path.read_bytes())
            except OSError as exc:
                raise EgressPolicyError(
                    f"Cannot read [egress.upstream] ca_cert {ca_path} "
                    f"from {self.path}: {exc.strerror}."
                ) from exc
        return b"".join(chunks)


@dataclass(frozen=True)
class EffectivePolicy:
    """The allowlist and interception the sidecar enforces."""

    policy_path: Path | None
    allow: tuple[str, ...]
    copilot: bool
    bump_set: tuple[str, ...]
    file: PolicyFile = field(default_factory=PolicyFile, compare=False)

    @property
    def interception(self) -> bool:
        return bool(self.bump_set)

    @property
    def name_constraints(self) -> tuple[str, ...]:
        return name_constraints(self.bump_set)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

_TOP_KEYS = {"egress"}
_EGRESS_KEYS = {"allow", "copilot", "upstream", "ca", "sidecar"}
_COPILOT_KEYS = {"enabled"}
_UPSTREAM_KEYS = {"url", "ca_cert"}
_CA_KEYS = {"lifetime_days"}
_SIDECAR_KEYS = {"memory", "cpus", "forward_ports"}


def _reject_unknown(table: dict, allowed: set[str], where: str, path: Path) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise EgressPolicyError(
            f"Unknown key {where}{unknown[0]!r} in egress policy {path}. "
            f"Allowed keys: {', '.join(sorted(allowed))}."
        )


def _table(value: object, where: str, path: Path) -> dict:
    if not isinstance(value, dict):
        raise EgressPolicyError(f"[{where}] in egress policy {path} must be a table.")
    return value


def _string_list(value: object, where: str, path: Path) -> list:
    if not isinstance(value, list):
        raise EgressPolicyError(f"{where} in egress policy {path} must be a list.")
    return value


def _bool(value: object, where: str, path: Path) -> bool:
    if not isinstance(value, bool):
        raise EgressPolicyError(
            f"{where} in egress policy {path} must be true or false."
        )
    return value


def _int(value: object, where: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EgressPolicyError(f"{where} in egress policy {path} must be an integer.")
    return value


def _upstream_endpoint(url: str, *, path: Path | None) -> proxy_endpoint.ProxyEndpoint:
    """Validate like ``--internet-proxy`` (proxy_endpoint), as a policy error."""
    try:
        return proxy_endpoint.parse(url, option="[egress.upstream] url")
    except SystemExit as exc:
        raise EgressPolicyError(f"{exc} (in egress policy {path})") from None


def validate_forward_ports(ports: Iterable[object], *, source: str) -> tuple[int, ...]:
    """Local-service ports the sidecar forwards; privileged ports are refused."""
    result: list[int] = []
    for port in ports:
        if isinstance(port, bool) or not isinstance(port, int):
            raise EgressPolicyError(
                f"Invalid port {port!r} in {source}: expected an integer."
            )
        if not MIN_FORWARD_PORT <= port <= 65535:
            raise EgressPolicyError(
                f"local service port {port} in {source} cannot be forwarded in "
                f"sidecar mode (allowed: {MIN_FORWARD_PORT}-65535)"
            )
        if port == SIDECAR_PROXY_PORT:
            raise EgressPolicyError(
                f"local service port {port} in {source} collides with the sidecar "
                "proxy port"
            )
        if port not in result:
            result.append(port)
    return tuple(sorted(result))


def load(path: Path) -> PolicyFile:
    """Parse and validate an egress policy file."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise EgressPolicyError(
            f"Cannot read egress policy {path}: {exc.strerror}."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise EgressPolicyError(
            f"Invalid TOML in egress policy {path}: {exc}."
        ) from exc
    return parse(data, path=path)


def parse(data: dict, *, path: Path) -> PolicyFile:
    _reject_unknown(data, _TOP_KEYS, "", path)
    egress = _table(data.get("egress", {}), "egress", path)
    if "github" in egress:
        raise EgressPolicyError(
            f"[egress.github] in egress policy {path} is not supported: GitHub "
            "repository access is blocked in sidecar mode (per-repository rules "
            "are on the roadmap). Remove the table; use [egress.copilot] for Copilot."
        )
    _reject_unknown(egress, _EGRESS_KEYS, "egress.", path)

    allow = tuple(
        _allowable_host(item, source=f"egress.allow in {path}")
        for item in _string_list(egress.get("allow", []), "egress.allow", path)
    )

    copilot_table = _table(egress.get("copilot", {}), "egress.copilot", path)
    _reject_unknown(copilot_table, _COPILOT_KEYS, "egress.copilot.", path)
    copilot = _bool(copilot_table.get("enabled", False), "egress.copilot.enabled", path)

    upstream = _table(egress.get("upstream", {}), "egress.upstream", path)
    _reject_unknown(upstream, _UPSTREAM_KEYS, "egress.upstream.", path)
    upstream_url = upstream.get("url")
    if upstream_url is not None:
        if not isinstance(upstream_url, str):
            raise EgressPolicyError(
                f"egress.upstream.url in egress policy {path} must be a string."
            )
        _upstream_endpoint(upstream_url, path=path)
    ca_paths = []
    for item in _string_list(
        upstream.get("ca_cert", []), "egress.upstream.ca_cert", path
    ):
        if not isinstance(item, str) or not item:
            raise EgressPolicyError(
                f"egress.upstream.ca_cert in egress policy {path} must be a list "
                "of file paths."
            )
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        ca_paths.append(candidate)
    if ca_paths and upstream_url is None:
        raise EgressPolicyError(
            f"egress.upstream.ca_cert in egress policy {path} requires "
            "egress.upstream.url."
        )

    ca_table = _table(egress.get("ca", {}), "egress.ca", path)
    _reject_unknown(ca_table, _CA_KEYS, "egress.ca.", path)
    lifetime = _int(
        ca_table.get("lifetime_days", DEFAULT_CA_LIFETIME_DAYS),
        "egress.ca.lifetime_days",
        path,
    )
    if not 1 <= lifetime <= MAX_CA_LIFETIME_DAYS:
        raise EgressPolicyError(
            f"egress.ca.lifetime_days = {lifetime} in egress policy {path} is out of "
            f"range: the CA lifetime must be 1 to {MAX_CA_LIFETIME_DAYS} days "
            f"(maximum {MAX_CA_LIFETIME_DAYS})."
        )

    sidecar = _table(egress.get("sidecar", {}), "egress.sidecar", path)
    _reject_unknown(sidecar, _SIDECAR_KEYS, "egress.sidecar.", path)
    memory = sidecar.get("memory", DEFAULT_MEMORY)
    if not isinstance(memory, str) or not _MEMORY_RE.fullmatch(memory.upper()):
        raise EgressPolicyError(
            f"egress.sidecar.memory in egress policy {path} must be a size such as "
            '"256M" or "1G".'
        )
    cpus = _int(sidecar.get("cpus", DEFAULT_CPUS), "egress.sidecar.cpus", path)
    if not 1 <= cpus <= 64:
        raise EgressPolicyError(
            f"egress.sidecar.cpus in egress policy {path} must be 1 to 64."
        )
    forward_ports = validate_forward_ports(
        _string_list(
            sidecar.get("forward_ports", []), "egress.sidecar.forward_ports", path
        ),
        source=f"egress.sidecar.forward_ports in {path}",
    )

    return PolicyFile(
        path=path,
        allow=allow,
        copilot=copilot,
        upstream_url=upstream_url,
        upstream_ca_cert=tuple(ca_paths),
        ca_lifetime_days=lifetime,
        memory=memory.upper(),
        cpus=cpus,
        forward_ports=forward_ports,
    )


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


def effective(policy: PolicyFile) -> EffectivePolicy:
    """The allowlist the global sidecar enforces (design.md Decision 9).

    ``allow`` plus the provider hosts of every supported agent, plus the Copilot
    profile when ``[egress.copilot] enabled``. GitHub repository hosts are never
    admitted (``_allowable_host`` refuses them in ``allow``).
    """
    hosts: list[str] = [*policy.allow, *PROVIDER_HOSTS]
    bump_set = derive_bump_set(copilot=policy.copilot)
    if policy.copilot:
        hosts.extend(COPILOT_HOSTS)
        # Token exchange, path-restricted by interception (COPILOT_API_ACCESS).
        if COPILOT_API_HOST in bump_set:
            hosts.append(COPILOT_API_HOST)
    return EffectivePolicy(
        policy_path=policy.path,
        allow=_dedupe_hosts(hosts),
        copilot=policy.copilot,
        bump_set=bump_set,
        file=policy,
    )


def _allowable_host(value: object, *, source: str) -> str:
    """Normalise a user-supplied host and refuse GitHub repository hosts."""
    host = normalize_host_pattern(value, source=source)
    blocked = blocked_repository_hosts(host)
    if blocked:
        raise EgressPolicyError(
            f"Host {value!r} in {source} would admit GitHub repository access "
            f"({', '.join(blocked)}), which sidecar mode blocks. Per-repository "
            "access is on the roadmap; use --egress firewall with --allow-github "
            "if you need GitHub now."
        )
    return host


def blocked_repository_hosts(pattern: str) -> tuple[str, ...]:
    """GitHub repository hosts that ``pattern`` matches or, as a wildcard, covers."""
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"; Squid's .x also matches the apex x
        return tuple(
            h for h in GITHUB_REPOSITORY_HOSTS if h == suffix[1:] or h.endswith(suffix)
        )
    return (pattern,) if pattern in GITHUB_REPOSITORY_HOSTS else ()


def _dedupe_hosts(hosts: Iterable[str]) -> tuple[str, ...]:
    """Sort and dedupe host patterns, dropping entries a wildcard covers.

    A ``*.example.com`` pattern is rendered as Squid's ``.example.com``, which
    also matches the apex ``example.com``; Squid warns about overlapping
    entries in one ``dstdomain`` ACL, so covered entries are removed here.
    """
    unique = set(hosts)
    suffixes = [h[1:] for h in unique if h.startswith("*.")]  # ".example.com"

    def covered(host: str) -> bool:
        for suffix in suffixes:
            if host == "*" + suffix:
                continue
            bare = host.removeprefix("*.")
            if bare == suffix[1:] or bare.endswith(suffix):
                return True
        return False

    return tuple(sorted((h for h in unique if not covered(h)), key=_host_sort_key))


def _host_sort_key(host: str) -> tuple[tuple[str, ...], str]:
    bare = host.removeprefix("*.")
    return (tuple(reversed(bare.split("."))), host)


def derive_bump_set(*, copilot: bool) -> tuple[str, ...]:
    """Hosts to intercept: api.github.com, only for the Copilot token exchange.

    Nothing is intercepted unless the Copilot profile is on and
    ``COPILOT_API_ACCESS`` is ``"paths"``.
    """
    if copilot and COPILOT_API_ACCESS == "paths":
        return (COPILOT_API_HOST,)
    return ()


def name_constraints(bump_set: Iterable[str]) -> tuple[str, ...]:
    """Permitted DNS name constraints: the registrable domain of each bumped host.

    Squid mimics the origin certificate's names when it mints (for
    ``api.github.com`` GitHub serves ``*.github.com``/``github.com``), so the
    constraint must cover those names, not just the requested host. An RFC 5280
    ``dNSName`` constraint ``github.com`` permits ``github.com`` and its
    subdomains; any other domain is rejected by a verifying client.
    """
    return tuple(sorted({".".join(host.split(".")[-2:]) for host in bump_set}))


# --------------------------------------------------------------------------
# Policy location
# --------------------------------------------------------------------------


def default_policy_path() -> Path:
    """``$XDG_CONFIG_HOME/project-sandbox/egress.toml`` (``~/.config`` fallback).

    A relative or empty ``XDG_CONFIG_HOME`` is invalid per the XDG spec and is
    ignored, matching ``observability.state_dir``.
    """
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured) if configured.startswith("/") else Path.home() / ".config"
    return base / "project-sandbox" / POLICY_FILENAME


def load_global() -> PolicyFile:
    """Load the global policy, or the built-in defaults when it does not exist."""
    path = default_policy_path()
    if not path.exists() and not path.is_symlink():
        return PolicyFile()
    if not path.is_file():
        raise EgressPolicyError(f"Egress policy {path} is not a file.")
    return load(path)


def check_location(path: Path, protected: Iterable[Path], *, what: str) -> None:
    """Reject ``path`` when it, or a symlink on the way to it, is in ``protected``.

    ``protected`` holds the project, the worktree, the main ``.git`` and every
    sandbox mount source. A mount *containing* the path (``~/.config`` for the
    policy) counts, because the sandbox could then modify or read it.
    """
    roots = [Path(p).expanduser().resolve() for p in protected]
    for location in (*_traversed_paths(path), path.resolve()):
        for root in roots:
            if location == root or root in location.parents:
                raise EgressPolicyError(
                    f"The {what} {path} resolves inside {root}, which is mounted "
                    "into the sandbox: the agent could modify the egress policy or "
                    "read the sidecar's CA key. Keep it outside the project and "
                    "every --mount source."
                )


def check_policy_location(path: Path, protected: Iterable[Path]) -> None:
    check_location(path, protected, what="egress policy")


def _traversed_paths(path: Path, depth: int = 0) -> list[Path]:
    """Every lexical location visited while resolving ``path``.

    This includes each symlink's own location, not only its target: a symlink
    that sits inside the project but points outside it can be re-pointed by
    the agent, so it is as unsafe as a policy file inside the project.
    """
    if depth > 40:
        raise EgressPolicyError(
            f"Too many symlinks while resolving egress policy {path}."
        )
    absolute = Path(os.path.abspath(path))
    visited: list[Path] = []
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    for index, part in enumerate(parts):
        candidate = current / part
        visited.append(candidate)
        if candidate.is_symlink():
            target = Path(os.readlink(candidate))
            if not target.is_absolute():
                target = current / target
            remaining = parts[index + 1 :]
            visited.extend(_traversed_paths(target.joinpath(*remaining), depth + 1))
            return visited
        current = candidate
    return visited


# --------------------------------------------------------------------------
# Editing (project-sandbox-egress allow|forward)
# --------------------------------------------------------------------------


def _edit(path: Path, change) -> PolicyFile:
    """Apply ``change(document)`` to the policy file; validate; replace atomically.

    The file is left byte-identical when the change or the resulting policy is
    invalid. A missing file starts from an empty document.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        raise EgressPolicyError(
            f"Cannot read egress policy {path}: {exc.strerror}."
        ) from exc
    try:
        document = tomlkit.parse(text)
    except TomlkitParseError as exc:
        raise EgressPolicyError(
            f"Invalid TOML in egress policy {path}: {exc}."
        ) from exc
    change(document)
    new_text = tomlkit.dumps(document)
    try:
        policy = parse(tomllib.loads(new_text), path=path)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - tomlkit round-trips
        raise EgressPolicyError(f"Edit produced invalid TOML: {exc}.") from exc
    if new_text != text:
        _replace_atomically(path, new_text)
    return policy


def _replace_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _egress_table(document, *names: str):
    table = document
    for name in ("egress", *names):
        if name not in table:
            table[name] = tomlkit.table()
        table = table[name]
        if not isinstance(table, dict):
            raise EgressPolicyError(
                f"[{'.'.join(('egress', *names))}] must be a table."
            )
    return table


def _array(table, key: str):
    if key not in table:
        array = tomlkit.array()
        array.multiline(True)
        table[key] = array
    value = table[key]
    if not isinstance(value, list):
        raise EgressPolicyError(f"{key} in the egress policy must be a list.")
    return value


def add_allowed_hosts(path: Path, hosts: Iterable[str]) -> tuple[PolicyFile, list[str]]:
    """Add hosts to ``egress.allow``; return the policy and the hosts added."""
    normalized = [_allowable_host(h, source="allow add") for h in hosts]
    added: list[str] = []

    def change(document) -> None:
        array = _array(_egress_table(document), "allow")
        present = {normalize_host_pattern(str(v), source=str(path)) for v in array}
        for host in normalized:
            if host not in present and host not in added:
                array.append(host)
                added.append(host)

    return _edit(path, change), added


def remove_allowed_hosts(
    path: Path, hosts: Iterable[str]
) -> tuple[PolicyFile, list[str]]:
    """Remove hosts from ``egress.allow``; unknown hosts are an error."""
    wanted = [normalize_host_pattern(h, source="allow remove") for h in hosts]
    removed: list[str] = []

    def change(document) -> None:
        array = _array(_egress_table(document), "allow")
        keep = []
        for value in list(array):
            if normalize_host_pattern(str(value), source=str(path)) in wanted:
                removed.append(normalize_host_pattern(str(value), source=str(path)))
            else:
                keep.append(value)
        missing = [h for h in wanted if h not in removed]
        if missing:
            raise EgressPolicyError(
                f"Not in egress.allow of {path}: {', '.join(missing)}"
                + (
                    " (agent provider hosts are always allowed and cannot be removed)"
                    if any(h in PROVIDER_HOSTS for h in missing)
                    else ""
                )
            )
        for index in reversed(range(len(array))):
            if array[index] not in keep:
                del array[index]

    return _edit(path, change), removed


def add_forward_ports(path: Path, ports: Iterable[int]) -> tuple[PolicyFile, list[int]]:
    wanted = validate_forward_ports(ports, source="forward add")
    added: list[int] = []

    def change(document) -> None:
        array = _array(_egress_table(document, "sidecar"), "forward_ports")
        for port in wanted:
            if port not in array:
                array.append(port)
                added.append(port)

    return _edit(path, change), added


def remove_forward_ports(
    path: Path, ports: Iterable[int]
) -> tuple[PolicyFile, list[int]]:
    wanted = list(dict.fromkeys(ports))
    removed: list[int] = []

    def change(document) -> None:
        array = _array(_egress_table(document, "sidecar"), "forward_ports")
        missing = [p for p in wanted if p not in array]
        if missing:
            raise EgressPolicyError(
                f"Not forwarded in {path}: {', '.join(str(p) for p in missing)}"
            )
        for index in reversed(range(len(array))):
            if array[index] in wanted:
                removed.append(int(array[index]))
                del array[index]

    return _edit(path, change), removed
