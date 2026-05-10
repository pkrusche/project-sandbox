from pathlib import Path


def render(home: Path, *, name: str | None, email: str | None) -> Path:
    cfg_dir = home / ".config" / "jj"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    out = cfg_dir / "config.toml"
    out.write_text(
        "[user]\n"
        f"name = \"{_toml_string(name or '')}\"\n"
        f"email = \"{_toml_string(email or '')}\"\n",
        encoding="utf-8",
    )
    return out


def _toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
