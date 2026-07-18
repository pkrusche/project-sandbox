import re
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
) -> Path:
    _validate_domains(extra_domains)
    tmpl = templating.get_template("init-firewall.sh.j2")
    container = _write(
        tmpl,
        context_dir / "init-firewall.sh",
        extra_domains=extra_domains,
        allow_github=allow_github,
        allow_host_network=False,
        pi_ollama=pi_ollama,
    )
    _write(
        tmpl,
        context_dir / "init-firewall-devcontainer.sh",
        extra_domains=extra_domains,
        allow_github=allow_github,
        allow_host_network=True,
        pi_ollama=pi_ollama,
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
) -> Path:
    out.write_text(
        tmpl.render(
            extra_domains=extra_domains,
            allow_github=allow_github,
            allow_host_network=allow_host_network,
            pi_ollama=pi_ollama,
        )
        + "\n",
        encoding="utf-8",
    )
    out.chmod(0o755)
    return out
