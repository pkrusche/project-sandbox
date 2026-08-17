import re
from dataclasses import dataclass
from pathlib import Path

from . import templating

# Strict hostname: dot-separated labels of letters/digits/hyphens (no leading or
# trailing hyphen per label), with an optional trailing dot. Anything else (command
# substitutions, backticks, quotes, whitespace, newlines, ...) is rejected so a
# malicious --extra-domain cannot execute when the firewall script runs as root.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)"
    r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*"
    r"\.?$"
)


@dataclass(frozen=True)
class LocalTcpDestination:
    label: str
    hostname: str
    port: int


NORMAL = "normal"
INTERNET_PROXY = "internet-proxy"


def _validate_domains(extra_domains: list[str]) -> None:
    for domain in extra_domains:
        if not _HOSTNAME_RE.fullmatch(domain):
            raise ValueError(
                f"Invalid --extra-domain {domain!r}: expected a hostname of "
                "letters, digits, hyphens, and dots."
            )


def render(
    context_dir: Path,
    *,
    extra_domains: list[str],
    allow_github: bool = False,
    pi_ollama: bool = False,
    ollama_hostname: str = "ollama.project-sandbox.internal",
    agent_proxy_port: int | None = None,
    agent_proxy_hostname: str = "agent-proxy.project-sandbox.internal",
    policy: str = NORMAL,
    local_destinations: list[LocalTcpDestination] | None = None,
) -> Path:
    _validate_domains(extra_domains)
    if policy not in (NORMAL, INTERNET_PROXY):
        raise ValueError(f"Unknown firewall policy: {policy}")
    destinations = list(local_destinations or [])
    if not destinations:
        if agent_proxy_port is not None:
            destinations.append(
                LocalTcpDestination("Agent proxy", agent_proxy_hostname, agent_proxy_port)
            )
        if pi_ollama:
            destinations.append(LocalTcpDestination("Ollama", ollama_hostname, 11434))
    tmpl = templating.get_template("init-firewall.sh.j2")
    container = _write(
        tmpl,
        context_dir / "init-firewall.sh",
        extra_domains=extra_domains,
        allow_github=allow_github,
        allow_host_network=False,
        pi_ollama=pi_ollama,
        ollama_hostname=ollama_hostname,
        agent_proxy_port=agent_proxy_port,
        agent_proxy_hostname=agent_proxy_hostname,
        policy=policy,
        local_destinations=destinations,
    )
    _write(
        tmpl,
        context_dir / "init-firewall-devcontainer.sh",
        extra_domains=extra_domains,
        allow_github=allow_github,
        # Proxy mode is gateway-only by default, including in the generated
        # devcontainer variant. Do not retain its broad host-gateway exception.
        allow_host_network=agent_proxy_port is None,
        pi_ollama=pi_ollama,
        ollama_hostname=ollama_hostname,
        agent_proxy_port=agent_proxy_port,
        agent_proxy_hostname=agent_proxy_hostname,
        policy=policy,
        local_destinations=destinations,
    )
    return container


def _write(
    tmpl,
    out: Path,
    *,
    extra_domains: list[str],
    allow_github: bool,
    allow_host_network: bool,
    pi_ollama: bool,
    ollama_hostname: str,
    agent_proxy_port: int | None,
    agent_proxy_hostname: str,
    policy: str,
    local_destinations: list[LocalTcpDestination],
) -> Path:
    out.write_text(
        tmpl.render(
            extra_domains=extra_domains,
            allow_github=allow_github,
            allow_host_network=allow_host_network,
            pi_ollama=pi_ollama,
            ollama_hostname=ollama_hostname,
            agent_proxy_port=agent_proxy_port,
            agent_proxy_hostname=agent_proxy_hostname,
            policy=policy,
            local_destinations=local_destinations,
        )
        + "\n",
        encoding="utf-8",
    )
    out.chmod(0o755)
    return out
