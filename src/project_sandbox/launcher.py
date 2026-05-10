from pathlib import Path

from jinja2 import Environment, PackageLoader



def render(
    *,
    output: Path,
    image_tag: str,
    memory: str,
    cpus: int,
    project_abs: Path,
    claude_settings_abs: Path,
    codex_config_abs: Path,
    claude_home_host_abs: Path | None,
    codex_home_host_abs: Path | None,
    ro_creds: bool,
    firewall_enabled: bool,
    agent: str,
    extra_envs: list[str],
) -> Path:
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    tmpl = env.get_template("run-agent.sh.j2")
    output.write_text(
        tmpl.render(
            image_tag=image_tag,
            memory=memory,
            cpus=cpus,
            project_abs=project_abs,
            claude_settings_abs=claude_settings_abs,
            codex_config_abs=codex_config_abs,
            claude_home_host_abs=claude_home_host_abs,
            codex_home_host_abs=codex_home_host_abs,
            ro_creds=ro_creds,
            firewall_enabled=firewall_enabled,
            agent=agent,
            extra_envs=extra_envs,
        )
        + "\n",
        encoding="utf-8",
    )
    output.chmod(0o755)
    return output
