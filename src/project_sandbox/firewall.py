from pathlib import Path

from jinja2 import Environment, PackageLoader


def render(
    context_dir: Path,
    *,
    extra_domains: list[str],
) -> Path:
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("init-firewall.sh.j2")
    container = _write(
        tmpl,
        context_dir / "init-firewall.sh",
        extra_domains=extra_domains,
        allow_host_network=False,
    )
    _write(
        tmpl,
        context_dir / "init-firewall-devcontainer.sh",
        extra_domains=extra_domains,
        allow_host_network=True,
    )
    return container


def _write(tmpl, out: Path, *, extra_domains: list[str], allow_host_network: bool) -> Path:
    out.write_text(
        tmpl.render(extra_domains=extra_domains, allow_host_network=allow_host_network) + "\n",
        encoding="utf-8",
    )
    out.chmod(0o755)
    return out
