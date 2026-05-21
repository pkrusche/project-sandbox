import shlex
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
    firewall_enabled: bool,
    agent: str,
    extra_envs: list[str],
    opencode_home_host_abs: Path | None = None,
    copilot_home_host_abs: Path | None = None,
) -> Path:
    env = Environment(loader=PackageLoader("project_sandbox", "templates"))
    env.filters["shq"] = _shell_quote
    tmpl = env.get_template("run-agent.sh.j2")
    output.write_text(
        tmpl.render(
            image_tag=image_tag,
            memory=memory,
            cpus=cpus,
            project_abs=project_abs,
            claude_settings_abs=claude_settings_abs,
            claude_config_dir_abs=claude_settings_abs.parent,
            codex_config_abs=codex_config_abs,
            codex_config_dir_abs=codex_config_abs.parent,
            claude_home_host_abs=claude_home_host_abs,
            codex_home_host_abs=codex_home_host_abs,
            opencode_home_host_abs=opencode_home_host_abs,
            copilot_home_host_abs=copilot_home_host_abs,
            firewall_enabled=firewall_enabled,
            agent=agent,
            extra_envs=extra_envs,
        )
        + "\n",
        encoding="utf-8",
    )
    output.chmod(0o755)
    return output


def _shell_quote(value: object) -> str:
    return shlex.quote(str(value))
