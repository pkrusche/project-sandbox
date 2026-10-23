import re
from dataclasses import dataclass
from pathlib import Path

from . import templating
from .proxy_endpoint import ProxyEndpoint

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
class TcpDestination:
    label: str
    hostname: str
    port: int
    scheme: str = "http"
    path: str = ""
    local: bool = True

    @classmethod
    def from_endpoint(cls, label: str, endpoint: ProxyEndpoint) -> "TcpDestination":
        return cls(
            label,
            endpoint.sandbox_host,
            endpoint.port,
            endpoint.scheme,
            endpoint.path,
            endpoint.local,
        )

    @property
    def url(self) -> str:
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        return f"{self.scheme}://{host}:{self.port}{self.path}"


# Compatibility for integrations using the original local-only renderer input.
LocalTcpDestination = TcpDestination


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
    ollama_hostname: str = "host.docker.internal",
    agent_proxy_port: int | None = None,
    agent_proxy_hostname: str = "host.docker.internal",
    agent_proxy_url: str | None = None,
    policy: str = NORMAL,
    local_destinations: list[LocalTcpDestination] | None = None,
    devcontainer_destinations: list[LocalTcpDestination] | None = None,
) -> Path:
    """Render the sandbox and devcontainer firewall scripts.

    ``devcontainer_destinations`` defaults to ``local_destinations``; it differs
    when the sandbox reaches an endpoint through a host relay that devcontainer
    tooling, which builds and runs outside project-sandbox, does not have.
    """
    _validate_domains(extra_domains)
    if policy not in (NORMAL, INTERNET_PROXY):
        raise ValueError(f"Unknown firewall policy: {policy}")
    destinations = list(local_destinations or [])
    devcontainer = list(
        destinations if devcontainer_destinations is None else devcontainer_destinations
    )
    for targets in (destinations, devcontainer):
        if not targets and agent_proxy_port is not None:
            targets.append(
                LocalTcpDestination(
                    "Agent proxy", agent_proxy_hostname, agent_proxy_port
                )
            )
    agent_proxy_url = (
        agent_proxy_url or f"http://{agent_proxy_hostname}:{agent_proxy_port}/"
    )
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
        agent_proxy_url=agent_proxy_url,
        policy=policy,
        local_destinations=destinations,
    )
    _write(
        tmpl,
        context_dir / "init-firewall-devcontainer.sh",
        extra_domains=extra_domains,
        allow_github=allow_github,
        # Internet-proxy mode permits only its explicit, port-scoped local
        # destinations, including in the generated devcontainer variant.
        allow_host_network=policy == NORMAL and agent_proxy_port is None,
        pi_ollama=pi_ollama,
        ollama_hostname=ollama_hostname,
        agent_proxy_port=agent_proxy_port,
        agent_proxy_hostname=agent_proxy_hostname,
        agent_proxy_url=agent_proxy_url,
        policy=policy,
        local_destinations=devcontainer,
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
    agent_proxy_url: str,
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
            agent_proxy_url=agent_proxy_url,
            agent_proxy_local=any(
                d.label == "Agent proxy" and d.local for d in local_destinations
            ),
            policy=policy,
            local_destinations=local_destinations,
            endpoint_hosts=list(dict.fromkeys(d.hostname for d in local_destinations)),
        )
        + "\n",
        encoding="utf-8",
    )
    out.chmod(0o755)
    return out
