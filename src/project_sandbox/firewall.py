from pathlib import Path

from jinja2 import Environment, PackageLoader


def render(
    context_dir: Path,
    *,
    allow_openai: bool,
    extra_domains: list[str],
    no_ipv6_firewall: bool = False,
) -> Path:
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("init-firewall.sh.j2")
    out = context_dir / "init-firewall.sh"
    out.write_text(
        tmpl.render(
            allow_openai=allow_openai,
            extra_domains=extra_domains,
            no_ipv6_firewall=no_ipv6_firewall,
        )
        + "\n",
        encoding="utf-8",
    )
    out.chmod(0o755)
    return out
