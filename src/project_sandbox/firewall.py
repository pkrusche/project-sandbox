import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import paths, templating
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
# --egress sidecar: per-session CA inputs, mounted read-only into the sandbox;
# mirrors egress_sidecar.SANDBOX_MOUNT.
SIDECAR_INPUT_DIR = "/project-sandbox-egress"


def _validate_domains(extra_domains: list[str]) -> None:
    for domain in extra_domains:
        if not _HOSTNAME_RE.fullmatch(domain):
            raise ValueError(
                f"Invalid --extra-domain {domain!r}: expected a hostname of "
                "letters, digits, hyphens, and dots."
            )


def render(
    context_dir: "paths.Target",
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
    copilot_tenant_hosts: Sequence[str] = (),
    egress_sidecar: bool = False,
) -> Path:
    """Render the sandbox firewall script.

    ``egress_sidecar`` (``--egress sidecar``, Internet-proxy policy only) adds a
    block that installs the sidecar's CA and the upstream proxy's CA from the
    per-session mount at container start.

    ``copilot_tenant_hosts`` are DNS-pinned GHE.com tenant hosts for
    ``--agent copilot --copilot-host <tenant>.ghe.com``.

    The devcontainer has no firewall: it runs with ``--network none``, or on
    the runtime's unfiltered network (see ``devcontainer.render``).
    """
    _validate_domains(extra_domains)
    tenant_hosts = list(copilot_tenant_hosts)
    for host in tenant_hosts:
        if not _HOSTNAME_RE.fullmatch(host):
            raise ValueError(f"Invalid Copilot tenant host {host!r}")
    if policy not in (NORMAL, INTERNET_PROXY):
        raise ValueError(f"Unknown firewall policy: {policy}")
    if egress_sidecar and policy != INTERNET_PROXY:
        raise ValueError("--egress sidecar uses the Internet-proxy firewall policy")
    destinations = list(local_destinations or [])
    if not destinations and agent_proxy_port is not None:
        destinations.append(
            LocalTcpDestination("Agent proxy", agent_proxy_hostname, agent_proxy_port)
        )
    agent_proxy_url = (
        agent_proxy_url or f"http://{agent_proxy_hostname}:{agent_proxy_port}/"
    )
    text = (
        templating.get_template("init-firewall.sh.j2").render(
            extra_domains=extra_domains,
            allow_github=allow_github,
            pi_ollama=pi_ollama,
            ollama_hostname=ollama_hostname,
            agent_proxy_port=agent_proxy_port,
            agent_proxy_hostname=agent_proxy_hostname,
            agent_proxy_url=agent_proxy_url,
            agent_proxy_local=any(
                d.label == "Agent proxy" and d.local for d in destinations
            ),
            policy=policy,
            local_destinations=destinations,
            endpoint_hosts=list(dict.fromkeys(d.hostname for d in destinations)),
            copilot_tenant_hosts=tenant_hosts,
            egress_sidecar=egress_sidecar,
            egress_input_dir=SIDECAR_INPUT_DIR,
        )
        + "\n"
    )
    with paths.target_dir(context_dir) as directory:
        directory.write("init-firewall.sh", text, mode=0o755)
    return paths.target_path(context_dir, "init-firewall.sh")
